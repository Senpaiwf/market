# backend/moysklad.py
# Поиск товара по КОДУ (поле code в МойСклад).
# Код — уникальный номер товара, отображается в печатных формах.
# Путь: GET /entity/product?filter=code=XXXXX

import unicodedata
import httpx, asyncio
from typing import Optional, List

BASE = "https://api.moysklad.ru/api/remap/1.2"

# Название типа цены для Яндекс.Маркет в вашем МС
YM_PRICE_NAME   = "Для ЯМ (FotoToad)"
OZON_PRICE_NAME = "Для Ozon (FotoToad)"
WB_PRICE_NAME   = "Для WB (FotoToad)"


class MoySkladClient:
    def __init__(self, token: str):
        self.token = token
        self.h = {
            "Authorization": f"Bearer {token}",
            "Accept-Encoding": "gzip",
            "Content-Type": "application/json",
        }

    async def _get(self, path: str, params: dict = None) -> Optional[dict]:
        for i in range(3):
            try:
                async with httpx.AsyncClient(timeout=25) as c:
                    r = await c.get(f"{BASE}{path}", headers=self.h, params=params)
                    if r.status_code == 200:
                        return r.json()
                    if r.status_code == 429:
                        await asyncio.sleep(2 ** i)
                        continue
                    if r.status_code == 401:
                        return {"_err": 401, "_msg": "Неверный токен МойСклад (401 Unauthorized)"}
                    return {"_err": r.status_code, "_msg": r.text[:300]}
            except Exception as e:
                if i == 2:
                    return {"_err": 0, "_msg": str(e)}
                await asyncio.sleep(1)
        return None

    # ─── Проверка подключения ───────────────────────────────
    async def test(self) -> dict:
        d = await self._get("/entity/product", params={"limit": 1})
        if not d:
            return {"ok": False, "error": "Нет ответа от МойСклад"}
        if "_err" in d:
            return {"ok": False, "error": d["_msg"]}
        total = d.get("meta", {}).get("size", 0)
        return {"ok": True, "total": total,
                "message": f"Подключено. Товаров в базе: {total}"}

    # ─── Поиск по КОДУ ─────────────────────────────────────
    async def find_by_code(self, code: str) -> dict:
        """
        Ищет товар по полю 'code' (Код) — уникальный номер товара.
        В МС: Товары → карточка товара → поле 'Код'.
        Endpoint: GET /entity/product?filter=code=ЗНАЧЕНИЕ
        """
        d = await self._get("/entity/product", params={
            "filter": f"code={code}",
            "limit": 1,
        })
        if not d:
            return {"ok": False, "error": "Нет ответа от МойСклад"}
        if "_err" in d:
            return {"ok": False, "error": d["_msg"]}
        rows = d.get("rows", [])
        if not rows:
            return {"ok": False, "error": f"Товар с кодом '{code}' не найден в МойСклад"}
        return {"ok": True, "product": rows[0]}

    async def get_product_folder(self, code: str) -> dict:
        """Возвращает папку товара (для определения категории по скрипту)."""
        d = await self._get("/entity/product", params={
            "filter": f"code={code}",
            "limit": 1,
            "expand": "productFolder",
        })
        if not d or "_err" in d:
            return {"ok": False, "error": "Нет ответа от МойСклад"}
        rows = d.get("rows", [])
        if not rows:
            return {"ok": False, "error": f"Товар с кодом '{code}' не найден"}
        p = rows[0]
        folder_raw = p.get("productFolder") or {}
        path_name = folder_raw.get("pathName", "")
        folder_name = path_name.split("/")[-1].strip() if path_name else ""
        return {
            "ok": True,
            "name": p.get("name", ""),
            "folder_name": folder_name,
            "folder_path": path_name,
        }

    # ─── Полные данные по коду ──────────────────────────────
    async def get_product_data(self, code: str) -> dict:
        """
        Главный метод. По коду возвращает всё нужное для выгрузки на ЯМ:
        - Название, описание, бренд
        - Цену "Для ЯМ (FotoToad)" — если такой тип цены создан в МС
        - Габариты в сантиметрах (МС хранит в мм → делим на 10)
        - Вес в кг
        - Фотографии (URL)
        - Характеристики/атрибуты
        - Артикул (поле article из МС)
        """
        found = await self.find_by_code(code)
        if not found["ok"]:
            return found

        p = found["product"]
        product_id = p.get("id", "")

        # Параллельно получаем фото и характеристики
        images = await self._get_images(product_id)
        attrs = self._extract_attrs(p)
        prices = self._extract_prices(p)

        name = p.get("name", "")
        description = p.get("description", "")
        article = p.get("article", "")  # Артикул — поле article (отдельное от кода)

        # ── Габариты: МС хранит в МИЛЛИМЕТРАХ ──
        # Смотрим сначала стандартные поля, затем кастомные атрибуты
        # ЯМ принимает в САНТИМЕТРАХ → делим на 10
        attrs_lower = {k.lower(): val for k, val in attrs.items()}

        def _mm(standard_val, *attr_names) -> float:
            v = standard_val or 0
            if not v:
                for name in attr_names:
                    raw = attrs_lower.get(name.lower(), "")
                    try:
                        v = float(str(raw).replace(",", ".")) if raw else 0
                    except ValueError:
                        continue
                    if v:
                        break
            return float(v)

        width_mm  = _mm(p.get("width"),  "Ширина")
        height_mm = _mm(p.get("height"), "Высота")
        depth_mm  = _mm(p.get("depth"),  "Глубина", "Длина")

        dims_cm = {
            "width_cm":  round(width_mm  / 10, 1) if width_mm  else None,
            "height_cm": round(height_mm / 10, 1) if height_mm else None,
            "depth_cm":  round(depth_mm  / 10, 1) if depth_mm  else None,
            "width_mm":  width_mm,
            "height_mm": height_mm,
            "depth_mm":  depth_mm,
        }

        # Вес МС хранит в кг
        weight_kg = p.get("weight", 0) or 0

        # ── Цены ──
        price_main = prices.get("Основная цена") or (list(prices.values())[0] if prices else 0)

        price_ym = prices.get(YM_PRICE_NAME)
        if not price_ym:
            price_ym = price_main
            price_ym_source = "Основная цена (цена 'Для ЯМ (FotoToad)' не найдена)"
        else:
            price_ym_source = YM_PRICE_NAME

        # Try several spelling variants of the Ozon price type name
        _ozon_candidates = [OZON_PRICE_NAME, "Для ОЗОН (FotoToad)", "Для Ozon", "Для ОЗОН"]
        price_ozon = None
        price_ozon_source = "Основная цена"
        for _pname in _ozon_candidates:
            _pv = prices.get(_pname)
            if _pv and _pv > 0:
                price_ozon = _pv
                price_ozon_source = _pname
                break
        if not price_ozon:
            price_ozon = price_main

        # ── Бренд ──
        brand = (
            attrs.get("Бренд") or
            attrs.get("brand") or
            attrs.get("Brand") or
            _extract_brand(name)
        )

        barcode = (p.get("barcodes") or [{}])[0].get("value", "")

        return {
            "ok": True,
            # Идентификаторы
            "code": code,
            "product_id": product_id,
            "article": article,       # Артикул (будет использован как offerId на ЯМ)
            "ms_name": p.get("name", ""),
            "barcode": barcode,
            # Контент
            "name": name,
            "description": description,
            "brand": brand,
            # Финансы
            "prices": prices,
            "price_ym": price_ym,
            "price_ym_source": price_ym_source,
            "price_ozon": price_ozon,
            "price_ozon_source": price_ozon_source,
            "price_main": price_main,
            # Физические параметры
            "weight_kg": weight_kg,
            "dims_cm": dims_cm,
            # Медиа
            "images": images,
            "images_count": len(images),
            # Атрибуты/характеристики из МС
            "attributes": attrs,
            # Статус заполненности
            "has_name":        bool(name),
            "has_description": len(description) >= 50,
            "has_price":       bool(price_ym and price_ym > 0),
            "has_images":      len(images) > 0,
            "has_dims":        all(v for v in [dims_cm["width_cm"], dims_cm["height_cm"], dims_cm["depth_cm"]]),
            "has_brand":       bool(brand),
        }

    # ─── Изображения ────────────────────────────────────────
    async def _get_images(self, product_id: str) -> list:
        d = await self._get(
            f"/entity/product/{product_id}/images",
            params={"limit": 10}
        )
        if not d or "_err" in d:
            return []
        result = []
        for img in d.get("rows", []):
            # miniature — уменьшенная версия с авторизацией
            # Для ЯМ нам нужен публичный URL — берём downloadHref если есть
            url = (
                img.get("miniature", {}).get("href", "") or
                img.get("meta", {}).get("href", "")
            )
            if url:
                result.append(url)
        return result

    # ─── Цены ───────────────────────────────────────────────
    def _extract_prices(self, p: dict) -> dict:
        """
        Возвращает словарь всех типов цен.
        МС хранит цены в КОПЕЙКАХ → делим на 100.
        Пример: {"Основная цена": 15000.0, "Для ЯМ (FotoToad)": 14500.0}
        """
        result = {}
        for sp in p.get("salePrices", []):
            name = sp.get("priceType", {}).get("name", "Основная цена")
            value = sp.get("value", 0)
            if value and value > 0:
                result[name] = value / 100  # копейки → рубли
        return result

    # ─── Атрибуты ───────────────────────────────────────────
    def _extract_attrs(self, p: dict) -> dict:
        result = {}
        for a in p.get("attributes", []):
            k = a.get("name", "")
            # Normalize Unicode (NFD→NFC) and strip whitespace so lookups like
            # attrs.get("Бренд") always work regardless of how the API encodes the key.
            k = unicodedata.normalize("NFC", k).strip()
            v = a.get("value", "")
            if isinstance(v, dict):
                v = v.get("name") or v.get("value", "")
            if k and v:
                result[k] = str(v)
        return result


