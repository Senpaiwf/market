# market/backend/marketplaces/ozon.py
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

_ATTRS_CACHE = Path(__file__).parent.parent / "ozon_category_attrs_cache.json"

# Ozon attribute matching rules.
# Key = internal MS value source, value = substrings to match against Ozon attr name (lowercase).
# Order matters — first match wins, so put more specific patterns first.
#
# Units found in Ozon attr names (from actual cache):
#   weight: "вес товара, г" / "вес с упаковкой, г"  → GRAMS (kg * 1000)
#   dims:   "высота, мм" / "длина, мм" / "ширина, мм" → MM
#
# MS custom attributes present in ALL products:
#   "Бренд"           → brand (more reliable than extracted product.brand)
#   "Вес_товара"      → weight in kg as string
#   "Размеры_товара"  → "depth/width/height" in cm as string
#   "длина"           → depth in mm as string
#   "ширина"          → width in mm as string
#   "высота"          → height in mm as string

_MS_TO_OZ: list[tuple[str, list[str]]] = [
    # Brand — prefer custom attr "Бренд", not extracted product.brand (often wrong)
    ("brand",        ["бренд", "brand", "производитель", "торговая марка"]),
    # Full product name
    ("name",         ["название товара", "наименование товара"]),
    # Model name for card grouping (Ozon id=9048)
    ("model_name",   ["название модели"]),
    # Description / annotation
    ("description",  ["аннотация", "описание", "annotation"]),
    # Weight in GRAMS (Ozon stores "г" in attr name)
    ("weight_g",     ["вес товара, г", "вес нетто, г", "масса нетто, г",
                       "вес товара", "вес нетто", "масса нетто"]),
    # Weight with packaging in GRAMS
    ("weight_g",     ["вес с упаковкой, г", "вес с упаковкой", "масса брутто"]),
    # Dimensions in MM (Ozon stores "мм" in attr name)
    ("depth_mm",     ["длина, мм", "глубина, мм", "длина"]),
    ("width_mm",     ["ширина, мм", "ширина"]),
    ("height_mm",    ["высота, мм", "высота"]),
    # Composite "Размеры, мм" — provide as "depth/width/height" in mm
    ("dims_mm_str",  ["размеры, мм", "размеры мм", "габариты, мм"]),
    # Seller article / part number
    ("article",      ["код продавца", "артикул продавца", "артикул производителя",
                       "артикул", "парт.номер", "parт-номер", "sku"]),
    # Barcode
    ("barcode",      ["штрихкод", "barcode", "gtin", "ean"]),
    # Country of origin
    ("country",      ["страна-изготовитель", "страна изготовитель", "страна производства",
                       "страна происхождения"]),
]

# Fixed values always written regardless of product data.
# Key = substring to match against Ozon attr name (lowercase), value = string to set.
_OZ_FIXED: list[tuple[str, str]] = [
    ("количество оптом", "1"),
    ("гарантийный срок", "1 Год"),
]


def _nfc(d: dict) -> dict:
    """Re-key dict with NFC-normalized, stripped keys (guards against MS API encoding quirks)."""
    return {unicodedata.normalize("NFC", k).strip(): v for k, v in d.items()}


def _build_ms_values(product: dict) -> dict[str, str]:
    """Extract all mappable MS values into a flat dict with multiple unit variants."""
    attrs = _nfc(product.get("attributes") or {})
    dims = product.get("dims_cm") or {}

    # Brand: custom attr "Бренд" is always correct; product.brand often wrong
    # (e.g. product "Клетка Tilta для FUJIFILM GFX" has brand="Fujifilm" but Бренд="Tilta")
    brand = attrs.get("Бренд") or product.get("brand", "")

    # Weight
    weight_kg = product.get("weight_kg") or 0.0
    # Ozon weight attrs expect grams
    weight_g = str(int(round(float(weight_kg) * 1000))) if weight_kg else ""

    # Dimensions in mm — prefer precomputed dims_mm, fallback to MS custom attrs
    depth_mm  = dims.get("depth_mm")  or _to_num(attrs.get("длина",  ""))
    width_mm  = dims.get("width_mm")  or _to_num(attrs.get("ширина", ""))
    height_mm = dims.get("height_mm") or _to_num(attrs.get("высота", ""))

    # "Размеры_товара" is "depth/width/height" in cm — convert to mm for Ozon
    # Ozon "Размеры, мм" attr expects same slash-separated format but in mm
    dims_mm_str = ""
    raw_dims = attrs.get("Размеры_товара", "")
    if raw_dims:
        try:
            parts = [float(x) for x in str(raw_dims).replace(",", ".").split("/")]
            dims_mm_str = "/".join(str(int(p * 10)) for p in parts)
        except (ValueError, IndexError):
            pass

    # Country of origin — usually not in MS data, leave empty so warning fires for required attrs
    country = attrs.get("Страна_производства") or attrs.get("Страна производства") or ""

    # Model name: use article as model identifier (Ozon uses this to group card variants)
    model_name = product.get("article", "")

    return {
        "brand":       str(brand),
        "name":        str(product.get("name", "") or ""),
        "model_name":  str(model_name),
        "description": str(product.get("description", "") or ""),
        "weight_g":    weight_g,
        "depth_mm":    str(int(depth_mm))  if depth_mm  else "",
        "width_mm":    str(int(width_mm))  if width_mm  else "",
        "height_mm":   str(int(height_mm)) if height_mm else "",
        "dims_mm_str": dims_mm_str,
        "article":     str(product.get("article", "") or ""),
        "barcode":     str(product.get("barcode", "") or ""),
        "country":     str(country),
    }


