# backend/ai_matcher.py
# Матчинг характеристик B&H → параметры категории Яндекс.Маркет через OpenAI gpt-4o-mini.

import os
import json
from typing import Optional

try:
    from openai import AsyncOpenAI
    _HAS_OPENAI = True
except ImportError:
    _HAS_OPENAI = False


_client: Optional["AsyncOpenAI"] = None


def _get_client():
    global _client
    if _client is not None:
        return _client
    if not _HAS_OPENAI:
        return None
    key = os.getenv("OPENAI_API_KEY", "").strip()
    if not key:
        return None
    _client = AsyncOpenAI(api_key=key)
    return _client


async def match_specs_to_ym_params(
    bh_specs: dict,
    ym_params: list,
    product_name: str = "",
) -> dict:
    """
    Матчит B&H specs → параметры ЯМ через OpenAI.

    Args:
        bh_specs: {"Power": "96W", "Color Temperature": "2700-6500K", ...}
        ym_params: [{"id": 1234, "name": "Мощность, Вт", "type": "NUMERIC",
                     "required": true, "values": [...]}, ...]
        product_name: для контекста

    Returns:
        {"matches": [{"param_id": 1234, "param_name": "Мощность, Вт",
                      "value": "96", "confidence": 0.95}],
         "confidence": 0.87}
    """
    client = _get_client()
    if not client or not bh_specs or not ym_params:
        return {"matches": [], "confidence": 0.0, "error": "no_client_or_empty"}

    # Упрощаем ym_params для промпта (убираем лишние поля)
    simplified_params = []
    for p in ym_params[:80]:  # лимит чтобы не раздувать промпт
        sp = {
            "id": p.get("id"),
            "name": p.get("name", ""),
            "type": p.get("type", ""),
            "required": p.get("required", False),
        }
        if p.get("unit"):
            sp["unit"] = p["unit"]
        # для вариантных — список допустимых значений
        vals = p.get("values") or p.get("dictionary") or []
        if vals and isinstance(vals, list):
            names = [v.get("value") or v.get("name") for v in vals[:30] if isinstance(v, dict)]
            names = [n for n in names if n]
            if names:
                sp["allowed_values"] = names
        simplified_params.append(sp)

    system = (
        "You are an expert at matching product specifications between B&H Photo Video "
        "and Yandex.Market. Given B&H specs and a list of Yandex.Market category parameters, "
        "find the best matches. Convert units where needed (inches→cm, lb→kg, °F→°C). "
        "For parameters with allowed_values, you MUST pick one from the list (exact string). "
        "Skip parameters where you are not confident (<0.5). "
        "Return strict JSON: "
        '{"matches": [{"param_id": <int>, "param_name": "<str>", "value": "<str>", "confidence": <0..1>}], '
        '"overall_confidence": <0..1>}.'
    )

    user_payload = {
        "product_name": product_name,
        "bh_specs": bh_specs,
        "ym_parameters": simplified_params,
    }

    try:
        resp = await client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False)},
            ],
            response_format={"type": "json_object"},
            temperature=0.1,
            max_tokens=2000,
        )
        content = resp.choices[0].message.content or "{}"
        data = json.loads(content)
        matches = data.get("matches", []) or []
        # Нормализуем поля
        norm = []
        for m in matches:
            if not isinstance(m, dict):
                continue
            pid = m.get("param_id")
            val = m.get("value")
            if pid is None or val is None or val == "":
                continue
            norm.append({
                "param_id": pid,
                "param_name": m.get("param_name", ""),
                "value": str(val),
                "confidence": float(m.get("confidence", 0.8)),
            })
        return {
            "matches": norm,
            "confidence": float(data.get("overall_confidence", 0.0)),
        }
    except Exception as e:
        return {"matches": [], "confidence": 0.0, "error": str(e)[:200]}


