# backend/ai_gemini.py
# Gemini AI integration (gemini-2.5-flash, google-genai SDK 3.x)

import os
import json
import re
import asyncio
import httpx
from typing import Optional

try:
    from google import genai
    _HAS_GEMINI = True
except ImportError:
    genai = None
    _HAS_GEMINI = False

MODELS = [
    "gemini-3.1-pro-preview",       # Gemini 3.1 Pro
    "gemini-3-pro-preview",          # Gemini 3.0 Pro
    "gemini-3.1-flash-lite-preview", # Gemini 3.1 Flash
    "gemini-3-flash-preview",        # Gemini 3.0 Flash
    "gemini-2.5-flash",              # fallback
    "gemini-2.5-flash-lite",         # last fallback
]
_client = None
_configured_key = ""


def _get_client(api_key: str = ""):
    """Return (client, None) or (None, error_str)."""
    global _client, _configured_key
    if not _HAS_GEMINI:
        return None, "Библиотека google-genai не установлена (docker compose up --build)"
    key = (api_key or os.getenv("GEMINI_API_KEY", "")).strip()
    if not key:
        return None, "Gemini API ключ не задан — введите его в Настройках"
    if _client and key == _configured_key:
        return _client, None
    _client = genai.Client(api_key=key)
    _configured_key = key
    return _client, None


def _parse_json(text: str) -> dict:
    """Extract JSON from Gemini response (may be wrapped in ```json ... ```)."""
    text = text.strip()
    m = re.search(r"```(?:json)?\s*([\s\S]*?)```", text)
    if m:
        text = m.group(1).strip()
    try:
        return json.loads(text)
    except Exception:
        return {}


async def _download_images(image_urls: list, ms_token: str = "") -> list:
    """Download images and return list of Gemini Part objects (bytes)."""
    if not _HAS_GEMINI or not image_urls:
        return []
    from google.genai import types
    headers = {"Authorization": f"Bearer {ms_token}"} if ms_token else {}
    parts = []
    async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
        for url in image_urls[:5]:
            try:
                r = await client.get(url, headers=headers)
                if r.status_code == 200:
                    ct = r.headers.get("content-type", "image/jpeg").split(";")[0].strip()
                    if ct not in ("image/jpeg", "image/png", "image/webp", "image/gif"):
                        ct = "image/jpeg"
                    parts.append(types.Part.from_bytes(data=r.content, mime_type=ct))
            except Exception:
                continue
    return parts


async def _ask(prompt: str, image_parts: list = None, api_key: str = "") -> tuple:
    """Send prompt (+ optional images) to Gemini. Returns (text, None) or (None, error_str).
    Retries on 503/overload with exponential backoff across all models.
    """
    client, err = _get_client(api_key)
    if not client:
        return None, err

    if _HAS_GEMINI and image_parts:
        from google.genai import types
        contents = image_parts + [types.Part.from_text(text=prompt)]
    else:
        contents = prompt

    last_err = ""
    for model in MODELS:
        for attempt in range(3):
            try:
                resp = await client.aio.models.generate_content(
                    model=model,
                    contents=contents,
                )
                return resp.text or "", None
            except Exception as e:
                msg = str(e)
                last_err = f"Gemini ошибка: {msg}"
                is_overload = (
                    "503" in msg or "UNAVAILABLE" in msg
                    or "overloaded" in msg.lower() or "high demand" in msg.lower()
                )
                is_rate_limit = (
                    "429" in msg or "RESOURCE_EXHAUSTED" in msg
                    or "quota" in msg.lower() or "rate limit" in msg.lower()
                )
                # Rate limit: skip to next model immediately (quota resets slowly)
                # Overload: retry same model with backoff (transient server issue)
                if is_rate_limit:
                    break
                if is_overload and attempt < 2:
                    await asyncio.sleep(2 ** attempt * 3)  # 3s, 6s
                    continue
                break

    return None, last_err


