# backend/main.py — MarketSync (МойСклад → Яндекс.Маркет + Ozon + WB)
from fastapi import FastAPI, UploadFile, File, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from typing import List, Optional, Dict, Any
import asyncio, json, os, io, re
import httpx as _httpx
from pathlib import Path
from dotenv import load_dotenv

try:
    from PIL import Image as _PILImage
    _HAS_PILLOW = True
except ImportError:
    _HAS_PILLOW = False

load_dotenv()

from moysklad import MoySkladClient, detect_category
from yandex_market import (
    YandexMarketClient, get_ym_categories,
    flatten_categories_tree, reload_categories,
    filter_subtree, merge_categories,
    get_category_parameters_cached,
)
from ozon import OzonClient
from wb import WildberriesClient, extract_wb_price
from bh_playwright import get_bh_data
from ai_matcher import ai_enrich_product
from ai_gemini import auto_enrich as gemini_auto_enrich, suggest_category as gemini_suggest_category
import video_builder
from marketplaces import fill_card_attributes, MarketplaceAPIError, AttributeMappingError
from marketplaces.models import FillRequest, FillResponse, RatingResult as _RatingResult

PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "").strip().rstrip("/")

_BH_CACHE:       Dict[str, Dict[str, Any]] = {}
_AI_ENRICH_CACHE: Dict[str, Dict[str, Any]] = {}
_LOW_RATED_FILE      = os.path.join(os.path.dirname(__file__), "low_rated_offers.json")
_LOW_RATED_THRESHOLD = 80
_YM_CRITICAL_MISSING = {"Название", "Цена", "Изображения"}
_ERRORS_FILE         = os.path.join(os.path.dirname(__file__), "product_errors.json")
_WB_CATS_FILE        = os.path.join(os.path.dirname(__file__), "wb_categories.json")
_OZON_CATS_FILE      = os.path.join(os.path.dirname(__file__), "ozon_categories.json")
_OZ_ATTRS_CACHE      = os.path.join(os.path.dirname(__file__), "ozon_category_attrs_cache.json")
_OZ_ATTR_VALS_CACHE  = os.path.join(os.path.dirname(__file__), "ozon_attr_values_cache.json")

app = FastAPI(title="MarketSync")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

FRONTEND = os.path.join(os.path.dirname(__file__), "..", "frontend")

# ─── Pydantic models ──────────────────────────────────────────

class AllKeys(BaseModel):
    ms_token:        str = os.getenv("MS_TOKEN", "")
    ym_api_key:      str = os.getenv("YM_API_KEY", "")
    ym_campaign_id:  str = os.getenv("YM_CAMPAIGN_ID", "")
    ym_business_id:  Optional[str] = os.getenv("YM_BUSINESS_ID", "")
    ozon_client_id:  str = os.getenv("OZON_CLIENT_ID", "")
    ozon_api_key:    str = os.getenv("OZON_API_KEY", "")
    wb_api_key:      str = os.getenv("WB_API_KEY", "")
    gemini_api_key:  str = os.getenv("GEMINI_API_KEY", "")

class YMKeys(BaseModel):
    ms_token:       str = os.getenv("MS_TOKEN", "")
    ym_api_key:     str = os.getenv("YM_API_KEY", "")
    ym_campaign_id: str = os.getenv("YM_CAMPAIGN_ID", "")
    ym_business_id: Optional[str] = os.getenv("YM_BUSINESS_ID", "")

class OzonKeys(BaseModel):
    ms_token:       str = os.getenv("MS_TOKEN", "")
    ozon_client_id: str = os.getenv("OZON_CLIENT_ID", "")
    ozon_api_key:   str = os.getenv("OZON_API_KEY", "")

class WBKeys(BaseModel):
    ms_token:   str = os.getenv("MS_TOKEN", "")
    wb_api_key: str = os.getenv("WB_API_KEY", "")

class WBUploadRequest(WBKeys):
    codes:        List[str]
    dry_run:      bool = True
    force_update: bool = False

class YMCodeRequest(YMKeys):
    code: str

class YMPreviewRequest(YMKeys):
    codes: List[str]

class YMUploadRequest(YMKeys):
    codes: List[str]
    dry_run: bool = True
    resolved: Optional[Dict[str, Dict[str, Any]]] = {}

class OzonUploadRequest(OzonKeys):
    codes: List[str]
    dry_run: bool = True
    force_update: bool = False

class SaveAnswersRequest(BaseModel):
    code: str
    answers: Dict[str, Any]

_ANSWERS_FILE = os.path.join(os.path.dirname(__file__), "answers.json")

def _load_answers_from_disk() -> Dict[str, Dict[str, Any]]:
    try:
        with open(_ANSWERS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}

def _save_answers_to_disk(store: Dict[str, Dict[str, Any]]) -> None:
    try:
        with open(_ANSWERS_FILE, "w", encoding="utf-8") as f:
            json.dump(store, f, ensure_ascii=False, indent=2)
    except Exception:
        pass

answers_store: Dict[str, Dict[str, Any]] = _load_answers_from_disk()

# ─── Config (expose env keys to frontend) ────────────────────

@app.get("/api/config")
async def get_config():
    return {
        "ms_token":        os.getenv("MS_TOKEN", ""),
        "ym_api_key":      os.getenv("YM_API_KEY", ""),
        "ym_campaign_id":  os.getenv("YM_CAMPAIGN_ID", ""),
        "ym_business_id":  os.getenv("YM_BUSINESS_ID", ""),
        "ozon_client_id":  os.getenv("OZON_CLIENT_ID", ""),
        "ozon_api_key":    os.getenv("OZON_API_KEY", ""),
        "wb_api_key":      os.getenv("WB_API_KEY", ""),
        "gemini_api_key":  os.getenv("GEMINI_API_KEY", ""),
    }

# ─── Root ─────────────────────────────────────────────────────

@app.get("/")
async def root():
    f = os.path.join(FRONTEND, "index.html")
    return FileResponse(f) if os.path.exists(f) else {"ok": True, "status": "backend running"}

# ─── Test connections ──────────────────────────────────────────

@app.post("/api/test/all")
async def test_all(k: AllKeys):
    ms = MoySkladClient(k.ms_token)
    ym = YandexMarketClient(k.ym_api_key, k.ym_campaign_id, k.ym_business_id)
    oz = OzonClient(k.ozon_client_id, k.ozon_api_key)
    wb = WildberriesClient(k.wb_api_key) if k.wb_api_key else None
    tasks = [ms.test(), ym.test(), oz.test()]
    if wb:
        tasks.append(wb.test())
    results = await asyncio.gather(*tasks)
    ms_r, ym_r, oz_r = results[0], results[1], results[2]
    wb_r = results[3] if wb else {"ok": False, "message": "ключ не задан"}
    return {"ms": ms_r, "ym": ym_r, "ozon": oz_r, "wb": wb_r,
            "ok": ms_r.get("ok") and (ym_r.get("ok") or oz_r.get("ok") or wb_r.get("ok"))}

@app.post("/api/test/ym")
async def test_ym(k: YMKeys):
    ms = MoySkladClient(k.ms_token)
    ym = YandexMarketClient(k.ym_api_key, k.ym_campaign_id, k.ym_business_id)
    ms_r, ym_r = await asyncio.gather(ms.test(), ym.test())
    return {"ms": ms_r, "ym": ym_r, "ok": ms_r.get("ok") and ym_r.get("ok")}

@app.post("/api/test/ozon")
async def test_ozon(k: OzonKeys):
    ms = MoySkladClient(k.ms_token)
    oz = OzonClient(k.ozon_client_id, k.ozon_api_key)
    ms_r, oz_r = await asyncio.gather(ms.test(), oz.test())
    return {"ms": ms_r, "ozon": oz_r, "ok": ms_r.get("ok") and oz_r.get("ok")}

# ─── Answers ───────────────────────────────────────────────────

@app.post("/api/answers/save")
async def save_answers(req: SaveAnswersRequest):
    if req.code not in answers_store:
        answers_store[req.code] = {}
    answers_store[req.code].update(req.answers)
    _save_answers_to_disk(answers_store)
    return {"ok": True, "saved": answers_store[req.code]}

@app.get("/api/answers/{code}")
async def get_answers(code: str):
    return {"ok": True, "answers": answers_store.get(code, {})}

@app.get("/api/answers")
async def get_all_answers():
    return {"ok": True, "store": answers_store}

# ─── Card Autofill ────────────────────────────────────────────

@app.post("/api/card/fill", response_model=FillResponse)
async def card_fill(req: FillRequest):
    """Auto-fill marketplace card attributes from MoySklad data.

    Returns updated_fields keyed as 'ym_{param_id}' for Yandex and 'oz_{attr_id}' for Ozon.
    Frontend saves these into answers_store under 'params_values' (YM) or 'ozon_attrs' (Ozon).
    """
    ms = MoySkladClient(req.ms_token)
    ozon_client = None
    if req.marketplace == "ozon" and req.ozon_client_id and req.ozon_api_key:
        ozon_client = OzonClient(req.ozon_client_id, req.ozon_api_key)

    try:
        return await fill_card_attributes(
            code=req.code,
            marketplace=req.marketplace,
            category_id=req.category_id,
            ms_client=ms,
            ozon_client=ozon_client,
        )
    except MarketplaceAPIError as e:
        return FillResponse(
            status="error",
            marketplace=req.marketplace,
            category_id=req.category_id,
            rating=0.0,
            rating_result=_RatingResult(
                score=0.0, missing_mandatory=[], recommendations=[], status="low", details=[]
            ),
            updated_fields={},
            warnings=[],
            errors=[str(e)],
        )
    except AttributeMappingError as e:
        return FillResponse(
            status="partial",
            marketplace=req.marketplace,
            category_id=req.category_id,
            rating=0.0,
            rating_result=_RatingResult(
                score=0.0, missing_mandatory=[], recommendations=[], status="low", details=[]
            ),
            updated_fields={},
            warnings=[str(e)],
            errors=[],
        )

# ─── YM: load single product ──────────────────────────────────

@app.post("/api/ym/product")
async def ym_get_product(req: YMCodeRequest):
    ms = MoySkladClient(req.ms_token)
    ym = YandexMarketClient(req.ym_api_key, req.ym_campaign_id, req.ym_business_id)

    product = await ms.get_product_data(req.code)
    if not product["ok"]:
        return product

    cat, cat_confidence = await detect_category(product["name"], product["attributes"])
    saved = answers_store.get(req.code, {})

    questions = ym.get_questions(product, cat)
    for q in questions:
        if q["id"] in saved:
            q["value"] = saved[q["id"]]
        elif q["id"] == "params" and "params_values" in saved:
            q["saved_values"] = saved["params_values"]

    offer_id = product.get("article") or product.get("code")
    ym_exists = await ym.check_exists(offer_id)

    bh_data = await _enrich_from_bh(req.code, product, cat, ym)
    bh_specs = (bh_data or {}).get("specs") if bh_data else None
    ai_enrich = await _ai_enrich(req.code, product, cat, ym, bh_specs)

    resolved = dict(saved)
    if ai_enrich:
        if not resolved.get("brand") and ai_enrich.get("brand"):
            resolved["brand"] = ai_enrich["brand"]
        if not resolved.get("description") and ai_enrich.get("description"):
            resolved["description"] = ai_enrich["description"]
        pv = dict(resolved.get("params_values", {}) or {})
        for m in ai_enrich.get("parameter_values", []) or []:
            key = f"ym_{m['param_id']}"
            if key not in pv:
                pv[key] = m["value"]
        if pv:
            resolved["params_values"] = pv

    offer_preview = ym.build_offer(product, cat, resolved)

    category_params_inline = None
    if cat:
        try:
            category_params_inline = await _build_category_params_data(ym, cat, req.code)
        except Exception:
            pass

    return {
        "ok": True,
        "product": product,
        "category": cat,
        "category_confidence": cat_confidence,
        "category_name": get_ym_categories().get(cat, {}).get("name", cat),
        "questions": questions,
        "offer_preview": offer_preview,
        "ym_exists": ym_exists,
        "has_saved_answers": len(saved) > 0,
        "bh_data": bh_data,
        "ai_enrich": ai_enrich,
        "resolved_auto": resolved,
        "category_params": category_params_inline,
    }

# ─── YM: preview list ─────────────────────────────────────────