def _extract_brand(name: str) -> str:
    """Извлекает бренд из первых слов названия"""
    known = ["Sony", "Canon", "Nikon", "DJI", "Fujifilm", "Panasonic", "Rode",
             "Shure", "Sennheiser", "Godox", "Aputure", "Zhiyun", "Manfrotto",
             "Sigma", "Tamron", "Zeiss", "Leica", "GoPro", "Insta360",
             "Blackmagic", "Atomos", "SmallRig", "Tilta"]
    n = name.lower()
    for b in known:
        if b.lower() in n:
            return b
    return name.split()[0] if name else ""


# Ключевые слова для определения категории по названию товара.
# Матчится с именами категорий из загруженного дерева _YM_CATEGORIES.
_CATEGORY_KEYWORDS = [
    # (keywords_in_product, keyword_in_ym_category_name)
    (["lens", "объектив", "mm f/", "f/1.", "f/2.", "f/4", "50mm", "85mm", "24mm", "35mm", "70-200"], "объектив"),
    (["mavic", "phantom", "drone", "дрон", "квадро", "fpv"], "квадрокоптер"),
    (["gimbal", "стабилизатор", "ronin", "rs3", "rs4", "crane", "zhiyun"], "стабилизатор"),
    (["microphone", "микрофон", "rode wireless", "shure mv", "sennheiser mke", "xlr"], "микрофон"),
    (["видеосвет", "led panel", "softbox", "softlight", "bi-color", "bicolor", "осветитель",
      "godox ", "aputure", "nanlite", "накамерный свет"], "осветительн"),
    (["tripod", "штатив", "monopod", "монопод", "manfrotto", "gitzo"], "штатив"),
    (["memory card", "sd card", "карта памяти", "cfexpress", "xqd"], "карт"),
    (["camera bag", "camera case", "чехол для камер", "сумка для камер", "рюкзак для камер", "кофр", "lowepro"], "сумк"),
    (["камер", "фотоаппарат", "mirrorless", "беззеркал", "зеркальн", "cinema camera",
      "camcorder", "видеокамер"], "фотоаппарат"),
]