async def extract_bh_search_query(product_name: str, api_key: str = "") -> str:
    """Extract brand + model from product name to use as B&H search query."""
    prompt = (
        f"Extract the brand and model identifier from this Russian product name "
        f"for searching on bhphotovideo.com.\n"
        f"Product: \"{product_name}\"\n"
        f"Return ONLY the brand + model string, nothing else. "
        f"Example: for \"Осветитель Godox SL60W\" return \"Godox SL60W\"."
    )
    text, _ = await _ask(prompt, api_key=api_key)
    if not text:
        parts = product_name.strip().split()
        return " ".join(parts[1:3]) if len(parts) >= 3 else product_name
    return text.strip().strip('"').strip("'") or product_name


async def auto_enrich(
    product: dict,
    saved: dict,
    ym_categories: dict,
    ym_params: list,
    ozon_categories: dict,
    wb_categories: dict,
    bh_specs: dict,
    api_key: str = "",
) -> dict:
    """
    Main entry point: given product data + marketplace categories, returns:
    {
      "brand": str,
      "description": str,
      "ym_category_id": str | None,
      "ym_params": {"ym_{id}": value, ...},
      "ozon_category_key": str | None,
      "ozon_category_id": int | None,
      "ozon_type_id": int | None,
      "ozon_attrs": {"oz_{id}": value, ...},
      "wb_category_key": str | None,
      "wb_subject_id": int | None,
      "wb_chars": {"wb_{id}": value, ...},
      "bh_search_query": str,
      "overall_confidence": float,
      "error": str | None,
    }
    """
    client, err = _get_client(api_key)
    if not client:
        return {"error": err}

    name     = product.get("name", "")
    desc     = (product.get("description") or "")[:800]
    attrs    = product.get("attributes") or {}
    brand_ms = product.get("brand", "")

    ym_cats_list = _compact_cats(ym_categories, 80)
    oz_cats_list = _compact_cats(ozon_categories, 80)
    wb_cats_list = _compact_wb_cats(wb_categories, 80)

    ym_params_compact = []
    for p in (ym_params or [])[:60]:
        sp = {"id": p.get("id"), "name": p.get("name", ""), "required": p.get("required", False)}
        if p.get("unit"):
            sp["unit"] = p["unit"]
        vals = p.get("values") or p.get("dictionary") or []
        if vals:
            sp["allowed"] = [v.get("value") or v.get("name") for v in vals[:20] if isinstance(v, dict)]
        ym_params_compact.append(sp)

    payload = {
        "product_name":        name,
        "product_description": desc,
        "brand_from_erp":      brand_ms,
        "ms_attributes":       attrs,
        "bh_specs":            bh_specs or {},
        "ym_categories":       ym_cats_list,
        "ym_params":           ym_params_compact,
        "ozon_categories":     oz_cats_list,
        "wb_categories":       wb_cats_list,
    }

    system = """You are an expert product catalog manager for Russian marketplaces.
Given product data, determine the best matching categories and fill characteristics.

Return strict JSON with exactly these fields:
{
  "brand": "Brand name extracted from product name, or empty string",
  "description": "Natural Russian product description 150-500 chars, specific to this product",
  "bh_search_query": "brand + model for searching on bhphotovideo.com (in English)",
  "ym_category_id": "string key from ym_categories, or null",
  "ym_params": {"ym_<id>": "value", ...},
  "ozon_category_key": "key from ozon_categories, or null",
  "wb_category_key": "key from wb_categories (must be a leaf s_* key), or null",
  "ozon_attrs": {"oz_<id>": "value", ...},
  "wb_chars": {"wb_<id>": "value", ...},
  "overall_confidence": 0.0
}

Rules:
- ym_params key = "ym_" + param id. Only fill params you are confident about.
  For params with allowed values, use exact string from the allowed list.
- ozon_attrs key = "oz_" + attribute id. Leave empty dict if no ozon_categories provided.
- wb_chars key = "wb_" + characteristic id. Leave empty dict if no wb_categories provided.
- wb_category_key must be a leaf (s_* key).
- overall_confidence: 0.0 to 1.0

BRAND RULES (very important):
- The store sells camera accessories and AV equipment from niche/specialist brands.
- Famous consumer brands like Sony, Canon, Nikon, Fujifilm, DJI, Panasonic, GoPro, Apple, Samsung
  often appear in the product NAME as compatibility info (e.g. "case for Sony a7"),
  but the ACTUAL product brand is the accessory manufacturer (SmallRig, Portkeys, Tilta, etc.).
- Extract the TRUE manufacturer brand from the product name — the company that makes the accessory,
  NOT the camera/device brand it is compatible with.
- If brand_from_erp contains a well-known consumer brand (Sony, Canon, DJI, etc.) but the
  product name shows a different accessory brand — use the accessory brand, ignore brand_from_erp.
- Examples: "SmallRig case for Fujifilm X100VI" → brand=SmallRig (not Fujifilm)
            "Portkeys cable for Sony cameras" → brand=Portkeys (not Sony)
            "Kingma adapter for Canon LP-E5" → brand=Kingma (not Canon)

BATTERY / ACCUMULATOR RULES:
- If the product is a battery, accumulator, or power bank:
  - Set oz attr "Класс опасности товара" = "Класс 9. Прочие опасные вещества и изделия"
    (or the closest Класс 9 option from allowed values)
  - Set oz attr "Химический тип" = the lithium type matching the product
    (LiIon, LiFePo4, etc. — pick from allowed values)

PACKAGING RULE:
- If the product's B&H specs or description mentions a box/carton packaging,
  set oz attr "Упаковка" = "Коробка" (or closest cardboard/box option from allowed values).

USE YOUR OWN KNOWLEDGE:
- When bh_specs is empty, rely on your training knowledge about this specific product model.
- Camera accessory model numbers encode key specs — decode them:
  Battery codes: NP-FZ100 (Sony, 7.2V, ~2280mAh stock/up to 2600mAh 3rd party),
  LP-E6/LP-E6N (Canon, 7.2V, ~1865mAh stock), EN-EL15/EN-EL15c (Nikon, 7.0V),
  NP-W235 (Fujifilm, 7.2V), DMW-BLK22 (Panasonic, 7.2V), NP-BX1 (Sony, 3.6V).
- Write "description" that includes actual product specs from your knowledge — voltage,
  capacity, compatibility, materials, dimensions — specific to this exact model."""

    prompt = system + "\n\nProduct data:\n" + json.dumps(payload, ensure_ascii=False, indent=2)

    raw, ask_err = await _ask(prompt, api_key=api_key)
    if not raw:
        return {"error": ask_err or "Пустой ответ от Gemini"}

    data = _parse_json(raw)
    if not data:
        return {"error": f"Не удалось разобрать ответ Gemini: {raw[:300]}"}

    result = {
        "brand":            (data.get("brand") or "").strip(),
        "description":      (data.get("description") or "").strip(),
        "bh_search_query":  (data.get("bh_search_query") or name).strip(),
        "ym_category_id":   data.get("ym_category_id") or None,
        "ym_params":        _clean_dict(data.get("ym_params")),
        "ozon_category_key": data.get("ozon_category_key") or None,
        "ozon_attrs":       _clean_dict(data.get("ozon_attrs")),
        "wb_category_key":  data.get("wb_category_key") or None,
        "wb_chars":         _clean_dict(data.get("wb_chars")),
        "overall_confidence": float(data.get("overall_confidence") or 0.0),
    }

    if result["ozon_category_key"] and ozon_categories:
        entry = ozon_categories.get(result["ozon_category_key"])
        if entry:
            result["ozon_category_id"] = entry.get("desc_cat_id")
            result["ozon_type_id"]     = entry.get("type_id")

    if result["wb_category_key"] and wb_categories:
        entry = wb_categories.get(result["wb_category_key"])
        if entry:
            result["wb_subject_id"]    = entry.get("int_id")
            result["wb_category_name"] = entry.get("name", "")

    return result


