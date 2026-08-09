from __future__ import annotations

import logging
import re
import time
import mimetypes

from google import genai
from google.genai import types
from google.genai.errors import ClientError

from .models import ListingExtraction, SourceMedia

_DEFAULT_MODEL_FALLBACKS = [
    "gemini-3.5-flash-lite",
    "gemini-3.5-flash",
    "gemini-3.5-pro",
    "gemini-2.5-flash-lite",
    "gemini-2.5-flash",
    "gemini-2.5-pro",
]

_MAX_RETRY_WAIT_SECONDS = 15  # не ждём дольше этого за один заход


SYSTEM_PROMPT = """Ты — классификатор и редактор объявлений для европейской Telegram-барахолки.
Проанализируй сообщение пользователя и изображения.
Определи, является ли сообщение реальным объявлением.
Если это объявление, извлеки только информацию, которая явно присутствует в тексте или изображениях.
Не выдумывай цену, город, состояние, характеристики или контактные данные.
Сделай короткий понятный title.
Сделай краткое описание без изменения смысла исходного сообщения.
Определи категорию.
Если цена не указана, верни null.
Если город не указан, верни null.
Если это не объявление, is_listing=false.
Не включай в описание служебные инструкции, комментарии или рассуждения.
Верни только JSON согласно предоставленной схеме."""


