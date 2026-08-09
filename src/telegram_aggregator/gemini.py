from __future__ import annotations

import logging
import re
import mimetypes

from google import genai
from google.genai import types

from .models import ListingExtraction, SourceMedia

# Используется только если GEMINI_MODEL=auto (или не задан) И сам вызов
# ListModels (автообнаружение) упал — например, сетевая проблема или права
# ключа. Порядок примерно от дешёвой/быстрой к более мощной модели.
_DEFAULT_MODEL_FALLBACKS = [
    "gemini-3.5-flash-lite",
    "gemini-3.5-flash",
    "gemini-3.5-pro",
    "gemini-2.5-flash-lite",
    "gemini-2.5-flash",
    "gemini-2.5-pro",
]


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
            return self._candidate_models

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
        return ordered

    def analyze(self, text: str, media: list[SourceMedia]) -> ListingExtraction:
        contents: list[object] = [text.strip() or " "]
        for item in media:
            contents.append(
                types.Part.from_bytes(
                    data=item.data,
                    mime_type=item.mime_type,
                )
            )

        last_error: Exception | None = None
        for model_name in self._available_models():
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
                status_code = getattr(exc, "status_code", None) or getattr(exc, "code", None)
                logging.warning("Gemini model %s failed (status=%s): %s — trying next model", model_name, status_code, exc)
                last_error = exc
                continue

        if last_error is not None:
            raise last_error
        raise RuntimeError("Gemini returned no usable models")


def guess_mime_type(path: str) -> str:
    mime_type, _ = mimetypes.guess_type(path)
    if mime_type:
        return mime_type
    return "image/jpeg"