# market/backend/marketplaces/yandex.py
from __future__ import annotations
import json
import logging
import unicodedata
from pathlib import Path

from .base import MarketplaceAdapter

logger = logging.getLogger(__name__)

try:
    import sys, os
    sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
    from spec_extractor import extract_specs, get_category_rules
    _HAS_SPEC_EXTRACTOR = True
except ImportError:
    _HAS_SPEC_EXTRACTOR = False

_PARAMS_CACHE = Path(__file__).parent.parent / "yandex_category_params_cache.json"

# YM attribute matching rules.
# Key = internal MS value source, value = substrings to match against YM param name (lowercase).
# Order matters — first match wins.
#
# Units for YM:
#   weight → kg  (product.weight_kg, already in kg)
#   dims   → cm  (dims_cm.*, already in cm)
#
# MS custom attributes present in ALL products:
#   "Бренд"           → brand (more reliable than extracted product.brand)
#   "Вес_товара"      → weight in kg (string)
#   "Размеры_товара"  → "depth/width/height" in cm as string
#   "длина"           → depth in mm (numeric string)
#   "ширина"          → width in mm (numeric string)
#   "высота"          → height in mm (numeric string)

_MS_TO_YM: list[tuple[str, list[str]]] = [
    # Brand — prefer custom attr "Бренд", not extracted product.brand (often wrong)
    ("brand",        ["бренд", "brand", "производитель", "торговая марка"]),
    # Description
    ("description",  ["описание"]),
    # Weight in kg (YM uses kg)
    ("weight_kg",    ["вес товара", "вес нетто", "масса нетто", "вес"]),
    # Dimensions in cm (YM uses cm, dims_cm already converted from MS mm)
    ("depth_cm",     ["глубина товара", "глубина", "длина товара", "длина упаковки"]),
    ("width_cm",     ["ширина товара", "ширина упаковки", "ширина"]),
    ("height_cm",    ["высота товара", "высота упаковки", "высота"]),
    # Article / part number
    ("article",      ["артикул производителя", "артикул продавца", "артикул", "sku", "parт-номер"]),
    # Barcode
    ("barcode",      ["штрихкод", "barcode", "gtin", "ean"]),
    # Country
    ("country",      ["страна-изготовитель", "страна изготовитель", "страна производства"]),
]


def _nfc(d: dict) -> dict:
    """Re-key dict with NFC-normalized, stripped keys (guards against MS API encoding quirks)."""
    return {unicodedata.normalize("NFC", k).strip(): v for k, v in d.items()}


def _build_ms_values(product: dict) -> dict[str, str]:
    """Extract all mappable MS values into a flat dict."""
    attrs = _nfc(product.get("attributes") or {})
    dims = product.get("dims_cm") or {}

    # Brand: custom attr "Бренд" is always correct; product.brand often wrong
    # (e.g. product "Клетка Tilta для FUJIFILM GFX" has brand="Fujifilm" but Бренд="Tilta")
    brand = attrs.get("Бренд") or product.get("brand", "")

    # Weight in kg (already correct unit for YM)
    weight_kg = product.get("weight_kg") or 0.0
    weight_kg_str = str(weight_kg) if weight_kg else ""

    # Dims in cm (YM uses cm, dims_cm already converted)
    depth_cm  = dims.get("depth_cm")
    width_cm  = dims.get("width_cm")
    height_cm = dims.get("height_cm")

    # Country of origin
    country = attrs.get("Страна_производства") or attrs.get("Страна производства") or ""

    return {
        "brand":       str(brand),
        "name":        str(product.get("name", "") or ""),
        "description": str(product.get("description", "") or ""),
        "weight_kg":   weight_kg_str,
        "depth_cm":    str(depth_cm)  if depth_cm  else "",
        "width_cm":    str(width_cm)  if width_cm  else "",
        "height_cm":   str(height_cm) if height_cm else "",
        "article":     str(product.get("article", "") or ""),
        "barcode":     str(product.get("barcode", "") or ""),
        "country":     str(country),
    }


class YandexAdapter(MarketplaceAdapter):
    async def get_category_attributes(self, category_id: str) -> list[dict]:
        try:
            data = json.loads(_PARAMS_CACHE.read_text(encoding="utf-8"))
            entry = data.get(str(category_id))
            if entry:
                return entry.get("parameters", [])
        except (FileNotFoundError, json.JSONDecodeError) as e:
            logger.warning("YM params cache read error for %s: %s", category_id, e)
        return []

    def map_ms_to_attributes(
        self, product: dict, attributes: list[dict]
    ) -> tuple[dict, list[str], list[str]]:
        ms_values = _build_ms_values(product)
        # Also index raw MS custom attributes by lowercase name for fuzzy fallback
        ms_custom = {
            unicodedata.normalize("NFC", k).strip().lower().replace("_", " "): str(v)
            for k, v in (product.get("attributes") or {}).items() if v
        }

        updated: dict = {}
        warnings: list[str] = []
        errors: list[str] = []

        # Pre-extract specs from name + description for rules layer
        extracted = {}
        if _HAS_SPEC_EXTRACTOR:
            name = product.get("name", "")
            desc = product.get("description", "") or ""
            extracted = extract_specs(name, desc)

        for attr in attributes:
            attr_id = attr.get("id")
            if not attr_id:
                continue
            attr_name_lower = attr.get("name", "").lower()
            key = f"ym_{attr_id}"

            matched: str | None = None

            # 1. Category rules (battery → Класс 9, Li-Ion etc.)
            if _HAS_SPEC_EXTRACTOR and extracted:
                allowed_vals = []
                for v in (attr.get("values") or []):
                    if isinstance(v, dict):
                        n = v.get("value") or v.get("name")
                        if n:
                            allowed_vals.append(n)
                val = get_category_rules(extracted, attr_name_lower, allowed_vals or None)
                if val:
                    matched = val

            # 2. Structured map (ordered, first match wins)
            if not matched:
                for ms_field, patterns in _MS_TO_YM:
                    if any(p in attr_name_lower for p in patterns):
                        val = ms_values.get(ms_field, "")
                        if val:
                            matched = val
                            break

            # 3. Fuzzy fallback: raw MS custom attributes by name similarity
            if not matched:
                for ms_key, ms_val in ms_custom.items():
                    if len(ms_key) < 4:
                        continue
                    if ms_key in attr_name_lower or attr_name_lower in ms_key:
                        matched = ms_val
                        break

            if matched:
                updated[key] = matched
            elif attr.get("required"):
                warnings.append(f"Нет данных для обязательного поля '{attr.get('name')}'")

        return updated, warnings, errors
