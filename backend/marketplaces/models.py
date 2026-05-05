# market/backend/marketplaces/models.py
from __future__ import annotations
from typing import Any, Literal
from pydantic import BaseModel


class FillRequest(BaseModel):
    code: str
    marketplace: Literal["yandex", "ozon", "wb"]
    category_id: str
    ms_token: str
    ym_api_key: str = ""
    ym_campaign_id: str = ""
    ym_business_id: str = ""
    ozon_client_id: str = ""
    ozon_api_key: str = ""
    wb_api_key: str = ""


class FieldRating(BaseModel):
    name: str
    weight: float
    filled: bool
    value: Any = None


class RatingResult(BaseModel):
    score: float  # 0–100
    missing_mandatory: list[str]
    recommendations: list[str]
    status: Literal["high", "medium", "low"]
    details: list[FieldRating]


class FillResponse(BaseModel):
    status: Literal["success", "partial", "error"]
    marketplace: str
    category_id: str
    rating: float
    rating_result: RatingResult
    updated_fields: dict[str, Any]
    warnings: list[str]
    errors: list[str]
