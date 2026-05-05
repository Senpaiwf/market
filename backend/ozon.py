# ozon.py
# Ozon Seller API v3 — авторизация через Client-Id + Api-Key
# Документация: https://docs.ozon.ru/api/seller/

import httpx, asyncio
from typing import List

BASE = "https://api-seller.ozon.ru"

# Категории Ozon для фото/видео (description_category_id)
OZON_CATEGORIES = {
    "camera":     {"id": 93726111, "type_id": 10001,  "name": "Фотоаппараты"},
    "lens":       {"id": 93726112, "type_id": 10002,  "name": "Объективы"},
    "drone":      {"id": 93726120, "type_id": 10003,  "name": "Квадрокоптеры"},
    "gimbal":     {"id": 93726125, "type_id": 10004,  "name": "Стабилизаторы"},
    "microphone": {"id": 93726130, "type_id": 10005,  "name": "Микрофоны"},
    "lighting":   {"id": 93726135, "type_id": 10006,  "name": "Осветители"},
    "tripod":     {"id": 93726140, "type_id": 10007,  "name": "Штативы"},
    "storage":    {"id": 93726150, "type_id": 10008,  "name": "Карты памяти"},
}

def _to_ozon_values(val) -> list:
    """Convert stored ozon_attrs value to Ozon API values list.

    Handles three formats:
    - list of {value, dict_id} dicts  → is_collection with real IDs
    - single {value, dict_id} dict    → single dict attribute
    - plain string (comma-separated)  → backward compat, dict_id=0
    """
    if isinstance(val, list):
        result = []
        for item in val:
            if isinstance(item, dict):
                result.append({
                    "dictionary_value_id": int(item.get("dict_id") or 0),
                    "value": str(item.get("value", "")),
                })
            elif item not in (None, ""):
                result.append({"dictionary_value_id": 0, "value": str(item)})
        return result
    if isinstance(val, dict):
        return [{"dictionary_value_id": int(val.get("dict_id") or 0), "value": str(val.get("value", ""))}]
    # Plain string — may be comma-separated for legacy is_collection entries
    parts = [v.strip() for v in str(val).split(",") if v.strip()]
    return [{"dictionary_value_id": 0, "value": v} for v in parts]