def _to_num(v) -> float:
    try:
        return float(str(v).replace(",", "."))
    except (ValueError, TypeError):
        return 0.0


class OzonAdapter(MarketplaceAdapter):
    def __init__(self, ozon_client=None):
        # ozon_client: OzonClient instance — used only on cache miss
        self._client = ozon_client

    async def get_category_attributes(self, category_id: str) -> list[dict]:
        # 1. Try local cache
        try:
            data = json.loads(_ATTRS_CACHE.read_text(encoding="utf-8"))
            if category_id in data:
                return data[category_id]
        except (FileNotFoundError, json.JSONDecodeError):
            data = {}

        # 2. Fetch from Ozon API on cache miss
        if self._client:
            parts = str(category_id).split("_")
            try:
                desc_cat_id = int(parts[0])
                type_id = int(parts[1]) if len(parts) > 1 else None
            except (ValueError, IndexError) as e:
                logger.warning("Cannot parse Ozon category_id '%s': %s", category_id, e)
                return []

            result = await self._client.get_category_attributes(desc_cat_id, type_id)
            if result.get("ok"):
                attrs = result["attributes"]
                data[category_id] = attrs
                try:
                    _ATTRS_CACHE.write_text(
                        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
                    )
                except OSError as e:
                    logger.warning("Cannot write Ozon attrs cache: %s", e)
                return attrs
            logger.warning(
                "Ozon API returned error for category %s: %s",
                category_id, result.get("error"),
            )

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
            key = f"oz_{attr_id}"

            matched: str | None = None

            # Skip attrs like "Поддерживаемые бренды пылесосов" — they are
            # compatibility lists, not the product's own brand/name/etc.
            _skip_prefixes = ("поддерживаемые", "совместимые", "подходящие")
            if any(attr_name_lower.startswith(p) for p in _skip_prefixes):
                continue

            # 0. Fixed values (always win — set regardless of product data)
            for pattern, fixed_val in _OZ_FIXED:
                if pattern in attr_name_lower:
                    matched = fixed_val
                    break

            if matched:
                updated[key] = matched
                continue

            # 1. Category rules (battery → Класс 9, Li-Ion etc.)
            if _HAS_SPEC_EXTRACTOR and extracted:
                allowed_vals = []
                for v in (attr.get("values") or attr.get("dictionary") or []):
                    if isinstance(v, dict):
                        n = v.get("value") or v.get("name")
                        if n:
                            allowed_vals.append(n)
                val = get_category_rules(extracted, attr_name_lower, allowed_vals or None)
                if val:
                    matched = val

            # 2. Structured map (ordered, first match wins)
            if not matched:
                for ms_field, patterns in _MS_TO_OZ:
                    if any(p in attr_name_lower for p in patterns):
                        val = ms_values.get(ms_field, "")
                        if val:
                            matched = val
                            break

            # 3. Fuzzy fallback: raw MS custom attributes by name similarity
            if not matched:
                for ms_key, ms_val in ms_custom.items():
                    # Skip generic single-word keys that would over-match
                    if len(ms_key) < 4:
                        continue
                    if ms_key in attr_name_lower or attr_name_lower in ms_key:
                        matched = ms_val
                        break

            if matched:
                updated[key] = matched
            elif attr.get("required"):
                warnings.append(f"Нет данных для '{attr.get('name')}'")

        return updated, warnings, errors
