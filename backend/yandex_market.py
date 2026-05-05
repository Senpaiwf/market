# backend/yandex_market.py
# Яндекс.Маркет Partner API — авторизация через Api-Key
# Заполняет: название, категорию, бренд, артикул, описание,
# фото, габариты (в см), цену, ключевые характеристики

import httpx, asyncio, json, os
from typing import Optional, List

BASE = "https://api.partner.market.yandex.ru"

_CATEGORIES_FILE = os.path.join(os.path.dirname(__file__), "yandex_category.json")

def _load_categories() -> dict:
    try:
        with open(_CATEGORIES_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

_YM_CATEGORIES: dict = _load_categories()

def get_ym_categories() -> dict:
    return _YM_CATEGORIES

def flatten_categories_tree(data) -> dict:
    """Рекурсивно разворачивает дерево категорий ЯМ в плоский словарь.
    Формат: {str(id): {id, name, parent_id, path, has_children}}
    API возвращает: {"status": "OK", "result": {"id": ..., "name": ..., "children": [...]}}
    """
    result = {}
    def _walk(node, parent_id, path):
        if not isinstance(node, dict):
            return
        cat_id = node.get("id")
        cat_name = node.get("name", "")
        if not (cat_id and cat_name):
            return
        current_path = path + [cat_name]
        children = node.get("children", []) or []
        result[str(cat_id)] = {
            "id": cat_id,
            "name": cat_name,
            "parent_id": parent_id,
            "path": current_path,
            "has_children": bool(children),
        }
        for child in children:
            _walk(child, cat_id, current_path)

    # Точка входа: data = {"status": "OK", "result": {root_node}}
    root = data.get("result") if isinstance(data, dict) else data
    _walk(root, None, [])
    return result

def filter_subtree(categories: dict, root_name: str) -> dict:
    """Возвращает подмножество categories — узел с именем root_name и всех его потомков."""
    root_name_low = root_name.lower().strip()
    # Находим корневой узел по имени (case-insensitive, по подстроке)
    root = None
    for cat in categories.values():
        if root_name_low in cat["name"].lower():
            # Выбираем самый "верхний" (короткий path) матч
            if root is None or len(cat["path"]) < len(root["path"]):
                root = cat
    if not root:
        return {}
    root_id = root["id"]
    root_name_exact = root["name"]
    # Всё, у чего в path присутствует root_name_exact на соответствующей глубине
    root_depth = len(root["path"]) - 1  # индекс root в path потомков
    subset = {}
    for key, cat in categories.items():
        p = cat["path"]
        if len(p) > root_depth and p[root_depth] == root_name_exact:
            subset[key] = cat
    return subset

def reload_categories(categories: dict):
    """Обновляет категории в памяти и сохраняет в файл."""
    global _YM_CATEGORIES
    _YM_CATEGORIES = categories
    with open(_CATEGORIES_FILE, "w", encoding="utf-8") as f:
        json.dump(categories, f, ensure_ascii=False, indent=2)

# ═══ Кеш параметров категорий ЯМ ════════════════════════════
# Файл: backend/yandex_category_params_cache.json
# Формат: {"461": {"fetched_at": "2026-04-22T10:00:00", "parameters": [...]}}
import time
_CATEGORY_PARAMS_CACHE_FILE = os.path.join(
    os.path.dirname(__file__), "yandex_category_params_cache.json"
)
_CACHE_TTL_SECONDS = 7 * 24 * 3600  # 7 дней


def _load_params_cache() -> dict:
    try:
        with open(_CATEGORY_PARAMS_CACHE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_params_cache(cache: dict):
    try:
        with open(_CATEGORY_PARAMS_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


_PARAMS_CACHE: dict = _load_params_cache()


async def get_category_parameters_cached(ym_client, category_id: str) -> list:
    """
    Возвращает список параметров категории ЯМ с кешированием на 7 дней.
    category_id — строковый числовой ID.
    """
    if not category_id:
        return []
    key = str(category_id)
    entry = _PARAMS_CACHE.get(key)
    now = time.time()
    if entry and (now - entry.get("ts", 0)) < _CACHE_TTL_SECONDS:
        return entry.get("parameters", []) or []
    # Запрос в API
    r = await ym_client.get_category_parameters(key)
    if not r.get("ok"):
        # Если ошибка — вернём старый кеш если есть
        return entry.get("parameters", []) if entry else []
    params = r.get("data", {}).get("result", {}).get("parameters", []) or []
    _PARAMS_CACHE[key] = {"ts": now, "parameters": params}
    _save_params_cache(_PARAMS_CACHE)
    return params


def merge_categories(new_categories: dict):
    """Добавляет new_categories к текущим _YM_CATEGORIES (с перезаписью по id)."""
    merged = dict(_YM_CATEGORIES)
    merged.update(new_categories)
    reload_categories(merged)
    return merged

# Маппинг числового ID категории → внутренний ключ для YM_KEY_PARAMS
_CATEGORY_ID_TO_KEY = {
    "458": "camera",
    "459": "lens",
    "8354942": "drone",
    "8354943": "gimbal",
    "460": "microphone",
    "461": "lighting",
    "462": "tripod",
    "464": "storage",
    "8354944": "bag",
}

# Ключевые характеристики по категориям
# Те, которые помогают покупателю выбрать товар
YM_KEY_PARAMS = {
    "camera": [
        {"ms_key": "Тип матрицы",         "ym_key": "Тип матрицы"},
        {"ms_key": "Размер матрицы",       "ym_key": "Размер матрицы"},
        {"ms_key": "Разрешение матрицы",   "ym_key": "Разрешение матрицы, МП"},
        {"ms_key": "Байонет",              "ym_key": "Тип байонета"},
        {"ms_key": "Видео",                "ym_key": "Формат записи видео"},
        {"ms_key": "Стабилизация",         "ym_key": "Встроенная стабилизация"},
        {"ms_key": "Страна производства",  "ym_key": "Страна производства"},
        {"ms_key": "Мегапиксели",          "ym_key": "Разрешение матрицы, МП"},
        {"ms_key": "ISO максимальное",     "ym_key": "Максимальное значение ISO"},
        {"ms_key": "Цвет",                 "ym_key": "Цвет"},
    ],
    "lens": [
        {"ms_key": "Фокусное расстояние",  "ym_key": "Фокусное расстояние, мм"},
        {"ms_key": "Диафрагма",            "ym_key": "Минимальная диафрагма"},
        {"ms_key": "Байонет",              "ym_key": "Тип байонета"},
        {"ms_key": "Стабилизация",         "ym_key": "Встроенная стабилизация"},
        {"ms_key": "Страна производства",  "ym_key": "Страна производства"},
        {"ms_key": "Тип объектива",        "ym_key": "Тип объектива"},
    ],
    "drone": [
        {"ms_key": "Время полёта",         "ym_key": "Максимальное время полёта, мин"},
        {"ms_key": "Дальность",            "ym_key": "Дальность управления, км"},
        {"ms_key": "Разрешение камеры",    "ym_key": "Разрешение камеры, МП"},
        {"ms_key": "Максимальная скорость","ym_key": "Максимальная скорость, км/ч"},
    ],
    "gimbal": [
        {"ms_key": "Грузоподъёмность",    "ym_key": "Максимальная нагрузка, кг"},
        {"ms_key": "Число осей",          "ym_key": "Количество осей стабилизации"},
        {"ms_key": "Время работы",        "ym_key": "Время работы от аккумулятора, ч"},
    ],
    "microphone": [
        {"ms_key": "Тип микрофона",       "ym_key": "Тип"},
        {"ms_key": "Подключение",         "ym_key": "Интерфейс подключения"},
        {"ms_key": "Диаграмма",           "ym_key": "Диаграмма направленности"},
        {"ms_key": "Частотный диапазон",  "ym_key": "Диапазон воспроизводимых частот"},
    ],
    "lighting": [
        {"ms_key": "Мощность",            "ym_key": "Мощность, Вт"},
        {"ms_key": "Цветовая температура","ym_key": "Цветовая температура, К"},
        {"ms_key": "CRI",                 "ym_key": "Индекс цветопередачи (CRI)"},
    ],
    "tripod": [
        {"ms_key": "Нагрузка",            "ym_key": "Максимальная нагрузка, кг"},
        {"ms_key": "Высота максимальная", "ym_key": "Максимальная высота, см"},
        {"ms_key": "Материал",            "ym_key": "Материал ног"},
    ],
    "storage": [
        {"ms_key": "Объём",               "ym_key": "Объём памяти, ГБ"},
        {"ms_key": "Скорость чтения",     "ym_key": "Скорость чтения, МБ/с"},
        {"ms_key": "Тип карты",           "ym_key": "Тип карты памяти"},
    ],
}


class YandexMarketClient:
    def __init__(self, api_key: str, campaign_id: str, business_id: str = ""):
        self.campaign_id = str(campaign_id).strip()
        self.business_id = str(business_id).strip()
        self.h = {
            "Api-Key": api_key.strip(),
            "Content-Type": "application/json",
        }
        self._categories = None  # Will hold dict of categories: {id: {'id': id, 'name': name}, ...} or list format

    async def _req(self, method: str, path: str, body: dict = None) -> dict:
        url = f"{BASE}{path}"
        for i in range(3):
            try:
                async with httpx.AsyncClient(timeout=30) as c:
                    r = await c.request(method, url, headers=self.h, json=body)
                    if r.status_code in (200, 201):
                        return {"ok": True, "data": r.json()}
                    if r.status_code == 429:
                        await asyncio.sleep(2 ** i)
                        continue
                    try:
                        err = r.json()
                    except Exception:
                        err = {"text": r.text[:300]}
                    codes = {
                        401: "Неверный Api-Key (401). Проверьте ключ в кабинете ЯМ.",
                        403: "Нет прав (403). Проверьте Campaign ID и права ключа.",
                        404: f"Кампания {self.campaign_id} не найдена (404).",
                    }
                    if r.status_code in codes:
                        return {"ok": False, "error": codes[r.status_code]}
                    errs = err.get("errors", [])
                    msg = errs[0].get("message", str(err)[:200]) if errs else str(err)[:200]
                    return {"ok": False, "error": f"HTTP {r.status_code}: {msg}"}
            except Exception as e:
                if i == 2:
                    return {"ok": False, "error": f"Сетевая ошибка: {e}"}
                await asyncio.sleep(1)
        return {"ok": False, "error": "Превышено число попыток"}

    async def test(self) -> dict:
        r = await self._req("GET", f"/campaigns/{self.campaign_id}")
        if r["ok"]:
            c = r["data"].get("campaign", {})
            return {"ok": True, "campaign": c.get("domain", self.campaign_id)}
        return r

    async def get_categories_tree(self) -> dict:
        """Получает дерево категорий Яндекс.Маркет"""
        return await self._req("POST", "/categories/tree", {})

    async def get_category_parameters(self, category_id: str) -> dict:
        """Получает параметры конкретной категории"""
        return await self._req("POST", f"/category/{category_id}/parameters", {})

    async def validate_offer_state(self, offer_id) -> dict:
        """Полная проверка состояния карточки в кабинете ЯМ.
        offer_id может быть строкой ИЛИ списком строк-кандидатов
        (тогда находим первый существующий).
        """
        candidates = [offer_id] if isinstance(offer_id, str) else list(offer_id or [])
        # Чистим: убираем пустые и дубли с сохранением порядка
        seen = set()
        candidates = [str(c).strip() for c in candidates if c and str(c).strip()]
        candidates = [c for c in candidates if not (c in seen or seen.add(c))]
        primary = candidates[0] if candidates else ""
        result = {
            "offer_id": primary,
            "candidates_tried": candidates,
            "exists": False,
            "status": "NOT_FOUND",
            "is_critical": False,
            "rating": None,
            "card_status": "",
            "missing_fields": [],
            "recommendations": [],
            "color": "⚪ NOT_FOUND",
        }
        if not self.business_id or not candidates:
            result["error"] = "no_business_id_or_offer_id"
            return result

        # 1. Запрашиваем mapping по ВСЕМ кандидатам разом
        m = await self._req(
            "POST",
            f"/businesses/{self.business_id}/offer-mappings",
            {"offerIds": candidates},
        )
        mapping_offer = {}
        found_offer_id = ""
        if m.get("ok"):
            items = (m.get("data", {}).get("result", {}).get("offerMappings") or [])
            for it in items:
                off = it.get("offer", {}) or {}
                oid = off.get("offerId") or ""
                if oid:
                    mapping_offer = off
                    found_offer_id = oid
                    result["exists"] = True
                    result["offer_id"] = oid
                    # Real category and name from YM
                    result["ym_category_id"] = off.get("marketCategoryId")
                    result["ym_name"] = off.get("name", "")
                    break

        if not result["exists"]:
            return result
        offer_id = found_offer_id  # дальше работаем с реально найденным id

        # 2. Проверяем заполнение основных полей оффера
        REQUIRED = [
            ("name", "Название"),
            ("description", "Описание"),
            ("vendor", "Бренд"),
            ("pictures", "Изображения"),
            ("basicPrice", "Цена"),
            ("weightDimensions", "Габариты/вес"),
        ]
        for key, label in REQUIRED:
            v = mapping_offer.get(key)
            if v is None or v == "" or v == [] or v == {}:
                result["missing_fields"].append(label)
            elif key == "description" and isinstance(v, str) and len(v) < 100:
                result["missing_fields"].append("Описание (короче 100 символов)")
            elif key == "pictures" and isinstance(v, list) and len(v) < 1:
                result["missing_fields"].append("Изображения")

        # 3. Запрашиваем offer-cards для рейтинга и рекомендаций
        c = await self._req(
            "POST",
            f"/businesses/{self.business_id}/offer-cards",
            {"offerIds": [offer_id]},
        )
        if c.get("ok"):
            cards = (c.get("data", {}).get("result", {}).get("offerCards") or [])
            if cards:
                card = cards[0]
                result["card_status"] = card.get("cardStatus", "")
                rating = None
                for _rf in ("contentRating", "ratingValue", "rating", "contentRatingLevel", "score"):
                    _rv = card.get(_rf)
                    if _rv is not None:
                        rating = _rv
                        break
                recs = card.get("recommendations") or []
                if rating is None and recs:
                    percents = [r.get("percent") for r in recs if isinstance(r.get("percent"), (int, float))]
                    if percents:
                        rating = int(sum(percents) / len(percents))
                try:
                    result["rating"] = int(rating) if rating is not None else None
                except (TypeError, ValueError):
                    result["rating"] = None
                result["recommendations"] = recs
                result["raw_card"] = {k: v for k, v in card.items() if k not in ("recommendations",)}
                for r in recs:
                    title = r.get("title") or r.get("type")
                    if title and title not in result["missing_fields"]:
                        result["missing_fields"].append(str(title))

        # 4. Финальный статус
        if result["rating"] is not None and result["rating"] < 80:
            result["status"] = "NEED_FIX"
            result["is_critical"] = True
            result["color"] = "🔴 RED"
        elif result["missing_fields"]:
            result["status"] = "NEED_FIX"
            result["is_critical"] = bool(result["missing_fields"])
            result["color"] = "🔴 RED"
        else:
            result["status"] = "OK"
            result["color"] = "🟢 OK"
        return result

    async def get_offer_card_rating(self, offer_id: str) -> dict:
        """Возвращает контентный рейтинг карточки и рекомендации.
        Использует POST /businesses/{businessId}/offer-cards.
        Возвращает: {ok, rating: int|None, status: str, recommendations: [...]}"""
        if not self.business_id or not offer_id:
            return {"ok": False, "error": "no_business_id_or_offer_id", "rating": None}
        r = await self._req(
            "POST",
            f"/businesses/{self.business_id}/offer-cards",
            {"offerIds": [offer_id]},
        )
        if not r.get("ok"):
            return {"ok": False, "error": r.get("error", "request_failed"), "rating": None}
        cards = (r.get("data", {}).get("result", {}).get("offerCards") or [])
        if not cards:
            return {"ok": True, "rating": None, "status": "NO_CARD", "recommendations": []}
        card = cards[0]
        # Пробуем несколько вариантов поля рейтинга (API эволюционирует)
        # Важно: не используем `or`, т.к. он пропускает 0
        rating = None
        for _rf in ("contentRating", "ratingValue", "rating", "contentRatingLevel", "score"):
            _rv = card.get(_rf)
            if _rv is not None:
                rating = _rv
                break
        recs = card.get("recommendations") or []
        if rating is None and recs:
            percents = [r.get("percent") for r in recs if isinstance(r.get("percent"), (int, float))]
            if percents:
                rating = int(sum(percents) / len(percents))
        try:
            rating = int(rating) if rating is not None else None
        except (TypeError, ValueError):
            rating = None
        return {
            "ok": True,
            "rating": rating,
            "status": card.get("cardStatus", ""),
            "recommendations": recs,
        }

    async def check_exists(self, offer_id: str) -> bool:
        if self.business_id:
            r = await self._req("POST",
                f"/businesses/{self.business_id}/offer-mappings",
                {"offerIds": [offer_id]})
            if r["ok"]:
                items = r["data"].get("result", {}).get("offerMappings", [])
                return len(items) > 0
        # Через campaign
        r = await self._req("GET",
            f"/campaigns/{self.campaign_id}/offer-mapping-entries",
            None)
        if r["ok"]:
            entries = r["data"].get("result", {}).get("offerMappingEntries", [])
            return any(
                e.get("offer", {}).get("shopSku") == offer_id
                for e in entries
            )
        return False

    async def upload(self, product: dict, category_key: str,
                     resolved: dict = None) -> dict:
        """
        Загружает оффер на ЯМ.
        resolved — словарь ответов на вопросы (категория, бренд и т.д.)
        """
        offer = self.build_offer(product, category_key, resolved or {})
        if self.business_id:
            path = f"/businesses/{self.business_id}/offer-mappings/update"
            payload = {"offerMappings": [{"offer": offer}]}
        else:
            path = f"/campaigns/{self.campaign_id}/offer-mapping-entries/updates"
            payload = {"offerMappingEntries": [{"offer": offer}]}

        r = await self._req("POST", path, payload)
        if r["ok"]:
            return {"ok": True, "offer_id": offer["offerId"], "offer": offer}
        return r

    def build_offer(self, p: dict, category_id: str, resolved: dict) -> dict:
        """
        Строит объект оффера для ЯМ.
        category_id — числовой ID категории ЯМ в виде строки (например "458").
        resolved может содержать переопределения от пользователя.
        """
        cat_id_str = str(resolved.get("category") or category_id or "458")
        cat = get_ym_categories().get(cat_id_str) or {"id": int(cat_id_str) if cat_id_str.isdigit() else 458, "name": cat_id_str}

        # Артикул — используем как offerId (идентификатор в ЯМ)
        offer_id = p.get("article") or p.get("code", "")

        # Название
        name = resolved.get("name") or p.get("name", "")

        # Описание
        description = resolved.get("description") or p.get("description", "")
        if not description:
            description = f"{name}. Профессиональное фото и видеооборудование."

        # Бренд
        brand = resolved.get("brand") or p.get("brand", "")

        # Цена — берём цену для ЯМ
        price = resolved.get("price") or p.get("price_ym", p.get("price_main", 0))

        # Фото: мс-фото + пользовательские (до 10 всего)
        mc_images = list(p.get("images") or [])
        user_images = list(resolved.get("user_images") or [])
        images = (mc_images + user_images)[:10]

        offer = {
            "offerId": offer_id,
            "name": name[:255],
            "vendor": brand,
            "vendorCode": offer_id,    # Артикул поставщика
            "description": description[:2000],
            "pictures": images,
            "marketCategoryId": cat["id"],
            "basicPrice": {
                "value": int(price),
                "currencyId": "RUR",
                "discountBase": int(price * 1.1),  # 9.09% discount (YM min 5%, formula: (base-price)/base)
            },
        }

        # ── Габариты и вес объединены в weightDimensions ──
        # ЯМ принимает габариты в САНТИМЕТРАХ, вес в КГ.
        dims = p.get("dims_cm", {})
        w = resolved.get("width_cm") or dims.get("width_cm")
        h = resolved.get("height_cm") or dims.get("height_cm")
        d = resolved.get("depth_cm") or dims.get("depth_cm")
        weight = p.get("weight_kg", 0)
        wd = {}
        if w and h and d:
            wd["length"] = float(d)
            wd["width"] = float(w)
            wd["height"] = float(h)
        if weight and weight > 0:
            wd["weight"] = round(float(weight), 3)
        if wd:
            offer["weightDimensions"] = wd

        # ── Ключевые характеристики ──
        params = self._build_params(p, cat_id_str, resolved)
        if params:
            offer["parameterValues"] = params

        # ── Видео (сгенерированное слайдшоу) ──
        video_url = resolved.get("video_url")
        if video_url:
            offer["videos"] = [video_url]

        return offer

    def _build_params(self, p: dict, category_id: str, resolved: dict) -> list:
        """Характеристики ЯМ. API требует числовой parameterId.
        Для enum-параметров отправляем valueId (int), для текстовых — value (str).
        Ключи вида 'ym_<param_id>' из resolved.params_values.
        """
        result = []
        params_values = resolved.get("params_values", {}) or {}
        for key, value in params_values.items():
            if value in (None, "", []):
                continue
            if not (isinstance(key, str) and key.startswith("ym_")):
                continue
            raw_id = key[3:]
            try:
                pid = int(raw_id)
            except ValueError:
                continue
            values = value if isinstance(value, list) else [value]
            for v in values:
                if v in (None, ""):
                    continue
                try:
                    vid = int(float(v))
                    result.append({"parameterId": pid, "valueId": vid})
                except (ValueError, TypeError):
                    result.append({"parameterId": pid, "value": str(v)})
        return result

    def get_questions(self, product: dict, category_id: str) -> list:
        """
        Возвращает список вопросов для пользователя —
        то, что не удалось определить автоматически.
        """
        questions = []
        attrs = product.get("attributes", {})
        category_key = _CATEGORY_ID_TO_KEY.get(str(category_id), "")

        # Вопрос о категории — каскадные селекты (дерево).
        # Фронтенд подгрузит дерево через GET /api/categories и построит каскад.
        cat_info = get_ym_categories().get(str(category_id), {}) if category_id else {}
        questions.append({
            "id": "category",
            "label": "Категория товара на Яндекс.Маркет",
            "type": "cascade_select",
            "value": str(category_id) if category_id else "",
            "path": cat_info.get("path", []),
            "required": True,
            "auto_detected": bool(category_id),
        })

        # Бренд
        if not product.get("brand"):
            questions.append({
                "id": "brand",
                "label": "Бренд товара",
                "type": "text",
                "value": "",
                "required": True,
                "auto_detected": False,
            })

        # Описание — если короткое
        if not product.get("has_description"):
            questions.append({
                "id": "description",
                "label": "Описание товара (отсутствует или слишком короткое в МС)",
                "type": "textarea",
                "value": product.get("description", ""),
                "required": False,
                "auto_detected": False,
            })

        # Габариты — если не заданы в МС
        dims = product.get("dims_cm", {})
        if not dims.get("width_cm"):
            questions.append({
                "id": "dims",
                "label": "Габариты товара",
                "type": "dims",
                "value": {"width_cm": "", "height_cm": "", "depth_cm": ""},
                "hint": "В сантиметрах. В МС габариты не заданы.",
                "required": False,
                "auto_detected": False,
            })

        # Ключевые характеристики категории
        param_defs = YM_KEY_PARAMS.get(category_key, []) if category_key else []
        missing_params = []
        for pd in param_defs:
            if not attrs.get(pd["ms_key"]):
                missing_params.append(pd)

        if missing_params:
            questions.append({
                "id": "params",
                "label": "Характеристики для Яндекс.Маркет",
                "type": "params",
                "params": missing_params,
                "required": False,
                "auto_detected": False,
            })

        return questions