@app.post("/api/ym/preview")
async def ym_preview(req: YMPreviewRequest):
    ms = MoySkladClient(req.ms_token)
    ym = YandexMarketClient(req.ym_api_key, req.ym_campaign_id, req.ym_business_id)
    results = []
    for code in [c.strip() for c in req.codes if c.strip()]:
        p = await ms.get_product_data(code)
        if not p["ok"]:
            results.append({"code": code, "error": p["error"], "action": "SKIP"})
            continue
        cat, _ = await detect_category(p["name"], p["attributes"])
        saved = answers_store.get(code, {})
        # Try article AND code so we find offers regardless of which was used as offerId
        candidates = list(dict.fromkeys(
            c for c in [p.get("article") or "", code] if c.strip()
        ))
        ym_state = await ym.validate_offer_state(candidates)
        exists   = ym_state.get("exists", False)
        ym_rating    = ym_state.get("rating")       # real YM content rating
        ym_offer_id  = ym_state.get("offer_id", "") if exists else ""
        ym_cat_id    = ym_state.get("ym_category_id")
        questions = ym.get_questions(p, cat)
        unanswered = [q for q in questions
                      if q.get("required") and not saved.get(q["id"]) and not q.get("auto_detected")]
        results.append({
            "code": code,
            "article": p.get("article", ""),
            "name": p["name"],
            "brand": saved.get("brand") or p.get("brand", ""),
            "price_ym": p["price_ym"],
            "price_ym_source": p["price_ym_source"],
            "images_count": p["images_count"],
            "has_description": p["has_description"],
            "has_dims": p["has_dims"],
            "category": saved.get("category") or cat,
            "ym_exists": exists,
            "ym_offer_id": ym_offer_id,
            "ym_rating": ym_rating,
            "ym_category_id": ym_cat_id,
            "action": "UPDATE" if exists else "UPLOAD",
            "has_saved_answers": len(saved) > 0,
            "unanswered_required": [q["label"] for q in unanswered],
            "warnings": _ym_warnings(p, saved),
        })
        await asyncio.sleep(0.2)
    up  = sum(1 for r in results if r.get("action") == "UPLOAD")
    upd = sum(1 for r in results if r.get("action") == "UPDATE")
    err = sum(1 for r in results if "error" in r)
    return {"results": results, "summary": {"total": len(results), "upload": up, "update": upd, "error": err}}

# ─── YM: categories ───────────────────────────────────────────

class CategoriesRefreshRequest(YMKeys):
    scope: str = "photo"

@app.post("/api/ym/categories/refresh")
async def ym_categories_refresh(req: CategoriesRefreshRequest):
    ym = YandexMarketClient(req.ym_api_key, req.ym_campaign_id, req.ym_business_id)
    result = await ym.get_categories_tree()
    if not result.get("ok"):
        return result
    full = flatten_categories_tree(result["data"])
    if not full:
        return {"ok": False, "error": "API вернул пустое дерево категорий"}
    scope = (req.scope or "photo").strip()
    if scope == "all":
        reload_categories(full)
        return {"ok": True, "count": len(full), "mode": "all", "message": f"Загружено {len(full)} категорий"}
    if scope.startswith("add:"):
        branch_name = scope[4:].strip()
        if not branch_name:
            return {"ok": False, "error": "Не указано имя ветки"}
        subset = filter_subtree(full, branch_name)
        if not subset:
            return {"ok": False, "error": f"Ветка '{branch_name}' не найдена"}
        merged = merge_categories(subset)
        return {"ok": True, "added": len(subset), "total": len(merged), "mode": "add", "branch": branch_name}
    subset = filter_subtree(full, "Фото и видеокамеры")
    if not subset:
        reload_categories(full)
        return {"ok": True, "count": len(full), "mode": "all-fallback"}
    reload_categories(subset)
    return {"ok": True, "count": len(subset), "mode": "photo", "message": f"Загружено {len(subset)} категорий"}

@app.get("/api/ym/categories")
async def ym_list_categories():
    cats = get_ym_categories()
    tree_ok = any(c.get("parent_id") for c in cats.values()) if cats else False
    return {"ok": True, "count": len(cats), "tree_ok": tree_ok, "categories": cats}

_GROUP_META = {
    "MAIN":       {"title": "Ключевые характеристики",            "score_max": 12},
    "FILTERABLE": {"title": "Дополнительные характеристики для фильтров", "score_max": 8},
    "ADDITIONAL": {"title": "Подробности о товаре",               "score_max": 5},
    "OTHER":      {"title": "Прочее",                             "score_max": 0},
}

class CategoryParamsRequest(YMKeys):
    code: Optional[str] = None

@app.post("/api/ym/category/{category_id}/params")
async def ym_category_params(category_id: str, req: CategoryParamsRequest):
    ym = YandexMarketClient(req.ym_api_key, req.ym_campaign_id, req.ym_business_id)
    try:
        result = await _build_category_params_data(ym, str(category_id), (req.code or "").strip())
    except Exception as e:
        return {"ok": False, "error": f"get_category_parameters: {e}"}
    if not result:
        return {"ok": False, "error": "Категория не найдена или не содержит параметров"}
    return result

# ─── YM: upload SSE ───────────────────────────────────────────