async def ai_enrich_product(
    product: dict,
    ym_params: list,
    bh_specs: Optional[dict] = None,
) -> dict:
    """
    Единый вызов OpenAI: на основании данных товара из МС и (опционально) B&H specs
    заполняет всё, что AI способен определить: бренд, описание, параметры категории ЯМ.

    Returns:
        {
          "brand": "SmallRig" | "",
          "description": "<товарное описание для ЯМ>",
          "parameter_values": [{param_id, param_name, value, confidence}...],
          "overall_confidence": 0..1,
        }
    """
    client = _get_client()
    if not client:
        return {"brand": "", "description": "", "parameter_values": [], "overall_confidence": 0.0,
                "error": "no_openai_key"}

    # Упрощаем параметры для промпта
    simplified = []
    for p in (ym_params or [])[:100]:
        sp = {
            "id": p.get("id"),
            "name": p.get("name", ""),
            "type": p.get("type", ""),
            "required": p.get("required", False),
        }
        if p.get("unit"):
            sp["unit"] = p["unit"]
        vals = p.get("values") or p.get("dictionary") or []
        if vals and isinstance(vals, list):
            names = [v.get("value") or v.get("name") for v in vals[:40] if isinstance(v, dict)]
            names = [n for n in names if n]
            if names:
                sp["allowed_values"] = names
        simplified.append(sp)

    payload = {
        "product_name": product.get("name", ""),
        "product_description": (product.get("description") or "")[:1500],
        "ms_attributes": product.get("attributes", {}),
        "dims_cm": product.get("dims_cm", {}),
        "weight_kg": product.get("weight_kg", 0),
        "bh_specs": bh_specs or {},
        "ym_parameters": simplified,
    }

    system = (
        "You extract product metadata for Yandex.Market listings. "
        "Given product data (from MoySklad ERP and optionally B&H Photo specs) and a list of "
        "Yandex.Market category parameters, produce strict JSON with three fields:\n"
        "  - brand: the TRUE manufacturer of the accessory/product. "
        "The store sells camera accessories from niche brands (SmallRig, Portkeys, Tilta, Sennheiser, etc.). "
        "Famous consumer brands like Sony, Canon, Nikon, Fujifilm, DJI, Panasonic, GoPro "
        "often appear in the product name as compatibility info — do NOT use them as brand. "
        "Extract the accessory maker brand. Never use generic words like 'Кабель', 'Монитор'. "
        "Examples: 'SmallRig case for Fujifilm' → SmallRig; 'Portkeys cable for Sony' → Portkeys. "
        "Return empty string if brand is truly unknown.\n"
        "  - description: a natural, useful Russian description (150-600 chars), "
        "specific to THIS product. Never generic marketing fluff.\n"
        "  - parameter_values: array of {param_id, param_name, value, confidence}. "
        "For parameters with allowed_values, value MUST be EXACTLY one string from allowed_values. "
        "For numeric/unit params, value must be a number as string (e.g. '6'). "
        "Only include parameters you are confident about (>=0.5). "
        "Convert units (inches→cm, lb→kg, °F→°C) when needed.\n"
        "Strict JSON: {\"brand\":\"\",\"description\":\"\",\"parameter_values\":[...],"
        "\"overall_confidence\":0..1}."
    )

    try:
        resp = await client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
            ],
            response_format={"type": "json_object"},
            temperature=0.2,
            max_tokens=3000,
        )
        data = json.loads(resp.choices[0].message.content or "{}")
        pvals = []
        for m in data.get("parameter_values", []) or []:
            if not isinstance(m, dict):
                continue
            pid = m.get("param_id")
            val = m.get("value")
            if pid is None or val in (None, ""):
                continue
            pvals.append({
                "param_id": pid,
                "param_name": m.get("param_name", ""),
                "value": str(val),
                "confidence": float(m.get("confidence", 0.7)),
            })
        return {
            "brand": (data.get("brand") or "").strip(),
            "description": (data.get("description") or "").strip(),
            "parameter_values": pvals,
            "overall_confidence": float(data.get("overall_confidence", 0.0)),
        }
    except Exception as e:
        return {"brand": "", "description": "", "parameter_values": [],
                "overall_confidence": 0.0, "error": str(e)[:200]}