async def suggest_category(
    mp: str,
    name: str,
    description: str,
    brand: str,
    categories: dict,
    api_key: str = "",
    image_urls: list = None,
    ms_token: str = "",
    bh_specs: dict = None,
) -> dict:
    """
    Suggest the best category for a product using multimodal Gemini.
    Sends product images + B&H specs for accurate matching.
    """
    client, err = _get_client(api_key)
    if not client:
        return {"ok": False, "error": err}

    # Download product images for multimodal request
    image_parts = []
    if image_urls:
        image_parts = await _download_images(image_urls, ms_token)

    # For WB — only leaf categories (s_*)
    if mp == "wb":
        cats_list = _compact_wb_cats(categories, 120)
    else:
        cats_list = _compact_cats_leaves(categories, 120)

    bh_block = ""
    if bh_specs:
        specs_text = "\n".join(f"  {k}: {v}" for k, v in list(bh_specs.items())[:30])
        bh_block = f"\nB&H Photo specs:\n{specs_text}\n"

    images_note = f"(смотри {len(image_parts)} фото товара выше)" if image_parts else "(фото не предоставлено)"

    prompt = (
        f"Ты эксперт по товарам фото/видео оборудования.\n"
        f"Определи наиболее подходящую категорию маркетплейса для товара.\n\n"
        f"Товар: {name}\n"
        f"Бренд: {brand}\n"
        f"Описание: {description[:400]}\n"
        f"Фотографии товара: {images_note}\n"
        f"{bh_block}\n"
        f"Маркетплейс: {mp.upper()}\n\n"
        f"Список категорий (выбирай только из этого списка):\n"
        f"{json.dumps(cats_list, ensure_ascii=False, indent=2)}\n\n"
        f"ВАЖНО: верни ТОЛЬКО валидный JSON без markdown-блоков:\n"
        f"{{\"category_id\": \"<точный id из списка>\", \"confidence\": 0.95, \"reason\": \"...\"}}\n"
        f"category_id должен быть ТОЧНО скопирован из поля 'id' в списке категорий."
    )

    raw, ask_err = await _ask(prompt, image_parts=image_parts, api_key=api_key)
    if not raw:
        return {"ok": False, "error": ask_err or "Пустой ответ Gemini"}

    data = _parse_json(raw)
    cat_id = str(data.get("category_id") or "").strip()

    # Validate that returned category_id actually exists in our dict
    if cat_id and cat_id not in categories:
        # Try to find closest match by string
        for key in categories:
            if str(key) == cat_id or str(categories[key].get("int_id", "")) == cat_id:
                cat_id = key
                break
        else:
            cat_id = ""

    if not cat_id:
        return {"ok": False, "error": f"Категория не найдена в ответе Gemini. Ответ: {raw[:300]}"}

    cat = categories[cat_id]
    return {
        "ok":          True,
        "category_id": cat_id,
        "path":        cat.get("path") or [cat.get("name", "")],
        "category_name": cat.get("name", ""),
        "confidence":  float(data.get("confidence", 0.5)),
        "reason":      data.get("reason", ""),
    }


