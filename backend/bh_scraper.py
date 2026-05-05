# backend/bh_scraper.py
# Парсинг B&H Photo Video (bhphotovideo.com) для автоподстановки характеристик.
# B&H защищён Akamai Bot Manager → используем curl_cffi с TLS-fingerprint Chrome.

import asyncio
import re
from typing import Optional
from urllib.parse import quote

try:
    from curl_cffi.requests import AsyncSession
    _HAS_CURL_CFFI = True
except ImportError:
    _HAS_CURL_CFFI = False

from bs4 import BeautifulSoup


UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
BASE = "https://www.bhphotovideo.com"


async def _fetch(url: str) -> Optional[str]:
    """GET через curl_cffi с эмуляцией Chrome TLS. Возвращает HTML или None."""
    if not _HAS_CURL_CFFI:
        return None
    for attempt in range(2):
        try:
            async with AsyncSession() as s:
                r = await s.get(
                    url,
                    impersonate="chrome124",
                    timeout=25,
                    headers={"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"},
                    allow_redirects=True,
                )
                if r.status_code == 200:
                    return r.text
                if r.status_code in (403, 429):
                    await asyncio.sleep(2 * (attempt + 1))
                    continue
                return None
        except Exception:
            await asyncio.sleep(1)
    return None


async def search_bh(query: str) -> Optional[dict]:
    """
    Ищет товар на B&H по названию (бренд + артикул).
    Возвращает {url, title} или None.
    """
    if not query or not query.strip():
        return None
    url = f"{BASE}/c/search?Ntt={quote(query.strip())}&N=0"
    html = await _fetch(url)
    if not html:
        return None
    soup = BeautifulSoup(html, "html.parser")

    # Поиск первой ссылки на карточку товара (/c/product/...)
    for a in soup.select("a[href*='/c/product/']"):
        href = a.get("href") or ""
        if "/c/product/" not in href:
            continue
        if href.startswith("/"):
            href = BASE + href
        # очищаем query-string и якорь
        clean_url = re.sub(r"[?#].*$", "", href)
        # Пытаемся извлечь title
        title = a.get("title") or a.get("aria-label") or a.get_text(strip=True) or ""
        return {"url": clean_url, "title": title[:200]}

    return None


async def fetch_specs(product_url: str) -> dict:
    """
    Парсит страницу /specs товара → {spec_name: value, ...}.
    """
    if not product_url:
        return {}
    url = product_url.rstrip("/")
    if not url.endswith("/specs"):
        url = url + "/specs"
    html = await _fetch(url)
    if not html:
        return {}
    soup = BeautifulSoup(html, "html.parser")

    specs: dict = {}

    # Вариант 1: table.specTable (классическая вёрстка)
    for row in soup.select("table.specTable tr"):
        key_el = row.select_one("th, .specName, [data-selenium='specName']")
        val_el = row.select_one("td, .specValue, [data-selenium='specValue']")
        if key_el and val_el:
            k = key_el.get_text(" ", strip=True)
            v = val_el.get_text(" ", strip=True)
            if k and v:
                specs[k] = v

    # Вариант 2: блоки с data-selenium (новая вёрстка SPA)
    if not specs:
        names = soup.select("[data-selenium='specName']")
        values = soup.select("[data-selenium='specValue']")
        for n, v in zip(names, values):
            k = n.get_text(" ", strip=True)
            val = v.get_text(" ", strip=True)
            if k and val:
                specs[k] = val

    # Вариант 3: dl/dt/dd
    if not specs:
        for dl in soup.select("dl"):
            dts = dl.select("dt")
            dds = dl.select("dd")
            for dt, dd in zip(dts, dds):
                k = dt.get_text(" ", strip=True)
                v = dd.get_text(" ", strip=True)
                if k and v:
                    specs[k] = v

    return _normalize_units(specs)


# ─── Конвертация единиц ──────────────────────────────────────
_RE_LB = re.compile(r"([\d.]+)\s*lb", re.I)
_RE_OZ = re.compile(r"([\d.]+)\s*oz", re.I)
_RE_IN = re.compile(r"([\d.]+)\s*(?:\"|in\b|inch)", re.I)
_RE_FT = re.compile(r"([\d.]+)\s*ft", re.I)
_RE_F = re.compile(r"([\d.]+)\s*[°º]?\s*F\b")


def _normalize_units(specs: dict) -> dict:
    """Конвертирует lb→кг, in→см, ft→см, °F→°C в значениях spec."""
    out = {}
    for k, v in specs.items():
        s = str(v)
        # lb → kg
        m = _RE_LB.search(s)
        if m:
            try:
                kg = round(float(m.group(1)) * 0.4536, 2)
                s = s + f" ({kg} кг)"
            except ValueError:
                pass
        # in → cm
        m = _RE_IN.search(s)
        if m:
            try:
                cm = round(float(m.group(1)) * 2.54, 1)
                s = s + f" ({cm} см)"
            except ValueError:
                pass
        # ft → cm
        m = _RE_FT.search(s)
        if m and "ft" in s.lower():
            try:
                cm = round(float(m.group(1)) * 30.48, 1)
                s = s + f" ({cm} см)"
            except ValueError:
                pass
        # °F → °C
        m = _RE_F.search(s)
        if m:
            try:
                c = round((float(m.group(1)) - 32) * 5 / 9, 1)
                s = s + f" ({c}°C)"
            except ValueError:
                pass
        out[k] = s
    return out


# Единая точка входа — вызывается из main.py
async def get_bh_data(product_name: str) -> Optional[dict]:
    """
    По названию товара: ищет на B&H и парсит характеристики.
    Возвращает {url, title, specs} или None.
    """
    found = await search_bh(product_name)
    if not found:
        return None
    specs = await fetch_specs(found["url"])
    return {
        "url": found["url"],
        "title": found["title"],
        "specs": specs,
        "specs_count": len(specs),
    }
