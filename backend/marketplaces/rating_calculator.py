# market/backend/marketplaces/rating_calculator.py
from __future__ import annotations
import json
import logging
from pathlib import Path

from .models import FieldRating, RatingResult

logger = logging.getLogger(__name__)

_WEIGHTS_FILE = Path(__file__).parent.parent / "config" / "categories_rating_weights.json"

_DEFAULT_WEIGHTS: dict = {
    "fields": [
        {"name": "name",        "source": "name",        "type": "mandatory",   "weight": 2.0},
        {"name": "description", "source": "description", "type": "recommended", "weight": 1.5},
        {"name": "brand",       "source": "brand",       "type": "mandatory",   "weight": 1.0},
        {"name": "price",       "source": "price_main",  "type": "mandatory",   "weight": 1.0},
        {"name": "images",      "source": "images",      "type": "mandatory",   "weight": 2.0},
        {"name": "dims",        "source": "dims_cm",     "type": "recommended", "weight": 0.5},
    ]
}


def _load_weights(marketplace: str, category_id: str) -> dict:
    try:
        data = json.loads(_WEIGHTS_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError) as e:
        logger.warning("categories_rating_weights.json read error: %s", e)
        return _DEFAULT_WEIGHTS

    mp_section = data.get(marketplace, {})
    return (
        mp_section.get(category_id)
        or mp_section.get("_default")
        or data.get("_default")
        or _DEFAULT_WEIGHTS
    )


def _is_filled(source: str, product: dict, filled_attrs: dict) -> tuple[bool, object]:
    """Return (filled, value) for a given source key."""
    if source == "name":
        v = product.get("name", "")
        return bool(v and len(str(v)) >= 5), v
    if source == "description":
        v = product.get("description", "")
        return bool(v and len(str(v)) >= 50), v
    if source == "brand":
        v = product.get("brand", "")
        return bool(v), v
    if source == "price_main":
        v = product.get("price_main") or product.get("price_ym") or 0
        return bool(v and v > 0), v
    if source == "images":
        imgs = product.get("images", [])
        return bool(imgs), len(imgs)
    if source == "dims_cm":
        dims = product.get("dims_cm") or {}
        filled = all(dims.get(k) for k in ("width_cm", "height_cm", "depth_cm"))
        return filled, dims
    if source == "weight_kg":
        v = product.get("weight_kg") or 0
        return bool(v and v > 0), v
    if source == "barcode":
        v = product.get("barcode", "")
        return bool(v), v
    # Fallback: check filled_attrs dict (auto-mapped marketplace fields)
    v = filled_attrs.get(source)
    return bool(v), v


def calculate_rating(
    product: dict,
    filled_attrs: dict,
    marketplace: str,
    category_id: str,
) -> RatingResult:
    config = _load_weights(marketplace, category_id)
    fields = config.get("fields", [])

    total_weight = sum(f["weight"] for f in fields)
    earned_weight = 0.0
    missing_mandatory: list[str] = []
    recommendations: list[str] = []
    details: list[FieldRating] = []

    for field in fields:
        name = field["name"]
        source = field.get("source", name)
        weight = float(field.get("weight", 1.0))
        ftype = field.get("type", "optional")

        filled, value = _is_filled(source, product, filled_attrs)

        if filled:
            earned_weight += weight
        else:
            if ftype == "mandatory":
                missing_mandatory.append(name)
            elif ftype == "recommended":
                recommendations.append(f"Рекомендуется заполнить: {name}")

        details.append(FieldRating(name=name, weight=weight, filled=filled, value=value))

    score = round((earned_weight / total_weight * 100) if total_weight > 0 else 0.0, 1)
    status: str = "high" if score >= 80 else ("medium" if score >= 50 else "low")

    return RatingResult(
        score=score,
        missing_mandatory=missing_mandatory,
        recommendations=recommendations,
        status=status,  # type: ignore[arg-type]
        details=details,
    )