# ─── Helpers ──────────────────────────────────────────────────

def _compact_cats(cats: dict, limit: int) -> list:
    if not cats:
        return []
    result = []
    for k, v in list(cats.items())[:limit]:
        result.append({
            "id":   k,
            "name": v.get("name", ""),
            "path": " > ".join(v.get("path") or [v.get("name", "")]),
        })
    return result


def _compact_cats_leaves(cats: dict, limit: int) -> list:
    """Return only leaf categories (no children) — avoids Gemini picking parent nodes."""
    if not cats:
        return []
    result = []
    for k, v in cats.items():
        if not v.get("has_children", True):  # is_leaf or no children flag
            result.append({
                "id":   k,
                "name": v.get("name", ""),
                "path": " > ".join(v.get("path") or [v.get("name", "")]),
            })
            if len(result) >= limit:
                break
    # Fallback: if all categories have children (no leaf flag), just return all
    if not result:
        return _compact_cats(cats, limit)
    return result


def _compact_wb_cats(cats: dict, limit: int) -> list:
    if not cats:
        return []
    result = []
    for k, v in cats.items():
        if k.startswith("s_") and len(result) < limit:
            result.append({
                "id":   k,
                "name": v.get("name", ""),
                "path": " > ".join(v.get("path") or [v.get("name", "")]),
            })
    return result