class OzonClient:
    def __init__(self, client_id: str, api_key: str):
        # Ozon требует оба заголовка одновременно
        self.h = {
            "Client-Id": str(client_id).strip(),
            "Api-Key": api_key.strip(),
            "Content-Type": "application/json",
        }

    async def _post(self, path: str, body: dict) -> dict:
        url = f"{BASE}{path}"
        for i in range(3):
            try:
                async with httpx.AsyncClient(timeout=30) as c:
                    r = await c.post(url, headers=self.h, json=body)
                    if r.status_code == 200:
                        return {"ok": True, "data": r.json()}
                    if r.status_code == 429:
                        await asyncio.sleep(2**i); continue
                    try: err = r.json()
                    except: err = {"message": r.text[:200]}
                    msgs = {
                        401: "Неверный Api-Key Ozon (401). Проверьте ключ.",
                        403: "Нет прав (403). Проверьте Client-Id и Api-Key.",
                        404: (
                            "Ozon вернул 404 — скорее всего у API-ключа нет нужных прав. "
                            "Зайдите в Ozon Seller → Настройки → API ключи → "
                            "включите роли «Контент» и «Товары»."
                        ),
                    }
                    if r.status_code in msgs:
                        return {"ok": False, "error": msgs[r.status_code]}
                    msg = err.get("message") or err.get("code") or str(err)[:200]
                    return {"ok": False, "error": f"HTTP {r.status_code}: {msg}"}
            except Exception as e:
                if i == 2: return {"ok": False, "error": f"Сетевая ошибка: {e}"}
                await asyncio.sleep(1)
        return {"ok": False, "error": "Превышено число попыток"}

    async def _get(self, path: str) -> dict:
        url = f"{BASE}{path}"
        try:
            async with httpx.AsyncClient(timeout=20) as c:
                r = await c.get(url, headers=self.h)
                if r.status_code == 200: return {"ok": True, "data": r.json()}
                try: err = r.json()
                except: err = {"message": r.text[:200]}
                if r.status_code == 401: return {"ok": False, "error": "Неверный Api-Key (401)"}
                return {"ok": False, "error": f"HTTP {r.status_code}: {err.get('message','')[:200]}"}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    async def test(self) -> dict:
        """Проверка подключения — запрашиваем список товаров (limit=1)"""
        r = await self._post("/v3/product/list", {"filter": {}, "last_id": "", "limit": 1})
        if r["ok"]:
            total = r["data"].get("result", {}).get("total", 0)
            return {"ok": True, "message": f"Подключено. Товаров в кабинете: {total}"}
        return r

    async def check_exists(self, offer_id: str) -> bool:
        """Проверить есть ли товар на Ozon по артикулу"""
        r = await self._post("/v3/product/list", {
            "filter": {"offer_id": [offer_id]},
            "last_id": "", "limit": 10
        })
        if r["ok"]:
            return len(r["data"].get("result", {}).get("items", [])) > 0
        return False

    def build_item(self, product: dict, saved: dict = None) -> dict:
        """Строит словарь товара для загрузки или превью."""
        saved = saved or {}
        cat_id  = saved.get("ozon_category_id") or 93726111
        type_id = saved.get("ozon_type_id")
        price   = int(product.get("price_ozon") or product.get("price_main") or 0)
        weight_g = max(1, int((product.get("weight_kg") or 0.001) * 1000))
        item = {
            "description_category_id": int(cat_id),
            "name":      (product.get("name") or "")[:255],
            "offer_id":  product.get("code") or product.get("article", ""),
            "barcode":   product.get("barcode", ""),
            "price":     str(price),
            "old_price": str(int(price * 1.1)) if price else "0",
            "vat":         "0",
            "weight":      weight_g,
            "weight_unit": "g",
            "images":      product.get("images", [])[:15],
            "attributes":  self._build_attrs_from_saved(product, saved),
        }
        if type_id:
            item["type_id"] = int(type_id)
        # MoySklad stores dimensions in mm — Ozon also needs mm
        dims = product.get("dims_cm") or {}
        depth_mm  = dims.get("depth_mm")
        width_mm  = dims.get("width_mm")
        height_mm = dims.get("height_mm")
        if depth_mm:  item["depth"]  = max(1, int(depth_mm))
        if width_mm:  item["width"]  = max(1, int(width_mm))
        if height_mm: item["height"] = max(1, int(height_mm))
        if any(k in item for k in ("depth", "width", "height")):
            item["dimension_unit"] = "mm"
        return item

    async def upload(self, product: dict, saved: dict = None) -> dict:
        """Создать/обновить карточку товара на Ozon (v3)."""
        item = self.build_item(product, saved)
        r = await self._post("/v3/product/import", {"items": [item]})
        if r["ok"]:
            task_id = r["data"].get("result", {}).get("task_id")
            return {"ok": True, "task_id": task_id, "offer_id": product.get("article", "")}
        return r

    async def get_upload_status(self, task_id: int) -> dict:
        """Проверить статус загрузки товара."""
        r = await self._post("/v2/product/import/info", {"task_id": task_id})
        if not r["ok"]: return r
        items = r["data"].get("result", {}).get("items", [])
        if not items: return {"ok": True, "status": "processing"}
        item = items[0]
        errors = item.get("errors", [])
        status = item.get("status", "")
        return {
            "ok": True,
            "status": status,
            "errors": [e.get("message", "") for e in errors],
        }

    async def get_categories_tree(self) -> dict:
        """Получить дерево категорий Ozon и вернуть в плоском виде.
        Узлы-типы (листья с type_id) хранятся под составным ключом '{cat_id}_{type_id}',
        чтобы избежать коллизии, когда несколько типов имеют одинаковый description_category_id.
        """
        r = await self._post("/v1/description-category/tree", {"language": "RU"})
        if not r["ok"]:
            return r
        cats: dict = {}
        def _walk(node, parent_key, path, parent_cat_id=None):
            if not isinstance(node, dict):
                return
            cat_id  = node.get("description_category_id")
            type_id = node.get("type_id")
            # Ozon type-nodes: have type_name+type_id but NO description_category_id
            # Use parent's cat_id so the composite key is correct
            cat_name = node.get("category_name") or node.get("type_name", "")
            if not cat_id:
                if type_id and parent_cat_id:
                    cat_id = parent_cat_id  # inherit from parent category
                else:
                    return
            current_path = path + [cat_name]
            # API returns children="" for leaf type-nodes (string, not list)
            raw_children = node.get("children")
            children = raw_children if isinstance(raw_children, list) else []
            my_key = f"{cat_id}_{type_id}" if type_id else str(cat_id)
            cats[my_key] = {
                "id": my_key,
                "desc_cat_id": cat_id,
                "type_id": type_id,
                "name": cat_name,
                "parent_id": parent_key,
                "path": current_path,
                "has_children": bool(children),
            }
            for child in children:
                _walk(child, my_key, current_path, cat_id)
        for root in (r["data"].get("result") or []):
            _walk(root, None, [])
        return {"ok": True, "categories": cats}

    async def get_category_attributes(self, description_category_id: int, type_id: int = None) -> dict:
        """Получить атрибуты категории Ozon"""
        body: dict = {"description_category_id": int(description_category_id), "language": "RU"}
        if type_id:
            body["type_id"] = int(type_id)
        r = await self._post("/v1/description-category/attribute", body)
        if not r["ok"]:
            return r
        attrs = []
        for a in (r["data"].get("result") or []):
            attrs.append({
                "id": a.get("id"),
                "name": a.get("name", ""),
                "description": a.get("description", ""),
                "type": a.get("type", "String"),
                "required": bool(a.get("is_required")),
                "is_collection": bool(a.get("is_collection")),
                "dictionary_id": a.get("dictionary_id", 0),
                "max_value_count": a.get("max_value_count", 1),
            })
        return {"ok": True, "attributes": attrs, "total": len(attrs)}

    async def get_product_info_by_offer(self, offer_id: str) -> dict:
        """Получить полные данные о товаре на Ozon по seller offer_id (артикулу)."""
        r = await self._post("/v3/product/info/list", {
            "offer_id": [offer_id],
            "product_id": [],
            "sku": [],
        })
        if not r["ok"]:
            return r
        items = (r["data"].get("result") or {}).get("items") or []
        if not items:
            return {"ok": False, "error": "Товар не найден на Ozon"}
        item = items[0]
        # Вытаскиваем sku для запроса рейтинга
        sources = item.get("sources") or []
        sku = sources[0].get("sku") if sources else None
        return {"ok": True, "item": item, "sku": sku}

    async def get_content_rating_by_skus(self, skus: list) -> dict:
        """Получить рейтинг контента по sku (marketplace internal ID)."""
        if not skus:
            return {"ok": False, "error": "Нет sku"}
        r = await self._post("/v1/product/rating-by-sku", {
            "skus": [str(s) for s in skus]
        })
        if not r["ok"]:
            return r
        return {"ok": True, "ratings": r["data"].get("items") or []}

    async def get_attribute_values(
        self, description_category_id: int, type_id: int, attribute_id: int
    ) -> dict:
        """Получить допустимые значения словарного атрибута Ozon (с пагинацией)."""
        values = []
        last_id = 0
        for _ in range(50):  # max 50 страниц × 200 = 10 000 значений
            body = {
                "attribute_id": attribute_id,
                "description_category_id": description_category_id,
                "type_id": type_id,
                "last_value_id": last_id,
                "limit": 200,
                "language": "RU",
            }
            r = await self._post("/v1/description-category/attribute/values", body)
            if not r["ok"]:
                break
            batch = r["data"].get("result") or []
            for v in batch:
                values.append({"id": v.get("id"), "value": v.get("value", ""), "info": v.get("info", "")})
            if not r["data"].get("has_next") or not batch:
                break
            last_id = batch[-1].get("id", 0)
            await asyncio.sleep(0.05)
        return {"ok": True, "values": values, "total": len(values)}

    async def get_products(self, limit: int = 20) -> dict:
        """Список товаров в кабинете Ozon"""
        r = await self._post("/v3/product/list", {
            "filter": {}, "last_id": "", "limit": limit
        })
        if not r["ok"]: return r
        items = r["data"].get("result", {}).get("items", [])
        return {"ok": True, "total": len(items), "products": items}

    def _build_attrs_from_saved(self, p: dict, saved: dict) -> List[dict]:
        """Формирует список атрибутов для отправки в Ozon.
        Приоритет: кастомные атрибуты из модального окна (ozon_attrs) →
        затем дефолтные (бренд, модель, страна, описание).
        Атрибуты: 85=Бренд, 9048=Название модели, 4174=Страна производства, 4191=Описание

        Поддерживаемые форматы в ozon_attrs:
          "oz_123": "text"                           — свободный текст (dict_id=0)
          "oz_123": {"value": "text", "dict_id": 5}  — справочное значение с ID
          "oz_123": [{"value": "v1", "dict_id": 1}, ...] — мультизначение (is_collection)
        """
        result: List[dict] = []
        attrs       = p.get("attributes") or {}
        ozon_attrs  = saved.get("ozon_attrs") or {}
        seen_ids: set = set()

        # ── Кастомные атрибуты из модального окна ──────────────
        for key, val in ozon_attrs.items():
            if val is None or val == "" or val == []:
                continue
            try:
                attr_id = int(str(key).replace("oz_", ""))
            except ValueError:
                continue
            values = _to_ozon_values(val)
            if values:
                result.append({"id": attr_id, "complex_id": 0, "values": values})
                seen_ids.add(attr_id)

        # ── Дефолтные атрибуты (если не перекрыты пользователем) ──
        brand = saved.get("brand") or p.get("brand") or attrs.get("Бренд", "")
        if brand and 85 not in seen_ids:
            result.append({"id": 85, "complex_id": 0,
                           "values": [{"dictionary_value_id": 0, "value": brand}]})
            seen_ids.add(85)

        model = attrs.get("Модель") or attrs.get("Model") or (p.get("name") or "")[:100]
        if model and 9048 not in seen_ids:
            result.append({"id": 9048, "complex_id": 0,
                           "values": [{"dictionary_value_id": 0, "value": model}]})
            seen_ids.add(9048)

        country = attrs.get("Страна производства") or attrs.get("Страна", "")
        if country and 4174 not in seen_ids:
            result.append({"id": 4174, "complex_id": 0,
                           "values": [{"dictionary_value_id": 0, "value": country}]})
            seen_ids.add(4174)

        desc = saved.get("description") or p.get("description") or \
               f"{p.get('name', '')}. Профессиональное оборудование."
        if 4191 not in seen_ids:
            result.append({"id": 4191, "complex_id": 0,
                           "values": [{"value": desc[:5000]}]})

        return result

    def _build_attrs(self, p: dict, cat: dict) -> List[dict]:
        return self._build_attrs_from_saved(p, {})