async def detect_category(name: str, attrs: dict) -> tuple:
    """
    Определяет ID категории ЯМ на основе названия товара и атрибутов.
    Возвращает (category_id: str, confidence: float).
    confidence = 0.0 означает «не найдено».
    """
    from yandex_market import get_ym_categories
    cats = get_ym_categories()
    if not cats:
        return "", 0.0

    text = (name + " " + " ".join(str(v) for v in attrs.values())).lower()

    for product_keywords, ym_keyword in _CATEGORY_KEYWORDS:
        matched = [k for k in product_keywords if k in text]
        if matched:
            # Уверенность пропорциональна доле совпавших ключевых слов
            keyword_score = min(1.0, 0.5 + len(matched) / len(product_keywords) * 0.5)
            best = None
            best_depth = -1
            for cat in cats.values():
                if ym_keyword in cat["name"].lower():
                    depth = len(cat.get("path", []))
                    if depth > best_depth and not cat.get("has_children", False):
                        best = cat
                        best_depth = depth
            if best:
                return str(best["id"]), round(min(0.95, keyword_score), 2)
            # Фолбэк — не листовой узел
            for cat in cats.values():
                if ym_keyword in cat["name"].lower():
                    return str(cat["id"]), round(min(0.65, keyword_score * 0.7), 2)

    return "", 0.0