def _clean_dict(d) -> dict:
    if not isinstance(d, dict):
        return {}
    return {str(k): v for k, v in d.items() if v not in (None, "", [], {})}


async def enrich_category_params(
    product: dict,
    marketplace: str,
    attributes: list,
    bh_specs: dict = None,
    extracted_specs: dict = None,
    api_key: str = "",
) -> dict:
    """Second-pass AI enrichment: fill as many category attributes as possible.

    Returns {"params": {"ym_123": "value", ...}, "filled": N, "error": None | str}
    For is_collection dict attributes, value is a list: {"oz_123": ["val1", "val2"]}
    """
    client, err = _get_client(api_key)
    if not client:
        return {"params": {}, "filled": 0, "error": err}

    if not attributes:
        return {"params": {}, "filled": 0, "error": "empty attribute list"}

    prefix = {"ym": "ym_", "ozon": "oz_", "wb": "wb_"}
    param_key = prefix.get(marketplace, "ym_")

    # Build compact attribute list — include is_collection + capped allowed values
    # Cap allowed_values to avoid enormous prompts (attributes like "Бренд" can have 10k+ values)
    MAX_VALS = 120
    compact_attrs = []
    for a in attributes[:150]:
        pid = a.get("id")
        if not pid:
            continue
        is_coll = bool(a.get("is_collection", False))
        rec = {
            "id":            pid,
            "name":          a.get("name", ""),
            "required":      bool(a.get("required", False)),
            "is_collection": is_coll,
        }
        if a.get("unit"):
            rec["unit"] = a["unit"]
        vals = a.get("values") or a.get("dictionary") or []
        if vals and isinstance(vals, list):
            av = [v.get("value") or v.get("name") for v in vals[:MAX_VALS] if isinstance(v, dict)]
            av = [x for x in av if x]
            if av:
                rec["allowed_values"] = av
        compact_attrs.append(rec)

    name      = product.get("name", "")
    desc      = (product.get("description") or "")[:3000]
    ms_attrs  = product.get("attributes") or {}
    weight_kg = product.get("weight_kg", 0)
    dims_cm   = product.get("dims_cm") or {}

    from spec_extractor import format_for_ai
    extracted_block = format_for_ai(extracted_specs or {})

    bh_block = ""
    if bh_specs:
        bh_block = "\n".join(f"  {k}: {v}" for k, v in list(bh_specs.items())[:50])

    # Build the prompt in a more structured way
    attrs_json = json.dumps(compact_attrs, ensure_ascii=False)
    product_block = (
        f"Название: {name}\n"
        f"Описание: {desc}\n"
        f"Атрибуты МС: {json.dumps(ms_attrs, ensure_ascii=False)}\n"
        f"Вес (кг): {weight_kg}  Габариты: {json.dumps(dims_cm, ensure_ascii=False)}\n"
        f"B&H спеки:\n{bh_block or '  нет'}\n"
        f"Извлечённые спеки: {extracted_block or 'нет'}"
    )

    prompt = f"""Ты заполняешь характеристики товара на маркетплейсе {marketplace.upper()} для магазина фото/видео оборудования FotoToad.

ЗАДАЧА: заполнить МАКСИМАЛЬНО ВОЗМОЖНОЕ количество характеристик из списка ниже.
Рейтинг карточки товара напрямую зависит от количества заполненных характеристик.
Заполняй ВСЁ что можно определить, даже с минимальной уверенностью.

═══ ДАННЫЕ ТОВАРА ═══
{product_block}

═══ СПИСОК ХАРАКТЕРИСТИК ДЛЯ ЗАПОЛНЕНИЯ ═══
{attrs_json}

═══ ПРАВИЛА ═══

ФОРМАТ ОТВЕТА:
- Ключ: всегда "{param_key}<id>" (например "{param_key}123")
- Для атрибута с "is_collection": false → значение: строка "значение"
- Для атрибута с "is_collection": true → значение: МАССИВ ["значение1", "значение2"]
  Выбери ВСЕ подходящие значения из allowed_values!

ВЫБОР ИЗ СПРАВОЧНИКА (allowed_values):
- Если есть allowed_values: ОБЯЗАТЕЛЬНО используй ТОЧНУЮ строку из этого списка
- Не придумывай значения — только из allowed_values
- Для is_collection: выбери все подходящие (может быть 1, 2, 3 и более)

АГРЕССИВНОЕ ЗАПОЛНЕНИЕ (заполняй всегда):
- Бренд → производитель аксессуара (SmallRig/Tilta/Portkeys/Kingma/Sennheiser и т.п.)
  ВНИМАНИЕ: Sony/Canon/Nikon/Fujifilm/DJI в названии = совместимость, НЕ бренд!
- Страна производства → "Китай" для аксессуаров (если не указано иное)
- Упаковка → "Коробка" или аналог из allowed_values
- Тип источника света → "LED" для LED осветителей
- Гарантийный срок → "12 месяцев" если не указано иное

АККУМУЛЯТОРЫ И БАТАРЕИ:
- Класс опасности → "Класс 9..." из allowed_values (опасные вещества)
- Химический тип → Li-Ion/Литий-ионный из allowed_values
- Напряжение: NP-FZ100=7.2В, LP-E6N=7.2В, EN-EL15=7.0В, NP-W235=7.2В, NP-BX1=3.6В
- Ёмкость: читай из названия (2600mAh → 2600) или из описания
- ТН ВЭД (код товарной номенклатуры): для аккумуляторов Li-Ion → "8507 60 000 0"

ИСПОЛЬЗУЙ СВОИ ЗНАНИЯ:
- Ты знаешь характеристики популярных фото-аксессуаров — используй эти знания
- Расшифровывай коды моделей: размеры, тип крепления, совместимость
- Заполняй числовые поля (вес, размеры, ёмкость) из описания или своих знаний

ЧИСЛОВЫЕ АТРИБУТЫ:
- Только число без единиц (например "7.2" не "7.2В", "2600" не "2600 mAh")

═══ ВЕРНИ ТОЛЬКО JSON ═══
Формат: {{"{param_key}<id>": "значение", "{param_key}<id>": ["значение1", "значение2"], ...}}
Заполни ВСЕ возможные характеристики. Пустые строки и null НЕ включай."""

    raw, ask_err = await _ask(prompt, api_key=api_key)
    if not raw:
        return {"params": {}, "filled": 0, "error": ask_err or "Gemini вернул пустой ответ"}

    data = _parse_json(raw)
    if not data:
        # Return raw snippet so caller can show it in diagnostics
        return {"params": {}, "filled": 0, "error": f"Не удалось разобрать JSON от Gemini: {raw[:400]}"}

    # Build is_collection lookup for validation
    coll_ids = {str(a.get("id")) for a in attributes if a.get("is_collection")}

    params = {}
    for k, v in data.items():
        if not k.startswith(param_key):
            continue
        if v in (None, "", [], {}):
            continue
        attr_id = k[len(param_key):]
        if isinstance(v, list):
            clean = [str(x) for x in v if x not in (None, "")]
            if clean:
                params[k] = clean if attr_id in coll_ids else clean[0]
        else:
            params[k] = str(v)

    # Diagnostic: how many raw keys did Gemini return vs how many passed the filter
    raw_count = len([k for k in data if k.startswith(param_key)])
    return {
        "params":    params,
        "filled":    len(params),
        "error":     None,
        "_debug":    {"raw_keys": raw_count, "passed": len(params), "prompt_attrs": len(compact_attrs)},
    }
