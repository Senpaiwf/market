# wb.py — Wildberries Content API v2
# Документация: https://openapi.wildberries.ru/

import httpx, asyncio, math
from typing import List

WB_PRICE_NAME = "Для WB (FotoToad)"


_WB_PRICE_NAMES = [
    WB_PRICE_NAME,           # "Для WB (FotoToad)"
    "для WB (FotoToad)",     # lowercase вариант
    "Для ВБ (FotoToad)",
    "для ВБ (FotoToad)",
    "Для WB", "для WB",
    "Для ВБ", "для ВБ",
]

def extract_wb_price(product: dict) -> int:
    """Возвращает цену WB как целое число (всегда в большую сторону — math.ceil).
    Ищет нечувствительно к регистру первой буквы."""
    prices = product.get("prices") or {}
    # Нормализуем ключи для case-insensitive поиска
    prices_lower = {k.lower(): v for k, v in prices.items()}
    raw = 0
    for _pn in _WB_PRICE_NAMES:
        _pv = prices.get(_pn) or prices_lower.get(_pn.lower())
        if _pv and _pv > 0:
            raw = _pv
            break
    if not raw:
        raw = product.get("price_main") or 0
    return math.ceil(raw) if raw else 0

BASE = "https://content-api.wildberries.ru"

class WildberriesClient:
    def __init__(self, api_key: str):
        token = api_key.strip()
        # WB Content API v2 requires standard Bearer auth
        if not token.lower().startswith("bearer "):
            token = f"Bearer {token}"
        self.h = {
            "Authorization": token,
            "Content-Type": "application/json",
        }

    async def _get(self, path: str, params: dict = None) -> dict:
        url = f"{BASE}{path}"
        try:
            async with httpx.AsyncClient(timeout=30) as c:
                r = await c.get(url, headers=self.h, params=params or {})
                if r.status_code == 200:
                    return {"ok": True, "data": r.json()}
                if r.status_code == 401:
                    return {"ok": False, "error": "Неверный API ключ WB (401). Проверьте токен."}
                try: err = r.json()
                except: err = {"errorText": r.text[:200]}
                msg = err.get("errorText") or err.get("message") or str(err)[:200]
                return {"ok": False, "error": f"HTTP {r.status_code}: {msg}"}
        except Exception as e:
            return {"ok": False, "error": f"Ошибка подключения: {e}"}

    async def _post(self, path: str, body) -> dict:
        url = f"{BASE}{path}"
        try:
            async with httpx.AsyncClient(timeout=30) as c:
                r = await c.post(url, headers=self.h, json=body)
                if r.status_code in (200, 201):
                    return {"ok": True, "data": r.json()}
                if r.status_code == 401:
                    return {"ok": False, "error": "Неверный API ключ WB (401). Проверьте токен."}
                try: err = r.json()
                except: err = {"errorText": r.text[:200]}
                msg = err.get("errorText") or err.get("message") or str(err)[:200]
                return {"ok": False, "error": f"HTTP {r.status_code}: {msg}"}
        except Exception as e:
            return {"ok": False, "error": f"Ошибка подключения: {e}"}

    async def test(self) -> dict:
        """Проверка подключения — читаем список родительских категорий."""
        r = await self._get("/content/v2/object/parent/all", {"locale": "ru"})
        if r["ok"]:
            total = len(r["data"].get("data") or [])
            return {"ok": True, "message": f"WB подключено. Родительских категорий: {total}"}
        return r

    async def get_categories(self) -> dict:
        """Двухуровневое дерево: родительская категория → предмет (subjectID)."""
        parents_r = await self._get("/content/v2/object/parent/all", {"locale": "ru"})
        if not parents_r["ok"]:
            return parents_r

        subjects: list = []
        limit, offset = 1000, 0
        for _ in range(60):
            subs_r = await self._get("/content/v2/object/all", {
                "locale": "ru", "limit": limit, "offset": offset
            })
            if not subs_r["ok"]:
                break
            batch = subs_r["data"].get("data") or []
            subjects.extend(batch)
            if len(batch) < limit:
                break
            offset += limit
            await asyncio.sleep(0.1)

        cats: dict = {}
        for p in (parents_r["data"].get("data") or []):
            pid = p.get("parentID")
            if not pid:
                continue
            key  = f"p_{pid}"
            name = p.get("name", "")
            cats[key] = {
                "id": key, "int_id": pid, "name": name,
                "parent_id": None, "path": [name],
                "has_children": True, "is_leaf": False,
            }

        for s in subjects:
            sid = s.get("subjectID")
            pid = s.get("parentID")
            if not sid:
                continue
            parent_key  = f"p_{pid}" if pid else None
            parent_name = s.get("parentName", "")
            name        = s.get("subjectName", "")
            cats[f"s_{sid}"] = {
                "id": f"s_{sid}", "int_id": sid, "name": name,
                "parent_id": parent_key,
                "path": [parent_name, name] if parent_name else [name],
                "has_children": False, "is_leaf": True,
            }

        return {"ok": True, "categories": cats}

    async def get_characteristics(self, subject_id: int) -> dict:
        """Характеристики предмета WB."""
        r = await self._get(f"/content/v2/object/charcs/{subject_id}", {"locale": "ru"})
        if not r["ok"]:
            return r
        chars = []
        for c in (r["data"].get("data") or []):
            chars.append({
                "id":        c.get("charcID"),
                "name":      c.get("name", ""),
                "required":  bool(c.get("required")),
                "unit":      c.get("unitName", ""),
                "type":      c.get("charcType", 1),   # 1=строка, 4=число
                "max_count": c.get("maxCount", 1),
                "is_color":  bool(c.get("isColor")),
                "popular":   bool(c.get("popular")),
            })
        chars.sort(key=lambda c: (not c["required"], not c["popular"], c["name"]))
        return {"ok": True, "characteristics": chars, "total": len(chars)}

    def build_card(self, product: dict, saved: dict = None) -> dict:
        """Строит карточку WB для загрузки."""
        saved = saved or {}
        subject_id  = saved.get("wb_subject_id")
        vendor_code = product.get("code") or product.get("article") or ""
        title       = (product.get("name") or "")[:60]
        desc        = (saved.get("description") or product.get("description", "") or "")[:2000]
        barcode     = product.get("barcode", "") or ""
        weight_kg   = product.get("weight_kg") or 0

        dims = product.get("dims_cm") or {}
        def mm_to_cm(v):
            return max(1, round(v / 10)) if v else 0

        length_cm = mm_to_cm(dims.get("depth_mm", 0))
        width_cm  = mm_to_cm(dims.get("width_mm",  0))
        height_cm = mm_to_cm(dims.get("height_mm", 0))

        chars_saved = saved.get("wb_chars", {})
        characteristics = []
        for k, val in chars_saved.items():
            try:
                char_id = int(str(k).replace("wb_", ""))
            except Exception:
                continue
            if not val:
                continue
            value = val if isinstance(val, list) else [str(val)]
            characteristics.append({"id": char_id, "value": value})

        user_images = saved.get("user_images") or []
        ms_images   = product.get("images") or []
        photos = (user_images + ms_images)[:10] if user_images else ms_images[:10]

        return {
            "subjectID": int(subject_id) if subject_id else 0,
            "variants": [{
                "vendorCode": vendor_code,
                "title": title,
                "description": desc,
                "dimensions": {
                    "length": length_cm,
                    "width":  width_cm,
                    "height": height_cm,
                    "isValidDimensions": bool(length_cm and width_cm and height_cm),
                    **({"weightBrutto": round(float(weight_kg), 3)} if weight_kg else {}),
                },
                "characteristics": characteristics,
                "sizes": [{
                    "techSize": "0",
                    "wbSize":   "",
                    "skus":     [barcode] if barcode else [],
                }],
                "photos": photos,
            }],
        }

    async def get_card_by_vendor_code(self, vendor_code: str) -> dict:
        """Получить существующую карточку WB по vendorCode (артикулу поставщика)."""
        r = await self._post("/content/v2/get/cards/list", {
            "settings": {
                "cursor": {"limit": 10},
                "filter": {
                    "textSearch": vendor_code,
                    "withPhoto": -1,
                }
            }
        })
        if not r["ok"]:
            return r
        cards = (r["data"].get("cards") or [])
        # Ищем точное совпадение по vendorCode в variants
        for card in cards:
            for v in (card.get("variants") or []):
                if v.get("vendorCode") == vendor_code:
                    return {"ok": True, "card": card, "variant": v}
        # Если точного нет — возвращаем первый результат как подходящий
        if cards:
            v0 = (cards[0].get("variants") or [{}])[0]
            return {"ok": True, "card": cards[0], "variant": v0, "fuzzy": True}
        return {"ok": False, "error": "Карточка не найдена на WB"}

    async def upload(self, product: dict, saved: dict = None) -> dict:
        """Загрузить карточку товара на WB. Если карточка уже есть — обновляет."""
        card = self.build_card(product, saved)
        if not card.get("subjectID"):
            return {"ok": False, "error": "Не выбрана категория WB (subjectID не задан)"}
        vendor_code = card["variants"][0]["vendorCode"]

        r = await self._post("/content/v2/cards/upload", [card])

        # WB вернул 400 «vendor code is used in other cards» — карточка уже есть, обновляем
        if not r["ok"] and "400" in r.get("error", "") and "vendor code" in r.get("error", "").lower():
            r2 = await self._post("/content/v2/cards/update", [card])
            if r2["ok"]:
                data2 = r2["data"] or {}
                err2 = data2.get("errorText") or ""
                if err2:
                    return {"ok": False, "error": f"[update] {err2}"}
                return {"ok": True, "code": product.get("code", ""), "vendor_code": vendor_code, "updated": True}
            return r2

        if not r["ok"]:
            return r
        data = r["data"] or {}
        err_text = data.get("errorText") or ""
        if err_text:
            return {"ok": False, "error": err_text}
        return {"ok": True, "code": product.get("code", ""), "vendor_code": vendor_code}

    async def force_update(self, product: dict, saved: dict = None) -> dict:
        """Принудительно обновить существующую карточку WB (только /update, без /upload)."""
        card = self.build_card(product, saved)
        if not card.get("subjectID"):
            return {"ok": False, "error": "Не выбрана категория WB (subjectID не задан)"}
        r = await self._post("/content/v2/cards/update", [card])
        if not r["ok"]:
            return r
        data = r["data"] or {}
        err_text = data.get("errorText") or ""
        if err_text:
            return {"ok": False, "error": err_text}
        return {"ok": True, "code": product.get("code", ""), "vendor_code": card["variants"][0]["vendorCode"], "updated": True}