@app.post("/api/ym/upload/stream")
async def ym_upload_stream(req: YMUploadRequest, request: Request):
    codes = [c.strip() for c in req.codes if c.strip()]
    _req_base = _req_base_url(request)

    async def generate():
        ms = MoySkladClient(req.ms_token)
        ym = YandexMarketClient(req.ym_api_key, req.ym_campaign_id, req.ym_business_id)
        total = len(codes)
        done = ok_count = err_count = 0

        def evt(type_: str, **kw):
            return f"data: {json.dumps({'type': type_, **kw}, ensure_ascii=False)}\n\n"

        yield evt("start", total=total, dry_run=req.dry_run, mode="DRY RUN" if req.dry_run else "LIVE")

        for i, code in enumerate(codes):
            yield evt("progress", current=i+1, total=total, code=code,
                      percent=int(i/total*100), status="reading")
            yield evt("log", level="info", msg=f"[{i+1}/{total}] Код: {code} → читаем из МойСклад...")

            product = await ms.get_product_data(code)
            if not product["ok"]:
                yield evt("log", level="error", msg=f"  ✗ {product['error']}")
                yield evt("item", code=code, success=False, error=product["error"])
                err_count += 1; done += 1
                yield evt("progress", current=done, total=total, code=code,
                          percent=int(done/total*100), status="error")
                continue

            name  = product["name"]
            price = product["price_ym"]
            imgs  = product["images_count"]
            yield evt("log", level="info", msg=f"  ✓ Найден: «{name}»")
            yield evt("log", level="info",
                      msg=f"    Цена ({product['price_ym_source']}): {price:,.0f} ₽  |  Фото: {imgs}")

            saved = {**answers_store.get(code, {}), **(req.resolved or {}).get(code, {})}
            for w in _ym_warnings(product, saved):
                yield evt("log", level="warn", msg=f"  ⚠ {w}")

            _cat_detected, _ = await detect_category(name, product["attributes"])
            cat = saved.get("category") or _cat_detected
            cat_name = get_ym_categories().get(cat, {}).get("name", cat)
            yield evt("log", level="info", msg=f"  Категория ЯМ: {cat_name}")

            bh_data  = await _enrich_from_bh(code, product, cat, ym)
            bh_specs = (bh_data or {}).get("specs") if bh_data else None
            ai_e     = await _ai_enrich(code, product, cat, ym, bh_specs)
            if ai_e and not ai_e.get("error"):
                if not saved.get("brand") and ai_e.get("brand"):
                    saved["brand"] = ai_e["brand"]
                if not saved.get("description") and ai_e.get("description"):
                    saved["description"] = ai_e["description"]
                pv = dict(saved.get("params_values", {}) or {})
                for m in ai_e.get("parameter_values", []) or []:
                    pv.setdefault(f"ym_{m['param_id']}", m["value"])
                if pv:
                    saved["params_values"] = pv
                yield evt("log", level="info",
                          msg=f"  AI: бренд={'✓' if ai_e.get('brand') else '—'} "
                              f"описание={'✓' if ai_e.get('description') else '—'} "
                              f"параметров={len(ai_e.get('parameter_values',[]))}")

            brand = saved.get("brand") or product.get("brand", "")
            yield evt("log", level="info", msg=f"  Бренд: {brand or '(не определён)'}")

            dims = product.get("dims_cm", {})
            if dims.get("width_cm"):
                yield evt("log", level="info",
                          msg=f"  Габариты: {dims['width_cm']}×{dims['height_cm']}×{dims['depth_cm']} см")
            else:
                yield evt("log", level="warn", msg="  Габариты не заданы в МС")

            if req.dry_run:
                offer = ym.build_offer(product, cat, saved)
                yield evt("log", level="info",
                          msg=f"  → [DRY RUN] offerId: {offer['offerId']} | категория: {cat_name}")
                yield evt("log", level="info", msg=f"  → Название: {offer['name'][:80]}")
                yield evt("log", level="info",
                          msg=f"  → Цена: {offer.get('basicPrice',{}).get('value',0):,} ₽ "
                              f"| Фото: {len(offer.get('pictures',[]))} "
                              f"| Параметров: {len(offer.get('parameterValues',[]))}")
                yield evt("item", code=code, success=True, dry_run=True,
                          name=name, price=price, category=cat_name,
                          article=product.get("article",""))
                ok_count += 1
            else:
                yield evt("progress", current=i+1, total=total, code=code,
                          percent=int((i+0.4)/total*100), status="processing")
                yield evt("log", level="info", msg="  → Подготавливаем фотографии...")
                ym_photos, img_warns = await _prepare_images(
                    req.ms_token, product, saved, code, subfolder="ym_proc", border_pct=0,
                    base_url=_req_base,
                )
                for w in img_warns:
                    yield evt("log", level="warn", msg=f"  ⚠ {w}")
                product = dict(product)
                product["images"] = ym_photos
                if ym_photos:
                    yield evt("log", level="info", msg=f"  ✓ Фото: {len(ym_photos)} шт.")
                else:
                    yield evt("log", level="warn", msg="  ⚠ Фото не подготовлены — карточка будет без фото")

                yield evt("progress", current=i+1, total=total, code=code,
                          percent=int((i+0.5)/total*100), status="uploading")
                yield evt("log", level="info", msg="  → Отправляем на Яндекс.Маркет...")
                result = await ym.upload(product, cat, saved)
                if result["ok"]:
                    offer_id_val = result.get("offer_id")
                    yield evt("log", level="success", msg=f"  ✓ Загружен! offer_id: {offer_id_val}")
                    candidates = [offer_id_val, code, product.get("article",""), product.get("product_id","")]
                    state   = await ym.validate_offer_state(candidates)
                    rating  = state.get("rating")
                    missing = state.get("missing_fields", [])
                    status  = state.get("status", "")
                    color   = state.get("color", "")
                    if rating is not None:
                        level = "success" if rating >= _LOW_RATED_THRESHOLD else "warn"
                        yield evt("log", level=level, msg=f"    {color} Рейтинг: {rating}/100 (status: {status})")
                    else:
                        yield evt("log", level="info", msg=f"    {color} Status: {status}")
                    critical = [f for f in missing if f in _YM_CRITICAL_MISSING]
                    if critical:
                        yield evt("log", level="warn",
                                  msg=f"    Не заполнено: {', '.join(critical)}")
                    elif missing:
                        yield evt("log", level="info",
                                  msg="    ЯМ обрабатывает данные асинхронно, проверьте кабинет через несколько минут")
                    is_critical_final = bool(critical)
                    if status == "NEED_FIX":
                        _append_low_rated({"code": code, "offer_id": offer_id_val, "name": name,
                                           "category": cat, "category_name": cat_name,
                                           "rating": rating, "card_status": state.get("card_status",""),
                                           "missing_fields": critical, "is_critical": is_critical_final,
                                           "uploaded_at": _now_iso()})
                    yield evt("item", code=code, success=True, offer_id=offer_id_val,
                              name=name, price=price, rating=rating, state_status=status,
                              is_critical=is_critical_final, missing_fields=critical)
                    ok_count += 1
                else:
                    err = result.get("error","Неизвестная ошибка")
                    yield evt("log", level="error", msg=f"  ✗ Ошибка ЯМ: {err}")
                    yield evt("item", code=code, success=False, error=err)
                    err_count += 1

            done += 1
            yield evt("progress", current=done, total=total, code=code,
                      percent=int(done/total*100), status="done")
            await asyncio.sleep(0.5)

        yield evt("finish", success=ok_count, errors=err_count, total=total, dry_run=req.dry_run)

    return StreamingResponse(generate(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

# ─── YM: offer detail preview ────────────────────────────────

@app.post("/api/ym/offer/detail-preview")
async def ym_offer_detail_preview(req: YMPreviewRequest):
    ms = MoySkladClient(req.ms_token)
    ym = YandexMarketClient(req.ym_api_key, req.ym_campaign_id, req.ym_business_id)
    results = []
    for code in [c.strip() for c in req.codes if c.strip()]:
        p = await ms.get_product_data(code)
        if not p["ok"]:
            results.append({"ok": False, "code": code, "error": p.get("error", "Не найден")})
            continue

        cat, cat_confidence = await detect_category(p["name"], p["attributes"])
        saved = answers_store.get(code, {})
        cat = saved.get("category") or cat
        cat_name = get_ym_categories().get(str(cat), {}).get("name", str(cat)) if cat else ""

        bh_data  = await _enrich_from_bh(code, p, cat, ym)
        bh_specs = (bh_data or {}).get("specs") if bh_data else None
        ai_e     = await _ai_enrich(code, p, cat, ym, bh_specs)

        resolved = dict(saved)
        if ai_e and not ai_e.get("error"):
            if not resolved.get("brand") and ai_e.get("brand"):
                resolved["brand"] = ai_e["brand"]
            if not resolved.get("description") and ai_e.get("description"):
                resolved["description"] = ai_e["description"]
            pv = dict(resolved.get("params_values", {}) or {})
            for m in ai_e.get("parameter_values", []) or []:
                pv.setdefault(f"ym_{m['param_id']}", m["value"])
            if pv:
                resolved["params_values"] = pv

        offer = ym.build_offer(p, cat, resolved)

        cat_params = None
        if cat:
            try:
                cat_params = await _build_category_params_data(ym, str(cat), code)
            except Exception:
                pass

        # Fetch real YM state (rating, category, existence) using article + code
        candidates = list(dict.fromkeys(
            c for c in [p.get("article") or "", code] if c.strip()
        ))
        ym_state = await ym.validate_offer_state(candidates)

        results.append({
            "ok": True,
            "code": code,
            "offer": offer,
            "category_name": cat_name,
            "category_id": cat,
            "cat_confidence": cat_confidence,
            "enrichment": {
                "bh_found": (bh_data or {}).get("found", False),
                "bh_specs_count": (bh_data or {}).get("specs_count", 0),
                "ai_params_count": len((ai_e or {}).get("parameter_values", [])),
            },
            "category_params": cat_params,
            "rating": _estimate_offer_rating(offer, cat_params),
            "ym_state": ym_state,
            "warnings": _ym_warnings(p, resolved),
        })
        await asyncio.sleep(0.2)

    return {"ok": True, "results": results}


def _estimate_offer_rating(offer: dict, cat_params) -> dict:
    score = 0
    breakdown = []

    def chk(field, pts, max_pts, ok, value, partial=0):
        nonlocal score
        earned = pts if ok else partial
        score += earned
        breakdown.append({"field": field, "pts": earned, "max": max_pts, "ok": ok, "value": value})

    # Название (15 баллов: ≥50 симв. = полные)
    name = offer.get("name", "")
    nlen = len(name)
    if nlen >= 50:
        chk("Название", 15, 15, True, name[:60])
    elif nlen >= 20:
        chk("Название", 15, 15, False, f"{nlen} симв. (рекоменд. ≥50)", partial=9)
    else:
        chk("Название", 15, 15, False, name[:60] if name else "отсутствует", partial=0)

    # Описание (20 баллов: ≥500 симв.)
    desc = offer.get("description", "")
    dlen = len(desc)
    if dlen >= 500:
        chk("Описание", 20, 20, True, f"{dlen} симв.")
    elif dlen >= 100:
        chk("Описание", 20, 20, False, f"{dlen} симв. (рекоменд. ≥500)", partial=12)
    elif dlen >= 50:
        chk("Описание", 20, 20, False, f"{dlen} симв. (нужно ≥50)", partial=6)
    else:
        chk("Описание", 20, 20, False, f"{dlen} симв." if desc else "отсутствует", partial=0)

    # Бренд (10 баллов)
    chk("Бренд", 10, 10, bool(offer.get("vendor")), offer.get("vendor") or "не указан")

    # Фотографии (15 баллов: ≥5 = полные, ≥1 = частичные)
    pics = offer.get("pictures", [])
    np_ = len(pics)
    if np_ >= 5:
        chk("Фотографии", 15, 15, True, f"{np_} шт.")
    elif np_ >= 3:
        chk("Фотографии", 15, 15, False, f"{np_} шт. (рекоменд. ≥5)", partial=10)
    elif np_ >= 1:
        chk("Фотографии", 15, 15, False, f"{np_} шт. (рекоменд. ≥5)", partial=5)
    else:
        chk("Фотографии", 15, 15, False, "нет", partial=0)

    # Цена (5 баллов)
    price = (offer.get("basicPrice") or {}).get("value", 0)
    chk("Цена", 5, 5, bool(price and price > 0), f"{price:,} ₽" if price else "не задана")

    # Габариты/вес (10 баллов)
    dims = offer.get("weightDimensions") or {}
    has_dims = all(dims.get(k) for k in ("width", "height", "length", "weight"))
    if has_dims:
        d = dims
        chk("Габариты/вес", 10, 10, True,
            f"{d.get('width',0)}×{d.get('height',0)}×{d.get('length',0)} см, {d.get('weight',0)} кг")
    else:
        missing = [k for k in ("width","height","length","weight") if not dims.get(k)]
        chk("Габариты/вес", 10, 10, False, f"нет: {', '.join(missing)}", partial=0)

    # Категория ЯМ (5 баллов)
    chk("Категория ЯМ", 5, 5, bool(offer.get("marketCategoryId")),
        str(offer.get("marketCategoryId") or "не определена"))

    # Характеристики категории (20 баллов)
    params = offer.get("parameterValues", [])
    n = len(params)
    if n >= 8:
        chk("Характеристики", 20, 20, True, f"{n} заполнено")
    elif n >= 4:
        chk("Характеристики", 20, 20, False, f"{n} заполнено (рекоменд. ≥8)", partial=12)
    elif n >= 1:
        chk("Характеристики", 20, 20, False, f"{n} заполнено", partial=5)
    else:
        chk("Характеристики", 20, 20, False, "нет данных", partial=0)

    status = "OK" if score >= 80 else ("WARN" if score >= 50 else "NEED_FIX")
    return {"score": score, "max": 100, "status": status, "breakdown": breakdown}


# ─── YM: offer check / low-rated ─────────────────────────────

@app.post("/api/ym/offer/check")
async def ym_offer_check(req: YMCodeRequest):
    ms = MoySkladClient(req.ms_token)
    ym = YandexMarketClient(req.ym_api_key, req.ym_campaign_id, req.ym_business_id)
    product = await ms.get_product_data(req.code)
    if not product.get("ok"):
        return {"ok": False, "error": product.get("error", "МС: товар не найден")}
    candidates = [req.code, product.get("article",""), product.get("code",""), product.get("product_id","")]
    state = await ym.validate_offer_state(candidates)
    state["ms_code"] = req.code
    state["ms_name"] = product.get("name","")
    if state.get("status") == "NEED_FIX":
        _append_low_rated({"code": req.code, "offer_id": state.get("offer_id"),
                           "name": product.get("name",""), "rating": state.get("rating"),
                           "missing_fields": state.get("missing_fields",[]),
                           "card_status": state.get("card_status",""), "checked_at": _now_iso()})
    return {"ok": True, "state": state}

@app.get("/api/ym/low-rated")
async def ym_low_rated():
    return {"ok": True, "threshold": _LOW_RATED_THRESHOLD, "items": _load_low_rated()}

@app.post("/api/ym/low-rated/clear")
async def ym_clear_low_rated():
    try:
        if os.path.exists(_LOW_RATED_FILE):
            os.remove(_LOW_RATED_FILE)
    except Exception:
        pass
    return {"ok": True}

# ─── Ozon: upload SSE ─────────────────────────────────────────

@app.post("/api/ozon/upload/stream")
async def ozon_upload_stream(req: OzonUploadRequest, request: Request):
    codes = [c.strip() for c in req.codes if c.strip()]
    _req_base = _req_base_url(request)

    async def generate():
        ms = MoySkladClient(req.ms_token)
        oz = OzonClient(req.ozon_client_id, req.ozon_api_key)
        total = len(codes)
        done = ok_count = err_count = 0

        def evt(type_: str, **kw):
            return f"data: {json.dumps({'type': type_, **kw}, ensure_ascii=False)}\n\n"

        yield evt("start", total=total, dry_run=req.dry_run, mode="DRY RUN" if req.dry_run else "LIVE")

        for i, code in enumerate(codes):
            yield evt("progress", current=i+1, total=total, code=code,
                      percent=int(i/total*100), status="reading")
            yield evt("log", level="info", msg=f"[{i+1}/{total}] Код: {code} → читаем из МойСклад...")

            product = await ms.get_product_data(code)
            if not product["ok"]:
                yield evt("log", level="error", msg=f"  ✗ {product['error']}")
                yield evt("item", code=code, success=False, error=product["error"])
                err_count += 1; done += 1
                yield evt("progress", current=done, total=total, code=code,
                          percent=int(done/total*100), status="error")
                continue

            name   = product["name"]
            price  = product.get("price_ozon") or product.get("price_main", 0)
            imgs   = product["images_count"]
            yield evt("log", level="info", msg=f"  ✓ Найден: «{name}»")
            yield evt("log", level="info", msg=f"    Цена Ozon: {price:,.0f} ₽  |  Фото: {imgs}")

            saved = answers_store.get(code, {})
            for w in _ozon_warnings(product, saved):
                yield evt("log", level="warn", msg=f"  ⚠ {w}")

            cat, _ = await detect_category(name, product.get("attributes", {}))
            yield evt("log", level="info", msg=f"  Категория Ozon: {cat or '(автоопределение)'}")

            if req.dry_run:
                yield evt("log", level="info",
                          msg=f"  → [DRY RUN] артикул: {product.get('article',code)} | категория: {cat}")
                yield evt("log", level="info", msg="  → Карточка сформирована, загрузки нет")
                yield evt("item", code=code, success=True, dry_run=True,
                          name=name, price=price, category=cat,
                          article=product.get("article", code))
                ok_count += 1
            else:
                # Prepare images: download MS (auth-required) images and store locally
                yield evt("progress", current=i+1, total=total, code=code,
                          percent=int((i+0.4)/total*100), status="processing")
                yield evt("log", level="info", msg="  → Подготавливаем фотографии...")
                oz_photos, img_warns = await _prepare_images(
                    req.ms_token, product, saved, code, subfolder="oz_proc", border_pct=0,
                    base_url=_req_base,
                )
                for w in img_warns:
                    yield evt("log", level="warn", msg=f"  ⚠ {w}")
                product = dict(product)
                product["images"] = oz_photos  # empty list when failed — never send raw MS auth URLs
                if oz_photos:
                    yield evt("log", level="info", msg=f"  ✓ Фото подготовлено: {len(oz_photos)} шт.")
                else:
                    yield evt("log", level="warn", msg="  ⚠ Фотографии недоступны — карточка будет без фото")

                yield evt("progress", current=i+1, total=total, code=code,
                          percent=int((i+0.5)/total*100), status="uploading")
                action_word = "Обновляем" if req.force_update else "Отправляем"
                _dbg_imgs = product.get("images", [])
                yield evt("log", level="info", msg=f"  → {action_word} на Ozon... (фото: {len(_dbg_imgs)} шт.)")
                for _di, _du in enumerate(_dbg_imgs[:5]):
                    yield evt("log", level="info", msg=f"    img[{_di}]: {str(_du)[:120]}")
                result = await oz.upload(product, saved)
                if result["ok"]:
                    task_id = result.get("task_id")
                    yield evt("log", level="success", msg=f"  ✓ Принято Ozon! task_id: {task_id}")
                    if task_id:
                        yield evt("log", level="info", msg="  ⏳ Ожидаем обработки Ozon...")
                        for _ in range(5):
                            await asyncio.sleep(3)
                            st     = await oz.get_upload_status(task_id)
                            status = st.get("status","")
                            errs   = st.get("errors",[])
                            if status == "imported":
                                yield evt("log", level="success", msg="  ✓ Статус: импортирован успешно")
                                break
                            elif status == "failed" or errs:
                                yield evt("log", level="error",
                                          msg=f"  ✗ Ошибка Ozon: {'; '.join(errs[:2])}")
                                break
                            else:
                                yield evt("log", level="info", msg=f"  ... статус: {status or 'processing'}")
                    yield evt("item", code=code, success=True, task_id=task_id,
                              name=name, price=price, article=product.get("article", code))
                    ok_count += 1
                else:
                    err = result.get("error","Неизвестная ошибка")
                    yield evt("log", level="error", msg=f"  ✗ Ошибка Ozon: {err}")
                    yield evt("item", code=code, success=False, error=err)
                    err_count += 1

            done += 1
            yield evt("progress", current=done, total=total, code=code,
                      percent=int(done/total*100), status="done")
            await asyncio.sleep(0.4)

        yield evt("finish", success=ok_count, errors=err_count, total=total, dry_run=req.dry_run)

    return StreamingResponse(generate(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

@app.post("/api/ozon/products")
async def ozon_get_products(k: OzonKeys):
    oz = OzonClient(k.ozon_client_id, k.ozon_api_key)
    return await oz.get_products()

# ─── Ozon: categories ─────────────────────────────────────────

@app.post("/api/ozon/categories/raw-debug")
async def ozon_categories_raw_debug(k: OzonKeys):
    """Возвращает первые 2 дерева из /v1/description-category/tree для диагностики структуры."""
    from ozon import OzonClient as _OZ
    oz2 = _OZ(k.ozon_client_id, k.ozon_api_key)
    r = await oz2._post("/v1/description-category/tree", {"language": "RU"})
    if not r["ok"]:
        return r
    roots = (r["data"].get("result") or [])[:2]
    return {"ok": True, "sample_roots": roots}

@app.post("/api/ozon/categories/refresh")
async def ozon_categories_refresh(k: OzonKeys):
    oz = OzonClient(k.ozon_client_id, k.ozon_api_key)
    result = await oz.get_categories_tree()
    if not result.get("ok"):
        return result
    cats = result.get("categories", {})
    try:
        with open(_OZON_CATS_FILE, "w", encoding="utf-8") as f:
            json.dump(cats, f, ensure_ascii=False, indent=2)
    except Exception as e:
        return {"ok": False, "error": f"Не удалось сохранить: {e}"}
    return {"ok": True, "count": len(cats), "message": f"Загружено {len(cats)} категорий Ozon"}

@app.get("/api/ozon/categories")
async def ozon_list_categories():
    try:
        with open(_OZON_CATS_FILE, "r", encoding="utf-8") as f:
            cats = json.load(f)
        roots    = [v for v in cats.values() if v.get("parent_id") is None]
        w_children = [v for v in cats.values() if v.get("has_children")]
        depths   = {}
        for v in cats.values():
            d = len(v.get("path") or []) - 1
            depths[d] = depths.get(d, 0) + 1
        return {
            "ok": True, "count": len(cats), "categories": cats,
            "tree_ok": len(roots) < len(cats),  # true если есть хоть один дочерний узел
            "roots": len(roots), "with_children": len(w_children), "depth_counts": depths,
        }
    except Exception:
        return {"ok": False, "count": 0, "categories": {}, "tree_ok": False}

class OzonCatAttrBody(BaseModel):
    ozon_client_id: str
    ozon_api_key: str
    category_key: str = ""  # composite "{desc_cat_id}_{type_id}" or plain "{desc_cat_id}"

@app.post("/api/ozon/category/attributes")
async def ozon_category_attributes(body: OzonCatAttrBody):
    oz = OzonClient(body.ozon_client_id, body.ozon_api_key)
    try:
        parts = body.category_key.split("_")
        cat_id  = int(parts[0])
        type_id = int(parts[1]) if len(parts) > 1 else None
    except Exception:
        return {"ok": False, "error": "Неверный формат category_key"}
    if not type_id:
        return {
            "ok": False,
            "error": (
                "Не найден тип товара (type_id) для этой категории Ozon. "
                "Выберите финальный подраздел в каскаде категорий "
                "(тот, у которого нет значка ›)."
            )
        }
    # Check file cache first
    cache: dict = {}
    try:
        with open(_OZ_ATTRS_CACHE, "r", encoding="utf-8") as f:
            cache = json.load(f)
    except Exception:
        pass
    if body.category_key in cache:
        attrs = cache[body.category_key]
        return {"ok": True, "attributes": attrs, "total": len(attrs), "cached": True}

    result = await oz.get_category_attributes(cat_id, type_id)
    if result.get("ok"):
        cache[body.category_key] = result["attributes"]
        try:
            with open(_OZ_ATTRS_CACHE, "w", encoding="utf-8") as f:
                json.dump(cache, f, ensure_ascii=False, indent=2)
        except Exception:
            pass
    return result

class OzonAttrValBody(BaseModel):
    ozon_client_id: str
    ozon_api_key: str
    description_category_id: int
    type_id: int
    attribute_id: int

@app.post("/api/ozon/category/attribute/values")
async def ozon_attribute_values(body: OzonAttrValBody):
    """Словарные значения для атрибута Ozon (с кэшированием в файл)."""
    cache_key = f"{body.description_category_id}_{body.type_id}_{body.attribute_id}"
    cache: dict = {}
    try:
        with open(_OZ_ATTR_VALS_CACHE, "r", encoding="utf-8") as f:
            cache = json.load(f)
    except Exception:
        pass
    if cache_key in cache:
        vals = cache[cache_key]
        return {"ok": True, "values": vals, "total": len(vals), "cached": True}

    oz = OzonClient(body.ozon_client_id, body.ozon_api_key)
    result = await oz.get_attribute_values(body.description_category_id, body.type_id, body.attribute_id)
    if result.get("ok"):
        cache[cache_key] = result["values"]
        try:
            with open(_OZ_ATTR_VALS_CACHE, "w", encoding="utf-8") as f:
                json.dump(cache, f, ensure_ascii=False, indent=2)
        except Exception:
            pass
    return result

# ─── Product: load for all marketplaces ───────────────────────

class ProductLoadRequest(AllKeys):
    code: str

@app.post("/api/product/load")
async def product_load(req: ProductLoadRequest):
    ms = MoySkladClient(req.ms_token)
    ym = YandexMarketClient(req.ym_api_key, req.ym_campaign_id, req.ym_business_id)
    oz = OzonClient(req.ozon_client_id, req.ozon_api_key)

    product = await ms.get_product_data(req.code)
    if not product["ok"]:
        return product

    offer_id = product.get("article") or req.code
    cat, cat_confidence = await detect_category(product["name"], product.get("attributes", {}))
    cat_name = get_ym_categories().get(str(cat), {}).get("name", cat) if cat else ""
    saved = answers_store.get(req.code, {})

    async def _ym_check():
        if not req.ym_api_key: return False
        try: return await ym.check_exists(offer_id)
        except: return False

    async def _oz_check():
        if not req.ozon_client_id: return False
        try: return await oz.check_exists(offer_id)
        except: return False

    ym_exists, oz_exists = await asyncio.gather(_ym_check(), _oz_check())

    video_info = video_builder.video_info(req.code)
    video_url = saved.get("video_url") or (_video_public_url(req.code) if video_info else None)

    oz_cat_id  = saved.get("ozon_category_id")
    oz_cat_key = saved.get("ozon_category_key") or (str(oz_cat_id) if oz_cat_id else "")
    try:
        with open(_OZON_CATS_FILE, "r", encoding="utf-8") as f:
            _oz_cats_tmp = json.load(f)
        entry = (_oz_cats_tmp.get(oz_cat_key) or _oz_cats_tmp.get(str(oz_cat_id), {})) if oz_cat_id else {}
        oz_cat_name = entry.get("name", "") if entry else ""
    except Exception:
        oz_cat_name = ""
        _oz_cats_tmp = {}

    wb_cat_key  = saved.get("wb_category_key", "")
    wb_cat_name = saved.get("wb_category_name", "")
    wb_subject_id = saved.get("wb_subject_id")

    # ── Gemini AI auto-enrichment (runs if key set and not already done) ──
    ai_result = None
    gemini_key = (req.gemini_api_key or os.getenv("GEMINI_API_KEY", "")).strip()
    if gemini_key and req.code not in _AI_ENRICH_CACHE:
        try:
            # Load cached categories for context
            _ym_cats = get_ym_categories() or {}
            _wb_cats: dict = {}
            try:
                with open(_WB_CATS_FILE, "r", encoding="utf-8") as f:
                    _wb_cats = json.load(f)
            except Exception:
                pass

            # YM params for detected category
            _ym_params = []
            if cat:
                try:
                    _ym_params = await get_category_parameters_cached(ym, cat)
                except Exception:
                    pass

            # B&H enrichment via Firecrawl
            bh_data = _BH_CACHE.get(req.code)
            if bh_data is None:
                try:
                    _attrs  = product.get("attributes") or {}
                    _brand  = _attrs.get("Бренд") or product.get("brand", "")
                    _art    = product.get("article", "")
                    bh_raw  = await get_bh_data(product["name"], brand=_brand, article=_art)
                    bh_data = bh_raw or {"found": False}
                    _BH_CACHE[req.code] = bh_data
                except Exception as e:
                    bh_data = {"error": str(e)[:200]}

            bh_specs = (bh_data or {}).get("specs") or {}

            ai_result = await gemini_auto_enrich(
                product=product,
                saved=saved,
                ym_categories=_ym_cats,
                ym_params=_ym_params,
                ozon_categories=_oz_cats_tmp,
                wb_categories=_wb_cats,
                bh_specs=bh_specs,
                api_key=gemini_key,
            )

            if ai_result and not ai_result.get("error"):
                _AI_ENRICH_CACHE[req.code] = ai_result
                # Merge into answers_store (only fields not already set by user)
                if req.code not in answers_store:
                    answers_store[req.code] = {}
                _ans = answers_store[req.code]
                if not _ans.get("brand") and ai_result.get("brand"):
                    _ans["brand"] = ai_result["brand"]
                if not _ans.get("description") and ai_result.get("description"):
                    _ans["description"] = ai_result["description"]
                if not _ans.get("category") and ai_result.get("ym_category_id"):
                    _ans["category"] = ai_result["ym_category_id"]
                if not _ans.get("ozon_category_key") and ai_result.get("ozon_category_key"):
                    _ans["ozon_category_key"] = ai_result["ozon_category_key"]
                    if ai_result.get("ozon_category_id"):
                        _ans["ozon_category_id"] = ai_result["ozon_category_id"]
                    if ai_result.get("ozon_type_id"):
                        _ans["ozon_type_id"] = ai_result["ozon_type_id"]
                if not _ans.get("wb_category_key") and ai_result.get("wb_category_key"):
                    _ans["wb_category_key"] = ai_result["wb_category_key"]
                    if ai_result.get("wb_subject_id"):
                        _ans["wb_subject_id"] = ai_result["wb_subject_id"]
                    if ai_result.get("wb_category_name"):
                        _ans["wb_category_name"] = ai_result["wb_category_name"]
                # Merge YM params
                if ai_result.get("ym_params"):
                    pv = dict(_ans.get("params_values") or {})
                    for k, v in ai_result["ym_params"].items():
                        if k not in pv:
                            pv[k] = v
                    _ans["params_values"] = pv
                # Merge Ozon attrs
                if ai_result.get("ozon_attrs"):
                    oa = dict(_ans.get("ozon_attrs") or {})
                    for k, v in ai_result["ozon_attrs"].items():
                        if k not in oa:
                            oa[k] = v
                    _ans["ozon_attrs"] = oa
                # Merge WB chars
                if ai_result.get("wb_chars"):
                    wc = dict(_ans.get("wb_chars") or {})
                    for k, v in ai_result["wb_chars"].items():
                        if k not in wc:
                            wc[k] = v
                    _ans["wb_chars"] = wc
                saved = answers_store[req.code]
        except Exception as e:
            ai_result = {"error": f"gemini: {str(e)[:200]}"}

    # Refresh category info after possible AI enrichment
    oz_cat_id    = saved.get("ozon_category_id") or oz_cat_id
    oz_cat_key   = saved.get("ozon_category_key") or oz_cat_key
    wb_cat_key   = saved.get("wb_category_key") or wb_cat_key
    wb_cat_name  = saved.get("wb_category_name") or wb_cat_name
    wb_subject_id = saved.get("wb_subject_id") or wb_subject_id

    return {
        "ok": True,
        "product": product,
        "category": saved.get("category") or cat,
        "category_confidence": cat_confidence,
        "category_name": cat_name,
        "ozon_category_id": oz_cat_id,
        "ozon_category_key": oz_cat_key,
        "ozon_category_name": oz_cat_name,
        "wb_category_key": wb_cat_key,
        "wb_category_name": wb_cat_name,
        "wb_subject_id": wb_subject_id,
        "ym_exists": ym_exists,
        "ozon_exists": oz_exists,
        "brand": saved.get("brand") or product.get("brand", ""),
        "description": saved.get("description") or product.get("description", ""),
        "video_url": video_url,
        "video_exists": bool(video_info),
        "user_images": saved.get("user_images", []),
        "ai_enriched": bool(ai_result and not ai_result.get("error")),
        "ai_error": (ai_result or {}).get("error"),
        "bh_data": _BH_CACHE.get(req.code),
    }

@app.post("/api/ozon/preview")
async def ozon_preview(req: OzonUploadRequest):
    ms = MoySkladClient(req.ms_token)
    oz = OzonClient(req.ozon_client_id, req.ozon_api_key)
    results = []
    for code in [c.strip() for c in req.codes if c.strip()]:
        p = await ms.get_product_data(code)
        if not p["ok"]:
            results.append({"code": code, "error": p["error"], "action": "SKIP"})
            continue
        article = p.get("article") or code
        exists  = await oz.check_exists(article)
        cat, _  = await detect_category(p["name"], p.get("attributes", {}))
        saved   = answers_store.get(code, {})
        results.append({
            "code": code, "article": article, "name": p["name"],
            "price_ozon": p.get("price_ozon") or p.get("price_main", 0),
            "images_count": p["images_count"],
            "has_description": p["has_description"],
            "category": cat,
            "ozon_exists": exists,
            "action": "UPDATE" if exists else "UPLOAD",
            "warnings": _ozon_warnings(p, saved),
        })
        await asyncio.sleep(0.2)
    up  = sum(1 for r in results if r.get("action") == "UPLOAD")
    upd = sum(1 for r in results if r.get("action") == "UPDATE")
    err = sum(1 for r in results if "error" in r)
    return {"results": results, "summary": {"total": len(results), "upload": up, "update": upd, "error": err}}

@app.post("/api/ozon/offer/detail-preview")
async def ozon_offer_detail_preview(req: OzonUploadRequest):
    ms = MoySkladClient(req.ms_token)
    oz = OzonClient(req.ozon_client_id, req.ozon_api_key)
    try:
        with open(_OZON_CATS_FILE, "r", encoding="utf-8") as f:
            ozon_cats = json.load(f)
    except Exception:
        ozon_cats = {}
    results = []
    for code in [c.strip() for c in req.codes if c.strip()]:
        p = await ms.get_product_data(code)
        if not p["ok"]:
            results.append({"ok": False, "code": code, "error": p.get("error", "Не найден")})
            continue
        saved = answers_store.get(code, {})
        cat_id  = saved.get("ozon_category_id")
        cat_key = saved.get("ozon_category_key") or (str(cat_id) if cat_id else "")
        cat_name = ""
        if cat_key:
            entry = ozon_cats.get(cat_key) or (ozon_cats.get(str(cat_id)) if cat_id else {}) or {}
            cat_name = entry.get("name", str(cat_id) if cat_id else "")
            path_arr = entry.get("path") or []
            if path_arr:
                cat_name = " › ".join(path_arr)
        item = oz.build_item(p, saved)

        # Проверяем существует ли товар на Ozon и получаем его данные
        offer_id = p.get("article") or code
        ozon_existing = None
        ozon_exists = False
        try:
            ex_r = await oz.get_product_info_by_offer(offer_id)
            if ex_r.get("ok"):
                ozon_exists = True
                oz_item = ex_r["item"]
                oz_sku  = ex_r.get("sku")
                oz_rating = None
                if oz_sku:
                    rtg_r = await oz.get_content_rating_by_skus([oz_sku])
                    if rtg_r.get("ok") and rtg_r["ratings"]:
                        oz_rating = rtg_r["ratings"][0].get("totalRating")
                oz_attrs = oz_item.get("attributes") or []
                ozon_existing = {
                    "product_id":   oz_item.get("id"),
                    "name":         oz_item.get("name", ""),
                    "category_id":  oz_item.get("description_category_id"),
                    "type_id":      oz_item.get("type_id"),
                    "status":       (oz_item.get("status") or {}).get("state", ""),
                    "content_rating": oz_rating,
                    "attributes_count": len(oz_attrs),
                    "price":        oz_item.get("price", ""),
                    "images":       oz_item.get("images") or [],
                    "sku":          oz_sku,
                }
        except Exception:
            pass

        # Считаем сохранённые кастомные атрибуты Ozon (из oz_pm_save)
        saved_oz_attrs = {k: v for k, v in saved.items() if k.startswith("oz_attr_") and v}
        results.append({
            "ok": True, "code": code,
            "item": item,
            "category_id": cat_id,
            "category_key": cat_key,
            "category_name": cat_name,
            "ozon_exists": ozon_exists,
            "ozon_existing": ozon_existing,
            "price_ozon": p.get("price_ozon") or p.get("price_main", 0),
            "price_ozon_source": p.get("price_ozon_source", ""),
            "saved_oz_attrs_count": len(saved_oz_attrs),
            "warnings": _ozon_warnings(p, saved),
        })
        await asyncio.sleep(0.1)
    return {"ok": True, "results": results}

# ─── Rules-based category matching (categories.yaml) ─────────

_RULES_CACHE: list | None = None

def _load_category_rules() -> list:
    global _RULES_CACHE
    if _RULES_CACHE is not None:
        return _RULES_CACHE
    try:
        import yaml
    except ImportError:
        _RULES_CACHE = []
        return []
    # Try marketplace_prep sibling dir first (local dev), fallback to local config/
    candidates = [
        Path(__file__).parent.parent.parent / "marketplace_prep" / "config" / "categories.yaml",
        Path(__file__).parent / "config" / "categories.yaml",
    ]
    for p in candidates:
        if p.exists():
            with open(p, encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
            _RULES_CACHE = data.get("rules", [])
            return _RULES_CACHE
    _RULES_CACHE = []
    return []


def _match_one_rule(rule: dict, product: dict) -> bool:
    """Проверяет одно правило против dict с полями товара (internal_category, name, sku)."""
    field = rule.get("field", "internal_category")
    value = str(product.get(field) or "").strip()
    if not value:
        return False
    pattern = rule.get("pattern", "")
    ptype = rule.get("pattern_type", rule.get("type", "exact"))
    if ptype == "exact":
        return value.lower() == pattern.lower()
    if ptype == "contains":
        return pattern.lower() in value.lower()
    if ptype == "regex":
        try:
            return bool(re.search(pattern, value, re.IGNORECASE))
        except re.error:
            return False
    return False


def _rules_match_category(product: dict, mp: str) -> dict | None:
    """
    Подбирает категорию по правилам categories.yaml.
    product — dict с ключами: internal_category, name, sku (любые могут быть пустыми).
    mp — 'ym' | 'oz' | 'wb'.
    """
    rules = _load_category_rules()
    mp_lower = mp.lower()
    mp_aliases = {mp_lower}
    if mp_lower == "oz":
        mp_aliases.add("ozon")
    elif mp_lower == "ozon":
        mp_aliases.add("oz")

    matches = []
    for rule in rules:
        rule_mp = (rule.get("marketplace") or "all").lower()
        if rule_mp not in mp_aliases and rule_mp != "all":
            continue
        if _match_one_rule(rule, product):
            matches.append(rule)

    if not matches:
        return None
    matches.sort(key=lambda r: r.get("priority", 0), reverse=True)
    return matches[0]


class RulesCategoryRequest(BaseModel):
    mp: str
    code: str = ""
    ms_token: str = os.getenv("MS_TOKEN", "")
    internal_category: str = ""


@app.post("/api/rules/suggest-category")
async def rules_suggest_category(req: RulesCategoryRequest):
    mp = req.mp.lower()
    internal_category = req.internal_category.strip()
    product_name = ""

    # Если код передан — грузим папку и название из МС
    if req.code:
        ms_token = (req.ms_token or os.getenv("MS_TOKEN", "")).strip()
        if ms_token:
            try:
                ms = MoySkladClient(ms_token)
                folder_r = await ms.get_product_folder(req.code)
                if folder_r.get("ok"):
                    if not internal_category:
                        internal_category = folder_r.get("folder_name", "")
                    product_name = folder_r.get("name", "")
            except Exception:
                pass

    if not internal_category and not product_name:
        return {"ok": False, "error": "Не удалось определить папку товара в МойСклад"}

    product = {
        "internal_category": internal_category,
        "name": product_name,
        "sku": req.code,
    }

    rule = _rules_match_category(product, mp)
    if not rule:
        return {
            "ok": False,
            "error": f"Нет правила для папки «{internal_category}» (маркетплейс: {mp}). "
                     f"Добавьте правило в categories.yaml.",
            "internal_category": internal_category,
        }

    cat_id = rule.get("cat_id")
    cat_type_id = rule.get("cat_type_id")
    cat_name = rule.get("cat_name", "")

    if mp == "oz":
        key = f"{cat_id}_{cat_type_id}" if cat_type_id else str(cat_id)
    elif mp == "wb":
        key = f"s_{cat_id}"
    else:  # ym
        key = str(cat_id)

    return {
        "ok": True,
        "category_id": key,
        "category_name": cat_name,
        "internal_category": internal_category,
        "path": [cat_name],
        "rule_pattern": rule.get("pattern"),
        "rule_priority": rule.get("priority", 0),
        "matched_field": rule.get("field", "internal_category"),
    }


# ─── AI Category (Gemini) ─────────────────────────────────────

class AICategoryRequest(BaseModel):
    mp: str                # 'ym' | 'oz' | 'wb'
    name: str = ""
    description: str = ""
    brand: str = ""
    gemini_api_key: str = ""
    # Extended: load real product data + images + B&H
    code: str = ""
    ms_token: str = os.getenv("MS_TOKEN", "")

@app.post("/api/ai/suggest-category")
async def ai_suggest_category(req: AICategoryRequest):
    """AI-подбор категории через Gemini.
    Если передан code — загружает товар из МС (с фото), парсит B&H, отправляет Gemini мультимодально.
    После выбора категории возвращает её характеристики.
    """
    mp = req.mp.lower()
    cats_dict: dict = {}

    if mp == "oz":
        try:
            with open(_OZON_CATS_FILE, "r", encoding="utf-8") as f:
                cats_dict = json.load(f)
        except Exception:
            return {"ok": False, "error": "Категории Ozon не загружены. Нажмите «Обновить категории»."}
    elif mp == "wb":
        try:
            with open(_WB_CATS_FILE, "r", encoding="utf-8") as f:
                cats_dict = json.load(f)
        except Exception:
            return {"ok": False, "error": "Категории WB не загружены. Нажмите «Обновить категории»."}
    elif mp == "ym":
        cats_dict = get_ym_categories()
        if not cats_dict:
            return {"ok": False, "error": "Категории ЯМ не загружены. Нажмите «Обновить категории ЯМ»."}
    else:
        return {"ok": False, "error": f"Неизвестный маркетплейс: {mp}"}

    # Load full product data from MoySklad if code is given
    name = req.name
    description = req.description
    brand = req.brand
    image_urls: list = []
    bh_specs: dict = {}

    if req.code:
        ms_token = (req.ms_token or os.getenv("MS_TOKEN", "")).strip()
        if ms_token:
            try:
                ms = MoySkladClient(ms_token)
                p = await ms.get_product_data(req.code)
                if p.get("ok"):
                    name        = name        or p.get("name", "")
                    description = description or p.get("description", "")
                    brand       = brand       or p.get("brand", "")
                    image_urls  = p.get("images", [])
            except Exception:
                pass

        # B&H lookup for MFR # and brand enrichment
        if name:
            try:
                _p_attrs = (p or {}).get("attributes") or {}
                _p_brand = _p_attrs.get("Бренд") or brand
                _p_art   = (p or {}).get("article", "")
                bh_data  = await get_bh_data(name, brand=_p_brand, article=_p_art)
                bh_specs = (bh_data or {}).get("specs") or {}
            except Exception:
                pass

    api_key = (req.gemini_api_key or os.getenv("GEMINI_API_KEY", "")).strip()
    try:
        result = await gemini_suggest_category(
            mp=mp,
            name=name,
            description=description,
            brand=brand,
            categories=cats_dict,
            api_key=api_key,
            image_urls=image_urls,
            ms_token=(req.ms_token or os.getenv("MS_TOKEN", "")).strip(),
            bh_specs=bh_specs,
        )
        if not result.get("ok"):
            return result

        # After category is found — load its characteristics
        cat_id = result["category_id"]
        characteristics = []
        try:
            if mp == "ym":
                ym_key = os.getenv("YM_API_KEY", "").strip()
                ym_bid = os.getenv("YM_BUSINESS_ID", "").strip()
                if ym_key and ym_bid:
                    ym = YandexMarketClient(ym_key, os.getenv("YM_CAMPAIGN_ID", ""), ym_bid)
                    characteristics = await get_category_parameters_cached(ym, str(cat_id))
            elif mp == "oz":
                oz_cid = os.getenv("OZON_CLIENT_ID", "").strip()
                oz_key = os.getenv("OZON_API_KEY", "").strip()
                if oz_cid and oz_key:
                    oz = OzonClient(oz_cid, oz_key)
                    cat = cats_dict.get(cat_id, {})
                    desc_cat_id = cat.get("desc_cat_id") or int(cat_id.split("_")[0])
                    attrs_r = await oz.get_category_attributes(desc_cat_id, cat.get("type_id"))
                    if attrs_r.get("ok"):
                        characteristics = attrs_r.get("attributes", [])
            elif mp == "wb":
                wb_key = os.getenv("WB_API_KEY", "").strip()
                if wb_key:
                    wb = WildberriesClient(wb_key)
                    cat = cats_dict.get(cat_id, {})
                    subject_id = cat.get("int_id")
                    if subject_id:
                        chars_r = await wb.get_characteristics(int(subject_id))
                        if chars_r.get("ok"):
                            characteristics = chars_r.get("characteristics", [])
        except Exception:
            pass

        result["characteristics"] = characteristics
        return result
    except Exception as e:
        return {"ok": False, "error": str(e)}

# ─── AI: enrich category params (second pass) ────────────────

class EnrichParamsRequest(BaseModel):
    code:           str
    marketplace:    str            # 'ym' | 'ozon' | 'wb'
    category_id:    str
    ms_token:       str = os.getenv("MS_TOKEN", "")
    ym_api_key:     str = os.getenv("YM_API_KEY", "")
    ym_campaign_id: str = os.getenv("YM_CAMPAIGN_ID", "")
    ym_business_id: str = os.getenv("YM_BUSINESS_ID", "")
    ozon_client_id: str = os.getenv("OZON_CLIENT_ID", "")
    ozon_api_key:   str = os.getenv("OZON_API_KEY", "")
    wb_api_key:     str = os.getenv("WB_API_KEY", "")
    gemini_api_key: str = os.getenv("GEMINI_API_KEY", "")

@app.post("/api/ai/enrich-params")
async def ai_enrich_params(req: EnrichParamsRequest):
    """
    Second-pass AI enrichment: fill marketplace category attributes after category is selected.

    Uses: MS product data + B&H specs + regex spec extraction + Gemini.
    Returns {params: {"ym_123": "value", ...}, rules: {...}, filled: N}
    — frontend merges into answers_store, skipping already-filled fields.
    """
    from ai_gemini import enrich_category_params
    from spec_extractor import extract_specs, get_category_rules, format_for_ai

    mp = req.marketplace.lower()
    ms_token = (req.ms_token or os.getenv("MS_TOKEN", "")).strip()
    api_key  = (req.gemini_api_key or os.getenv("GEMINI_API_KEY", "")).strip()

    # ── 1. Load product from MoySklad ─────────────────────────
    try:
        ms = MoySkladClient(ms_token)
        product = await ms.get_product_data(req.code)
        if not product.get("ok"):
            return {"ok": False, "error": product.get("error", "Ошибка МС")}
    except Exception as e:
        return {"ok": False, "error": f"МойСклад: {e}"}

    name = product.get("name", "")
    desc = product.get("description", "") or ""

    # ── 2. B&H specs (from cache or fresh lookup) ─────────────
    bh_specs: dict = {}
    bh_cache = _BH_CACHE.get(req.code)
    if bh_cache and bh_cache.get("found"):
        bh_specs = bh_cache.get("specs") or {}
    else:
        try:
            p_attrs  = product.get("attributes") or {}
            p_brand  = p_attrs.get("Бренд") or product.get("brand", "")
            p_art    = product.get("article", "")
            bh_data  = await get_bh_data(name, brand=p_brand, article=p_art)
            if bh_data and bh_data.get("found"):
                _BH_CACHE[req.code] = bh_data
                bh_specs = bh_data.get("specs") or {}
        except Exception:
            pass

    # ── 3. Regex spec extraction ───────────────────────────────
    extracted = extract_specs(name, desc, bh_specs)

    # ── 4. Load full attribute list for category ───────────────
    attributes: list = []
    try:
        if mp == "ym":
            ym = YandexMarketClient(
                req.ym_api_key or os.getenv("YM_API_KEY",""),
                req.ym_campaign_id or os.getenv("YM_CAMPAIGN_ID",""),
                req.ym_business_id or os.getenv("YM_BUSINESS_ID",""),
            )
            attributes = await get_category_parameters_cached(ym, req.category_id) or []
        elif mp == "ozon":
            oz = OzonClient(
                req.ozon_client_id or os.getenv("OZON_CLIENT_ID",""),
                req.ozon_api_key   or os.getenv("OZON_API_KEY",""),
            )
            parts = str(req.category_id).split("_")
            desc_cat_id = int(parts[0])
            type_id     = int(parts[1]) if len(parts) > 1 else None
            r = await oz.get_category_attributes(desc_cat_id, type_id)
            if r.get("ok"):
                attributes = r.get("attributes", [])

            # Enrich dict attributes with allowed values so AI can pick the right ones.
            # Load from cache first, then fetch uncached values from Ozon API in parallel.
            if attributes:
                type_id_s = str(type_id) if type_id else "0"
                type_id_int = type_id or 0
                try:
                    with open(_OZ_ATTR_VALS_CACHE, "r", encoding="utf-8") as _f:
                        _av = json.load(_f)
                except Exception:
                    _av = {}

                needs_fetch = [
                    a for a in attributes
                    if a.get("dictionary_id")
                    and not _av.get(f"{desc_cat_id}_{type_id_s}_{a['id']}")
                ]

                if needs_fetch:
                    # Capture loop vars explicitly to avoid closure pitfalls
                    async def _fetch_one(attr_id: int, ckey: str):
                        try:
                            rv = await oz.get_attribute_values(
                                desc_cat_id, type_id_int, attr_id
                            )
                            if rv.get("ok") and rv.get("values"):
                                _av[ckey] = rv["values"]
                        except Exception:
                            pass

                    tasks = [
                        _fetch_one(a["id"], f"{desc_cat_id}_{type_id_s}_{a['id']}")
                        for a in needs_fetch[:30]
                    ]
                    # return_exceptions=True so one failure doesn't abort all others
                    await asyncio.gather(*tasks, return_exceptions=True)

                    try:
                        with open(_OZ_ATTR_VALS_CACHE, "w", encoding="utf-8") as _f:
                            json.dump(_av, _f, ensure_ascii=False, indent=2)
                    except Exception:
                        pass

                # Attach cached values to attribute defs so AI sees allowed_values
                for attr in attributes:
                    if attr.get("dictionary_id"):
                        ckey = f"{desc_cat_id}_{type_id_s}_{attr['id']}"
                        cached_vals = _av.get(ckey, [])
                        if cached_vals:
                            attr["values"] = cached_vals
        elif mp == "wb":
            wb = WildberriesClient(req.wb_api_key or os.getenv("WB_API_KEY",""))
            r = await wb.get_characteristics(int(req.category_id))
            if r.get("ok"):
                attributes = r.get("characteristics", [])
    except Exception as e:
        return {"ok": False, "error": f"Ошибка загрузки атрибутов: {e}"}

    if not attributes:
        return {"ok": False, "error": "Список атрибутов категории пуст или не загружен"}

    prefix_map = {"ym": "ym_", "ozon": "oz_", "wb": "wb_"}
    prefix = prefix_map.get(mp, "ym_")

    # ── 5. Apply rules layer (deterministic, no AI) ────────────
    rules_params: dict = {}
    for a in attributes:
        pid = a.get("id")
        if not pid:
            continue
        aname = a.get("name", "").lower()
        allowed = [v.get("value") or v.get("name") for v in (a.get("values") or a.get("dictionary") or [])
                   if isinstance(v, dict) and (v.get("value") or v.get("name"))]
        val = get_category_rules(extracted, aname, allowed or None)
        if val:
            rules_params[f"{prefix}{pid}"] = val

    # ── 6. AI enrichment (Gemini second pass) ──────────────────
    ai_params: dict = {}
    ai_error:  str  = ""
    if api_key:
        try:
            result = await enrich_category_params(
                product      = product,
                marketplace  = mp,
                attributes   = attributes,
                bh_specs     = bh_specs,
                extracted_specs = extracted,
                api_key      = api_key,
            )
            ai_params = result.get("params") or {}
            ai_error  = result.get("error") or ""
        except Exception as e:
            ai_error = str(e)

    # ── 7. Merge: rules win over AI for fixed values ───────────
    merged = {**ai_params, **rules_params}

    # ── 8. For Ozon: resolve dictionary_value_id from cache ────
    if mp == "ozon" and merged:
        cat_parts       = str(req.category_id).split("_")
        desc_cat_id_str = cat_parts[0]
        type_id_str     = cat_parts[1] if len(cat_parts) > 1 else "0"
        attr_defs       = {str(a["id"]): a for a in attributes if a.get("id")}
        try:
            with open(_OZ_ATTR_VALS_CACHE, "r", encoding="utf-8") as _f:
                _av_cache = json.load(_f)
        except Exception:
            _av_cache = {}

        resolved_merged = {}
        for k, v in merged.items():
            if not k.startswith("oz_"):
                resolved_merged[k] = v
                continue
            attr_id_str = k[3:]
            adef        = attr_defs.get(attr_id_str, {})
            dict_id     = adef.get("dictionary_id", 0)
            is_coll     = adef.get("is_collection", False)

            if dict_id:
                cache_key = f"{desc_cat_id_str}_{type_id_str}_{attr_id_str}"
                vals_list = _av_cache.get(cache_key, [])
                val_map   = {entry["value"].lower().strip(): entry for entry in vals_list}

                def _resolve_one(text):
                    key = str(text).lower().strip()
                    m = val_map.get(key)
                    if m is None:
                        # prefix match: "класс 9" → "класс 9. прочие опасные вещества..."
                        for vk, ve in val_map.items():
                            if vk.startswith(key):
                                m = ve
                                break
                    return {"value": m["value"], "dict_id": m["id"]} if m else {"value": str(text), "dict_id": 0}

                if is_coll:
                    # Normalise to list regardless of whether AI returned str or list
                    items = v if isinstance(v, list) else [v]
                    resolved_merged[k] = [_resolve_one(t) for t in items if t not in (None, "")]
                else:
                    m = val_map.get(str(v).lower().strip())
                    resolved_merged[k] = {"value": m["value"], "dict_id": m["id"]} if m else v
            else:
                resolved_merged[k] = v
        merged = resolved_merged

    import logging
    logging.info(
        "[enrich-params] mp=%s code=%s attrs=%d rules=%d ai=%d total=%d ai_error=%s",
        mp, req.code, len(attributes), len(rules_params), len(ai_params), len(merged),
        ai_error or "none"
    )
    return {
        "ok":          True,
        "params":      merged,
        "rules":       rules_params,
        "ai_params":   ai_params,
        "filled":      len(merged),
        "ai_error":    ai_error or None,
        "specs_found": format_for_ai(extracted) or None,
        "debug": {
            "attrs_loaded":  len(attributes),
            "rules_filled":  len(rules_params),
            "ai_filled":     len(ai_params),
            "merged_total":  len(merged),
            "api_key_set":   bool(api_key),
        },
    }

# ─── Video ────────────────────────────────────────────────────

class VideoBuildRequest(YMKeys):
    code: str
    duration: int = 15

@app.post("/api/video/build")
async def api_video_build(req: VideoBuildRequest):
    ms = MoySkladClient(req.ms_token)
    product = await ms.get_product_data(req.code)
    if not product.get("ok"):
        return {"ok": False, "error": product.get("error","МС: товар не найден")}
    user_images = (answers_store.get(req.code, {}) or {}).get("user_images") or []
    mc_images   = product.get("images") or []
    images = (user_images + mc_images)[:10] if user_images else mc_images
    if not images:
        return {"ok": False, "error": "Нет фото: ни в МС, ни загруженных пользователем"}
    duration = max(5, min(60, int(req.duration or 15)))
    r = await video_builder.build_slideshow(req.code, images, req.ms_token, duration=duration)
    if not r.get("ok"):
        return r
    url = _video_public_url(req.code)
    if req.code not in answers_store:
        answers_store[req.code] = {}
    if answers_store[req.code].get("video_source") != "user":
        answers_store[req.code]["video_url"] = url
        answers_store[req.code]["video_source"] = "generated"
    return {"ok": True, "url": url, "size_mb": r["size_mb"], "duration": r["duration"], "frames": r["frames"]}

@app.delete("/api/video/{code}")
async def api_video_delete(code: str):
    ok = video_builder.delete_video(code)
    if answers_store.get(code, {}).get("video_url"):
        del answers_store[code]["video_url"]
    return {"ok": ok}

@app.get("/api/video/{code}")
async def api_video_info(code: str):
    info = video_builder.video_info(code)
    if not info:
        return {"ok": False, "exists": False}
    return {"ok": True, "exists": True, "url": _video_public_url(code), "size_mb": info["size_mb"]}

# ─── File uploads ─────────────────────────────────────────────

UPLOADS_DIR = os.path.join(os.path.dirname(__file__), "media", "uploads")
os.makedirs(UPLOADS_DIR, exist_ok=True)

ALLOWED_IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp"}
ALLOWED_VIDEO_EXT = {".mp4", ".mov", ".m4v"}
MAX_IMAGE_BYTES   = 10 * 1024 * 1024
MAX_VIDEO_BYTES   = 100 * 1024 * 1024

def _safe_code(code: str) -> str:
    s = "".join(c for c in (code or "") if c.isalnum() or c in "-_")
    if not s:
        raise HTTPException(status_code=400, detail="Некорректный код товара")
    return s

def _req_base_url(request: Request) -> str:
    """Return base URL respecting X-Forwarded-Proto (Cloudflare/reverse-proxy sets this)."""
    base = str(request.base_url).rstrip("/")
    proto = request.headers.get("x-forwarded-proto", "")
    if proto == "https" and base.startswith("http://"):
        base = "https://" + base[7:]
    return base

def _upload_url(code: str, filename: str, base_url: str = "") -> str:
    path = f"/media/uploads/{code}/{filename}"
    effective_base = (PUBLIC_BASE_URL or base_url).strip().rstrip("/")
    return f"{effective_base}{path}" if effective_base else path

class PhotosDownloadRequest(BaseModel):
    code: str
    ms_token: str = ""

@app.post("/api/photos/download")
async def api_photos_download(req: PhotosDownloadRequest, request: Request):
    """Download photos from MoySklad to local disk. Organized as media/uploads/{article}/orig/img_N.jpg"""
    code     = req.code.strip()
    ms_token = (req.ms_token or os.getenv("MS_TOKEN", "")).strip()
    if not code:
        return {"ok": False, "error": "code required"}
    ms = MoySkladClient(ms_token)
    product = await ms.get_product_data(code)
    if not product.get("ok"):
        return {"ok": False, "error": product.get("error", "МС: не найден")}
    _req_base = _req_base_url(request)
    urls, warns = await _prepare_images(ms_token, product, {}, code, subfolder="orig", base_url=_req_base)
    article = product.get("article") or code
    folder_key = "".join(c for c in article if c.isalnum() or c in "-_") or code
    disk_path = os.path.join(UPLOADS_DIR, folder_key, "orig")
    return {
        "ok": True,
        "code": code,
        "article": article,
        "downloaded": len(urls),
        "disk_path": disk_path,
        "urls": urls,
        "warnings": warns,
    }

@app.post("/api/upload/image")
async def api_upload_image(code: str, files: List[UploadFile] = File(...)):
    safe = _safe_code(code)
    dst  = os.path.join(UPLOADS_DIR, safe)
    os.makedirs(dst, exist_ok=True)
    urls_out = []
    ts = int(asyncio.get_event_loop().time() * 1000)
    for i, f in enumerate(files or []):
        ext = os.path.splitext((f.filename or ""))[1].lower()
        if ext not in ALLOWED_IMAGE_EXT:
            continue
        data = await f.read()
        if not data or len(data) > MAX_IMAGE_BYTES:
            continue
        name  = f"img_{ts}_{i}{ext}"
        fpath = os.path.join(dst, name)
        with open(fpath, "wb") as out:
            out.write(data)
        urls_out.append(_upload_url(safe, name))
    if not urls_out:
        return {"ok": False, "error": "Не удалось сохранить ни одного файла (формат/размер)"}
    if safe not in answers_store:
        answers_store[safe] = {}
    existing = list(answers_store[safe].get("user_images") or [])
    answers_store[safe]["user_images"] = existing + urls_out
    return {"ok": True, "urls": urls_out, "all": answers_store[safe]["user_images"]}

@app.post("/api/upload/video")
async def api_upload_video(code: str, file: UploadFile = File(...)):
    safe = _safe_code(code)
    ext  = os.path.splitext((file.filename or ""))[1].lower()
    if ext not in ALLOWED_VIDEO_EXT:
        return {"ok": False, "error": f"Формат не поддерживается: {ext}"}
    data = await file.read()
    if not data:
        return {"ok": False, "error": "Пустой файл"}
    if len(data) > MAX_VIDEO_BYTES:
        return {"ok": False, "error": f"Файл больше 100 МБ ({len(data)//1024//1024} МБ)"}
    dst   = os.path.join(UPLOADS_DIR, safe)
    os.makedirs(dst, exist_ok=True)
    fpath = os.path.join(dst, f"user_video{ext}")
    with open(fpath, "wb") as out:
        out.write(data)
    url = _upload_url(safe, f"user_video{ext}")
    if safe not in answers_store:
        answers_store[safe] = {}
    answers_store[safe]["video_url"]    = url
    answers_store[safe]["video_source"] = "user"
    return {"ok": True, "url": url, "size_mb": round(len(data)/1024/1024, 2)}

# ─── Internal helpers ─────────────────────────────────────────

async def _ai_enrich(code, product, category_id, ym, bh_specs):
    if code in _AI_ENRICH_CACHE:
        return _AI_ENRICH_CACHE[code]
    if not category_id:
        return None
    try:
        ym_params = await get_category_parameters_cached(ym, category_id)
    except Exception:
        ym_params = []
    try:
        result = await ai_enrich_product(product, ym_params, bh_specs)
    except Exception as e:
        return {"error": f"ai_enrich: {str(e)[:200]}", "brand":"", "description":"", "parameter_values":[]}
    _AI_ENRICH_CACHE[code] = result
    return result

async def _enrich_from_bh(code, product, category_id, ym):
    if code in _BH_CACHE:
        return _BH_CACHE[code]
    name = product.get("name", "")
    if not name:
        return None

    # Use brand (from MS attr "Бренд") and article for precise English query
    attrs = product.get("attributes") or {}
    brand   = attrs.get("Бренд") or product.get("brand", "")
    article = product.get("article", "")

    try:
        bh = await get_bh_data(name, brand=brand, article=article)
    except Exception as e:
        return {"error": f"bh_playwright: {str(e)[:200]}"}

    if not bh or not bh.get("found"):
        result = {"found": False}
        _BH_CACHE[code] = result
        return result

    result = {
        "found":       True,
        "url":         bh.get("url", ""),
        "title":       bh.get("title", ""),
        "mfr_number":  bh.get("mfr_number", ""),
        "brand_en":    bh.get("brand_en", ""),
        "specs":       bh.get("specs", {}),
        "specs_count": bh.get("specs_count", 0),
    }
    _BH_CACHE[code] = result
    return result

async def _build_category_params_data(ym, category_id, code=""):
    raw = await get_category_parameters_cached(ym, str(category_id))
    if not raw:
        return None
    manual_map: Dict[str, Any] = {}
    ai_map:  Dict[int, Dict[str, Any]] = {}
    if code:
        manual_map = (answers_store.get(code, {}) or {}).get("params_values", {}) or {}
        ai = _AI_ENRICH_CACHE.get(code) or {}
        for m in (ai.get("parameter_values") or []):
            pid = m.get("param_id")
            if isinstance(pid, int):
                ai_map[pid] = m
    groups = {k: {"title": v["title"], "score_max": v["score_max"],
                  "params": [], "filled": 0, "total": 0} for k, v in _GROUP_META.items()}
    for p in raw:
        pid = p.get("id")
        if not isinstance(pid, int):
            continue
        rec_types = p.get("recommendationTypes") or []
        group = rec_types[0] if rec_types else "OTHER"
        if group not in groups:
            group = "OTHER"
        vals    = p.get("values") or []
        allowed = [{"id": v.get("id"), "value": v.get("value") or v.get("name")}
                   for v in vals if isinstance(v, dict) and (v.get("value") or v.get("name"))]
        current_value, source, confidence = "", None, None
        manual_val = manual_map.get(f"ym_{pid}")
        if manual_val not in (None, "", []):
            current_value, source = manual_val, "manual"
        elif pid in ai_map:
            m = ai_map[pid]
            current_value, source, confidence = m.get("value",""), "ai", m.get("confidence")
        item = {"id": pid, "name": p.get("name",""), "type": p.get("type","STRING"),
                "required": bool(p.get("required")), "unit": p.get("unit",""),
                "description": p.get("description",""), "multivalue": bool(p.get("multivalue")),
                "allowCustomValues": bool(p.get("allowCustomValues")),
                "allowed_values": allowed,
                "current_value": current_value, "source": source, "confidence": confidence}
        groups[group]["params"].append(item)
        groups[group]["total"] += 1
        if current_value not in (None, "", []):
            groups[group]["filled"] += 1
    total  = sum(g["total"]  for g in groups.values())
    filled = sum(g["filled"] for g in groups.values())
    return {"ok": True, "category_id": str(category_id),
            "category_name": get_ym_categories().get(str(category_id), {}).get("name",""),
            "total": total, "filled": filled, "groups": groups}

def _ym_warnings(p, saved):
    w = []
    if p["images_count"] == 0:
        w.append("Нет фотографий в МойСклад")
    if not p["has_description"] and not saved.get("description"):
        w.append("Описание отсутствует — будет сгенерировано минимальное")
    if not p.get("has_price"):
        w.append("Цена 'Для ЯМ (FotoToad)' не задана — используется основная цена")
    if not p.get("has_dims"):
        w.append("Габариты не заданы в МС")
    if not (p.get("brand") or saved.get("brand")):
        w.append("Бренд не определён")
    return w

def _ozon_warnings(p, saved):
    w = []
    if p["images_count"] == 0:
        w.append("Нет фотографий в МойСклад")
    if not p["has_description"] and not saved.get("description"):
        w.append("Описание отсутствует")
    if not (p.get("price_ozon") or p.get("price_main")):
        w.append("Цена не задана")
    return w

def _now_iso():
    from datetime import datetime
    return datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")

def _load_low_rated():
    try:
        with open(_LOW_RATED_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, list) else []
    except Exception:
        return []

def _append_low_rated(entry):
    items = _load_low_rated()
    items = [x for x in items if x.get("offer_id") != entry.get("offer_id")]
    items.append(entry)
    try:
        with open(_LOW_RATED_FILE, "w", encoding="utf-8") as f:
            json.dump(items, f, ensure_ascii=False, indent=2)
    except Exception:
        pass

def _load_errors() -> dict:
    try:
        with open(_ERRORS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except Exception:
        return {}

def _save_errors(data: dict):
    try:
        with open(_ERRORS_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


class ErrorAddBody(BaseModel):
    code: str
    article: str = ""
    name: str = ""
    error: str = ""

@app.get("/api/errors")
async def get_errors():
    data = _load_errors()
    items = sorted(data.values(), key=lambda x: x.get("added", ""), reverse=True)
    return {"ok": True, "items": items, "total": len(items)}

@app.post("/api/errors/add")
async def add_error(body: ErrorAddBody):
    data = _load_errors()
    data[body.code] = {
        "code":    body.code,
        "article": body.article,
        "name":    body.name[:120] if body.name else "",
        "error":   body.error or "Не найден на B&H Photo Video",
        "added":   _now_iso(),
    }
    _save_errors(data)
    return {"ok": True}

@app.delete("/api/errors/{code}")
async def delete_error(code: str):
    data = _load_errors()
    data.pop(code, None)
    _save_errors(data)
    return {"ok": True}

@app.post("/api/errors/clear")
async def clear_errors():
    _save_errors({})
    return {"ok": True}


def _video_public_url(code):
    safe = "".join(c for c in code if c.isalnum() or c in "-_")
    path = f"/media/videos/{safe}.mp4"
    return f"{PUBLIC_BASE_URL}{path}" if PUBLIC_BASE_URL else path

# ═══════════════════════════════════════════════════════════════
# ─── Wildberries ───────────────────────────────────────────────
# ═══════════════════════════════════════════════════════════════



# ── WB image helpers ──────────────────────────────────────────────────────────

async def _download_bytes(url: str, auth_header: str = None) -> bytes:
    headers = {}
    if auth_header:
        headers["Authorization"] = auth_header
    try:
        async with _httpx.AsyncClient(timeout=30, follow_redirects=True) as c:
            r = await c.get(url, headers=headers)
            if r.status_code == 200:
                return r.content
    except Exception:
        pass
    return b""


def _add_wb_border(img_bytes: bytes, border_pct: float = 0.08) -> bytes:
    """Resize image to square with white border (WB photo requirements)."""
    if not _HAS_PILLOW or not img_bytes:
        return img_bytes
    try:
        img = _PILImage.open(io.BytesIO(img_bytes)).convert("RGB")
        w, h = img.size
        canvas_size = max(w, h, 900)
        inner = int(canvas_size * (1 - 2 * border_pct))
        ratio = min(inner / w, inner / h)
        nw, nh = int(w * ratio), int(h * ratio)
        resized = img.resize((nw, nh), _PILImage.LANCZOS)
        canvas = _PILImage.new("RGB", (canvas_size, canvas_size), (255, 255, 255))
        canvas.paste(resized, ((canvas_size - nw) // 2, (canvas_size - nh) // 2))
        buf = io.BytesIO()
        canvas.save(buf, "JPEG", quality=92)
        return buf.getvalue()
    except Exception:
        return img_bytes


async def _prepare_images(
    ms_token: str, product: dict, saved: dict, code: str,
    subfolder: str = "proc", border_pct: float = 0.0,
    base_url: str = "",
) -> tuple[list, list]:
    """Download product images from MoySklad to local disk, return public URLs.

    Photos are saved to media/uploads/{article}/{subfolder}/img_{i}.jpg
    (falls back to code if article unavailable).
    base_url is used when PUBLIC_BASE_URL env var is not set — pass request.base_url.
    """
    user_images = saved.get("user_images") or []
    ms_images   = product.get("images") or []
    all_raw = (user_images + ms_images) if user_images else ms_images
    all_raw = all_raw[:10]

    # Organise folder by article (human-readable) with code as fallback
    article = product.get("article") or ""
    folder_key = "".join(c for c in article if c.isalnum() or c in "-_") if article else ""
    if not folder_key:
        folder_key = "".join(c for c in code if c.isalnum() or c in "-_")
    dst = os.path.join(UPLOADS_DIR, folder_key, subfolder)
    os.makedirs(dst, exist_ok=True)

    ms_auth = f"Bearer {ms_token}" if ms_token else None
    result: list = []
    warnings: list = []
    downloaded = 0

    for i, url in enumerate(all_raw):
        try:
            filename  = f"img_{i}.jpg"
            filepath  = os.path.join(dst, filename)

            if not os.path.exists(filepath):
                if "media/uploads" in url:
                    rel = url.split("media/uploads/")[-1]
                    disk_path = os.path.join(UPLOADS_DIR, rel)
                    try:
                        raw = open(disk_path, "rb").read()
                    except Exception:
                        raw = b""
                else:
                    need_auth = "api.moysklad.ru" in url or "moysklad.ru" in url
                    raw = await _download_bytes(url, ms_auth if need_auth else None)

                if not raw:
                    warnings.append(f"Не удалось скачать фото {i+1}: {url[:80]}")
                    continue
                downloaded += 1
                processed = _add_wb_border(raw, border_pct) if border_pct > 0 else raw
                with open(filepath, "wb") as f:
                    f.write(processed)
            else:
                downloaded += 1

            pub = _upload_url(folder_key, f"{subfolder}/{filename}", base_url=base_url)
            if pub:
                result.append(pub)
        except Exception as e:
            warnings.append(f"Ошибка обработки фото {i+1}: {e}")
            continue

    effective_base = (PUBLIC_BASE_URL or base_url).rstrip("/")
    if not effective_base and result:
        warnings.append(
            "PUBLIC_BASE_URL не задан в .env — маркетплейс не сможет скачать фотографии. "
            "Укажите публичный URL сервера в .env: PUBLIC_BASE_URL=http://your-server.com"
        )
        return [], warnings

    return result, warnings

@app.post("/api/wb/test")
async def wb_test(k: WBKeys):
    wb = WildberriesClient(k.wb_api_key)
    return await wb.test()

@app.post("/api/wb/categories/refresh")
async def wb_categories_refresh(k: WBKeys):
    wb = WildberriesClient(k.wb_api_key)
    result = await wb.get_categories()
    if not result.get("ok"):
        return result
    cats = result.get("categories", {})
    try:
        with open(_WB_CATS_FILE, "w", encoding="utf-8") as f:
            json.dump(cats, f, ensure_ascii=False, indent=2)
    except Exception as e:
        return {"ok": False, "error": f"Не удалось сохранить: {e}"}
    return {"ok": True, "count": len(cats), "message": f"Сохранено {len(cats)} категорий WB"}

@app.get("/api/wb/categories")
async def wb_list_categories():
    try:
        with open(_WB_CATS_FILE, "r", encoding="utf-8") as f:
            cats = json.load(f)
        # Восстанавливаем родительские p_* узлы из данных листьев если их нет
        missing_parents = {}
        for v in cats.values():
            pid = v.get("parent_id")
            if pid and pid not in cats and pid not in missing_parents:
                path = v.get("path", [])
                parent_name = path[0] if path else pid
                missing_parents[pid] = {
                    "id": pid, "int_id": int(pid[2:]) if pid.startswith("p_") else 0,
                    "name": parent_name, "parent_id": None,
                    "path": [parent_name], "has_children": True, "is_leaf": False,
                }
        if missing_parents:
            cats.update(missing_parents)
        return {"ok": True, "count": len(cats), "categories": cats}
    except Exception:
        return {"ok": False, "error": "Категории WB не загружены. Нажмите «Обновить категории WB».", "categories": {}}

class WBCharBody(BaseModel):
    wb_api_key: str
    subject_id: int

@app.post("/api/wb/preview")
async def wb_preview(req: WBUploadRequest):
    ms = MoySkladClient(req.ms_token)
    results = []
    for code in [c.strip() for c in req.codes if c.strip()]:
        p = await ms.get_product_data(code)
        if not p["ok"]:
            results.append({"code": code, "error": p["error"], "action": "SKIP"})
            continue
        saved = answers_store.get(code, {})
        results.append({
            "code": code,
            "article": p.get("article", ""),
            "name": p["name"],
            "images_count": p["images_count"],
            "subject_id": saved.get("wb_subject_id"),
            "category_name": saved.get("wb_category_name", ""),
            "action": "UPLOAD",
            "warnings": _wb_warnings(p, saved),
        })
        await asyncio.sleep(0.1)
    up  = sum(1 for r in results if "error" not in r)
    err = sum(1 for r in results if "error" in r)
    return {"results": results, "summary": {"total": len(results), "upload": up, "error": err}}

@app.post("/api/wb/category/characteristics")
async def wb_category_characteristics(body: WBCharBody):
    wb = WildberriesClient(body.wb_api_key)
    return await wb.get_characteristics(body.subject_id)

@app.post("/api/wb/offer/detail-preview")
async def wb_offer_detail_preview(req: WBUploadRequest):
    ms = MoySkladClient(req.ms_token)
    wb = WildberriesClient(req.wb_api_key)
    try:
        with open(_WB_CATS_FILE, "r", encoding="utf-8") as f:
            wb_cats = json.load(f)
    except Exception:
        wb_cats = {}
    results = []
    for code in [c.strip() for c in req.codes if c.strip()]:
        p = await ms.get_product_data(code)
        if not p["ok"]:
            results.append({"ok": False, "code": code, "error": p.get("error", "Не найден")})
            continue
        saved       = answers_store.get(code, {})
        subject_id  = saved.get("wb_subject_id")
        subject_key = saved.get("wb_category_key") or (f"s_{subject_id}" if subject_id else "")
        cat_entry   = wb_cats.get(subject_key) or {}
        cat_name    = " › ".join(cat_entry.get("path") or [cat_entry.get("name", "")]) if cat_entry else ""
        card        = wb.build_card(p, saved)
        price_wb    = extract_wb_price(p)

        # Определяем источник цены WB (регистронезависимо)
        prices = p.get("prices", {})
        prices_lower = {k.lower(): k for k in prices}
        wb_price_source = "Основная цена"
        for _pn in ["Для WB (FotoToad)", "для WB (FotoToad)", "Для ВБ (FotoToad)", "для ВБ (FotoToad)", "Для WB", "для WB"]:
            real_key = prices_lower.get(_pn.lower())
            if real_key and prices.get(real_key):
                wb_price_source = real_key
                break

        # Пробуем найти существующую карточку на WB
        wb_existing = None
        wb_exists = False
        vendor_code = p.get("code") or p.get("article") or code
        try:
            ex_r = await wb.get_card_by_vendor_code(vendor_code)
            if ex_r.get("ok"):
                wb_exists = True
                wb_card = ex_r.get("card") or {}
                wb_vrnt = ex_r.get("variant") or {}
                wb_existing = {
                    "nm_id":        wb_card.get("nmID"),
                    "imt_id":       wb_card.get("imtID"),
                    "subject_id":   wb_card.get("subjectID"),
                    "subject_name": wb_card.get("subjectName", ""),
                    "title":        wb_vrnt.get("title", ""),
                    "brand":        wb_vrnt.get("brand", ""),
                    "description":  wb_vrnt.get("description", ""),
                    "characteristics": wb_vrnt.get("characteristics") or [],
                    "photos_count": len(wb_vrnt.get("photos") or []),
                    "fuzzy":        ex_r.get("fuzzy", False),
                }
        except Exception:
            pass

        results.append({
            "ok": True, "code": code,
            "card": card,
            "subject_id": subject_id,
            "subject_key": subject_key,
            "category_name": cat_name,
            "weight_kg": p.get("weight_kg"),
            "price_wb": price_wb,
            "price_wb_source": wb_price_source,
            "wb_exists": wb_exists,
            "wb_existing": wb_existing,
            "warnings": _wb_warnings(p, saved),
        })
        await asyncio.sleep(0.1)
    return {"ok": True, "results": results}

def _wb_warnings(p: dict, saved: dict) -> list:
    w = []
    if not saved.get("wb_subject_id"):
        w.append("Не выбрана категория WB")
    if p["images_count"] == 0:
        w.append("Нет фотографий")
    if not p["has_description"] and not saved.get("description"):
        w.append("Нет описания")
    if not extract_wb_price(p):
        w.append("Цена не задана (устанавливается через WB Seller)")
    return w

@app.post("/api/wb/upload/stream")
async def wb_upload_stream(req: WBUploadRequest, request: Request):
    codes = [c.strip() for c in req.codes if c.strip()]
    _req_base = _req_base_url(request)

    async def generate():
        ms = MoySkladClient(req.ms_token)
        wb = WildberriesClient(req.wb_api_key)
        total = len(codes)
        done = ok_count = err_count = 0

        def evt(type_: str, **kw):
            return f"data: {json.dumps({'type': type_, **kw}, ensure_ascii=False)}\n\n"

        yield evt("start", total=total, dry_run=req.dry_run, mode="DRY RUN" if req.dry_run else "LIVE")

        for i, code in enumerate(codes):
            yield evt("progress", current=i+1, total=total, code=code,
                      percent=int(i/total*100), status="reading")
            yield evt("log", level="info", msg=f"[{i+1}/{total}] Код: {code} → читаем из МойСклад...")

            product = await ms.get_product_data(code)
            if not product["ok"]:
                yield evt("log", level="error", msg=f"  ✗ {product['error']}")
                yield evt("item", code=code, success=False, error=product["error"])
                err_count += 1; done += 1
                yield evt("progress", current=done, total=total, code=code,
                          percent=int(done/total*100), status="error")
                continue

            name  = product["name"]
            yield evt("log", level="info", msg=f"  ✓ Найден: «{name}»")
            saved = answers_store.get(code, {})
            for w in _wb_warnings(product, saved):
                yield evt("log", level="warn", msg=f"  ⚠ {w}")

            if req.dry_run:
                yield evt("log", level="info", msg=f"  → [DRY RUN] код: {code}")
                yield evt("item", code=code, success=True, dry_run=True,
                          name=name, article=code)
                ok_count += 1
            else:
                yield evt("progress", current=i+1, total=total, code=code,
                          percent=int((i+0.5)/total*100), status="processing")
                yield evt("log", level="info", msg="  → Обрабатываем фотографии для WB...")
                wb_photos, img_warns = await _prepare_images(
                    req.ms_token, product, saved, code, subfolder="wb_proc", border_pct=0.08,
                    base_url=_req_base,
                )
                for w in img_warns:
                    yield evt("log", level="warn", msg=f"  ⚠ {w}")
                product = dict(product)
                product["images"] = wb_photos
                if wb_photos:
                    yield evt("log", level="info",
                              msg=f"  ✓ Фото обработано: {len(wb_photos)} шт. | URL: {wb_photos[0][:60]}…")
                else:
                    yield evt("log", level="warn",
                              msg="  ⚠ Фото недоступны — карточка будет без фотографий. "
                                  "Проверьте PUBLIC_BASE_URL в .env")

                yield evt("progress", current=i+1, total=total, code=code,
                          percent=int((i+0.7)/total*100), status="uploading")
                action_word = "Обновляем" if req.force_update else "Загружаем на"
                yield evt("log", level="info", msg=f"  → {action_word} WB...")

                if req.force_update:
                    result = await wb.force_update(product, saved)
                else:
                    result = await wb.upload(product, saved)

                if result["ok"]:
                    action = "Обновлено" if (result.get("updated") or req.force_update) else "Создано"
                    yield evt("log", level="success", msg=f"  ✓ {action} на WB! Код: {result.get('vendor_code','')}")
                    yield evt("item", code=code, success=True, name=name, article=code)
                    ok_count += 1
                else:
                    err = result.get("error", "Неизвестная ошибка")
                    yield evt("log", level="error", msg=f"  ✗ Ошибка WB: {err}")
                    yield evt("item", code=code, success=False, error=err)
                    err_count += 1

            done += 1
            yield evt("progress", current=done, total=total, code=code,
                      percent=int(done/total*100), status="done")
            await asyncio.sleep(0.3)

        yield evt("finish", success=ok_count, errors=err_count, total=total, dry_run=req.dry_run)

    return StreamingResponse(generate(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

if os.path.exists(FRONTEND):
    app.mount("/static", StaticFiles(directory=FRONTEND), name="static")

app.mount("/media", StaticFiles(directory=os.path.dirname(video_builder.MEDIA_DIR)), name="media")

@app.on_event("startup")
async def _startup_cleanup():
    async def _loop():
        while True:
            try:
                video_builder.cleanup_old_videos()
            except Exception:
                pass
            await asyncio.sleep(3600)
    asyncio.create_task(_loop())
