from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


ListingType = Literal["sell", "free", "exchange", "wanted", "service", "other"]
ListingCategory = Literal["Дом", "Мебель", "Электроника", "Одежда", "Детское", "Спорт", "Авто", "Услуги", "Другое"]

APPLICATION_STATUSES = (
    "NEW",
    "CONTACTED",
    "SELLER_CONTACTED",
    "SELLER_INTERESTED",
    "DEAL_IN_PROGRESS",
    "COMPLETED",
    "CANCELLED",
    "DISPUTED",
)

ACTIVE_APPLICATION_STATUSES = (
    "NEW",
    "CONTACTED",
    "SELLER_CONTACTED",
    "SELLER_INTERESTED",
    "DEAL_IN_PROGRESS",
)

STATUS_EVENT_MAP = {
    "CONTACTED": "application_contacted",
    "SELLER_CONTACTED": "seller_contacted",
    "DEAL_IN_PROGRESS": "deal_started",
    "COMPLETED": "deal_completed",
    "CANCELLED": "deal_cancelled",
    "DISPUTED": "deal_disputed",
}


class ListingExtraction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    is_listing: bool = Field(description="Whether the message is a real listing.")
    listing_type: ListingType | None = Field(default=None, description="Type of listing.")
    title: str | None = Field(default=None, description="Short human-friendly title.")
    category: ListingCategory | None = Field(default=None, description="Listing category.")
    price: float | None = Field(default=None, description="Price if explicitly stated.")
    currency: str | None = Field(default=None, description="Currency code if explicitly stated.")
    location: str | None = Field(default=None, description="Location if explicitly stated.")
    condition: str | None = Field(default=None, description="Condition if explicitly stated.")
    description: str | None = Field(default=None, description="Short normalized description.")
    contact_info: str | None = Field(default=None, description="Contact details if explicitly stated.")
    confidence: float = Field(ge=0.0, le=1.0, description="Confidence from 0 to 1.")

    @field_validator("title", "category", "currency", "location", "condition", "description", "contact_info", mode="before")
    @classmethod
    def _normalize_strings(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = str(value).strip()
        return value or None


@dataclass(slots=True)
class SourceMedia:
    data: bytes
    mime_type: str


@dataclass(slots=True)
class SourceMessageBundle:
    message_id: int
    chat_id: int
    grouped_id: int | None
    text: str
    media: list[SourceMedia]
    seller_link: str | None
    source_link: str | None
    raw_message_ids: list[int]


@dataclass(slots=True)
class ListingResult:
    listing_id: int | None
    bundle: SourceMessageBundle
    extraction: ListingExtraction
    post_text: str
    dedupe_fingerprint: str