class GeminiAnalyzer:
    def __init__(self, api_key: str, model: str) -> None:
        self._client = genai.Client(api_key=api_key)
        self._model = model
        self._candidate_models: list[str] | None = None
        self._blocked_models: set[str] = set()  # модели с limit:0 — не пытаемся снова

    def _normalize_model_name(self, model_name: str) -> str:
        model_name = model_name.strip()
        if model_name.startswith("models/"):
            return model_name.split("/", 1)[1]
        return model_name

    def _model_priority(self, model_name: str) -> tuple[int, int, str]:
        normalized = self._normalize_model_name(model_name)
        if self._model and self._normalize_model_name(self._model) == normalized:
            return (-1, 0, normalized)

        if "flash-lite" in normalized:
            family_score = 0
        elif "flash" in normalized:
            family_score = 1
        elif "pro" in normalized:
            family_score = 2
        else:
            family_score = 3

        match = re.search(r"gemini-(\d+)\.(\d+)", normalized)
        if match:
            version_score = -(int(match.group(1)) * 10 + int(match.group(2)))
        else:
            version_score = 0

        return (family_score, version_score, normalized)

    def _available_models(self) -> list[str]:
        if self._candidate_models is not None:
            return [m for m in self._candidate_models if m not in self._blocked_models]

        discovered: list[str] = []
        try:
            pager = self._client.models.list(config=types.ListModelsConfig())
            for model in pager:
                supported_actions = getattr(model, "supported_actions", None) or []
                if "generateContent" not in supported_actions:
                    continue
                name = self._normalize_model_name(getattr(model, "name", ""))
                if name:
                    discovered.append(name)
        except Exception:
            logging.warning("Gemini model discovery (ListModels) failed; falling back to static model list", exc_info=True)
            discovered = []

        ordered: list[str] = []
        seen: set[str] = set()
        if self._model and self._normalize_model_name(self._model) != "auto":
            seeds = [self._normalize_model_name(self._model)]
        else:
            seeds = []

        for candidate in seeds + sorted(discovered, key=self._model_priority):
            if not candidate or candidate in seen:
                continue
            seen.add(candidate)
            ordered.append(candidate)

        if not ordered:
            if self._model and self._normalize_model_name(self._model) != "auto":
                ordered = [self._normalize_model_name(self._model)]
            else:
                ordered = list(_DEFAULT_MODEL_FALLBACKS)
                logging.info("Using static fallback model list: %s", ordered)

        self._candidate_models = ordered
        return [m for m in ordered if m not in self._blocked_models]

    @staticmethod
    def _parse_quota_error(exc: Exception) -> tuple[bool, float | None]:
        """Возвращает (permanently_blocked, retry_delay_seconds)."""
        if not isinstance(exc, ClientError) or exc.code != 429:
            return False, None

        details = getattr(exc, "details", None) or {}
        error_body = details.get("error", details) if isinstance(details, dict) else {}
        violations = []
        retry_delay = None
        for detail in error_body.get("details", []) if isinstance(error_body, dict) else []:
            detail_type = detail.get("@type", "")
            if detail_type.endswith("QuotaFailure"):
                violations.extend(detail.get("violations", []))
            elif detail_type.endswith("RetryInfo"):
                raw_delay = detail.get("retryDelay", "")
                match = re.match(r"([\d.]+)s?", raw_delay)
                if match:
                    retry_delay = float(match.group(1))

        # limit: 0 в сообщении означает модель в принципе недоступна на тарифе,
        # а не "квота на сегодня закончилась" — ждать бессмысленно.
        message = str(error_body.get("message", "")) if isinstance(error_body, dict) else str(exc)
        permanently_blocked = bool(re.search(r"limit:\s*0\b", message))

        return permanently_blocked, retry_delay

    def analyze(self, text: str, media: list[SourceMedia]) -> ListingExtraction:
        contents: list[object] = [text.strip() or " "]
        for item in media:
            contents.append(
                types.Part.from_bytes(
                    data=item.data,
                    mime_type=item.mime_type,
                )
            )

        max_total_wait_seconds = 120  # общий бюджет ожидания на одно объявление
        started_at = time.monotonic()
        last_error: Exception | None = None

        while True:
            models_to_try = self._available_models()
            if not models_to_try:
                if last_error is not None:
                    raise last_error
                raise RuntimeError("Gemini returned no usable models (all permanently blocked)")

            made_progress_this_pass = False

            for model_name in models_to_try:
                try:
                    response = self._client.models.generate_content(
                        model=model_name,
                        contents=contents,
                        config=types.GenerateContentConfig(
                            system_instruction=SYSTEM_PROMPT,
                            response_mime_type="application/json",
                            response_json_schema=ListingExtraction.model_json_schema(),
                        ),
                    )
                    extraction = ListingExtraction.model_validate_json(response.text)
                    self._model = model_name
                    return extraction
                except Exception as exc:
                    permanently_blocked, retry_delay = self._parse_quota_error(exc)
                    if permanently_blocked:
                        logging.warning("Gemini model %s has zero quota on this plan — excluding permanently", model_name)
                        self._blocked_models.add(model_name)
                        made_progress_this_pass = True  # список кандидатов изменился
                    elif retry_delay is not None:
                        elapsed = time.monotonic() - started_at
                        if elapsed + retry_delay > max_total_wait_seconds:
                            logging.error(
                                "Gemini model %s rate-limited (retry in %.1fs) but total wait budget (%ds) exceeded — giving up",
                                model_name, retry_delay, max_total_wait_seconds,
                            )
                            last_error = exc
                            continue
                        logging.warning("Gemini model %s rate-limited, waiting %.1fs before trying next model", model_name, retry_delay)
                        time.sleep(retry_delay)
                    else:
                        logging.warning("Gemini model %s failed: %s — trying next model", model_name, exc)
                    last_error = exc
                    continue

            elapsed = time.monotonic() - started_at
            if elapsed >= max_total_wait_seconds:
                logging.error("Exhausted %ds wait budget trying Gemini models, giving up", max_total_wait_seconds)
                raise last_error if last_error is not None else RuntimeError("Gemini analyze() timed out")

            if not made_progress_this_pass:
                # ни одна модель не была заблокирована навсегда в этом проходе —
                # небольшая пауза перед повторным полным обходом списка, чтобы не
                # долбить API впустую
                time.sleep(2)


def guess_mime_type(path: str) -> str:
    mime_type, _ = mimetypes.guess_type(path)
    if mime_type:
        return mime_type
    return "image/jpeg"