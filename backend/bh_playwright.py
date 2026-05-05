# backend/bh_playwright.py
# B&H Photo Video product lookup via Playwright + playwright-stealth.
# Replaces bh_firecrawl.py — same get_bh_data() interface, no paid API required.
from __future__ import annotations
import re
import logging
from urllib.parse import quote_plus

from playwright.async_api import async_playwright
from playwright_stealth import Stealth

logger = logging.getLogger(__name__)

_BH_BASE   = "https://www.bhphotovideo.com"
_BH_SEARCH = f"{_BH_BASE}/c/search"

# ─── Unit conversion (same as bh_firecrawl.py) ────────────────
_RE_LB = re.compile(r"([\d.]+)\s*lb", re.I)
_RE_IN = re.compile(r"(?<!/)([\d.]+)\s*(?:\"|in\b|inch)", re.I)
_RE_FT = re.compile(r"([\d.]+)\s*ft", re.I)
_RE_F  = re.compile(r"([\d.]+)\s*[°º]?\s*F\b")


def _normalize_units(specs: dict) -> dict:
    out = {}
    for k, v in specs.items():
        s = str(v)
        m = _RE_LB.search(s)
        if m:
            try:
                kg = round(float(m.group(1)) * 0.4536, 2)
                s = s + f" ({kg} кг)"
            except ValueError:
                pass
        m = _RE_IN.search(s)
        if m:
            try:
                cm = round(float(m.group(1)) * 2.54, 1)
                s = s + f" ({cm} см)"
            except ValueError:
                pass
        m = _RE_FT.search(s)
        if m and "ft" in s.lower():
            try:
                cm = round(float(m.group(1)) * 30.48, 1)
                s = s + f" ({cm} см)"
            except ValueError:
                pass
        m = _RE_F.search(s)
        if m:
            try:
                c = round((float(m.group(1)) - 32) * 5 / 9, 1)
                s = s + f" ({c}°C)"
            except ValueError:
                pass
        out[k] = s
    return out


def _build_query(brand: str, article: str, name: str) -> str:
    b = (brand or "").strip()
    a = (article or "").strip()
    if a.isdigit():
        a = ""
    if b and a:
        return f"{b} {a}"
    if b:
        tokens = re.findall(r"[A-Za-z0-9][A-Za-z0-9\-./]*", name or "")
        extras = [t for t in tokens if t.lower() != b.lower()][:4]
        return f"{b} {' '.join(extras)}".strip()
    tokens = re.findall(r"[A-Za-z0-9][A-Za-z0-9\-./]*", name or "")
    return " ".join(tokens[:6])


# ─── Browser helpers ──────────────────────────────────────────

async def _find_product_url(page, query: str) -> tuple[str, str]:
    """Search B&H and return (product_url, title) or ("", "")."""
    try:
        await page.goto(
            f"{_BH_SEARCH}?q={quote_plus(query)}",
            wait_until="networkidle",
            timeout=30_000,
        )
    except Exception as e:
        logger.warning("B&H search navigation failed: %s", e)
        return "", ""

    links = await page.query_selector_all('a[href*="/c/product/"]')
    for link in links:
        href = await link.get_attribute("href")
        if not href or "/c/product/" not in href:
            continue
        text = (await link.inner_text()).strip()
        if not text:
            continue
        url = href if href.startswith("http") else f"{_BH_BASE}{href}"
        url = re.sub(r"[?#].*$", "", url)
        return url, text

    return "", ""


async def _extract_specs_from_page(page, specs_url: str) -> dict:
    """Navigate to /specs URL and extract spec table from rendered DOM."""
    try:
        await page.goto(specs_url, wait_until="networkidle", timeout=30_000)
    except Exception as e:
        logger.warning("B&H specs navigation failed: %s", e)
        return {}

    specs: dict = {}

    # Strategy 1: data-selenium rows (B&H's known attribute)
    rows = await page.query_selector_all('[data-selenium="specsRow"]')
    if rows:
        for row in rows:
            cells = await row.query_selector_all("td")
            if len(cells) >= 2:
                k = (await cells[0].inner_text()).strip()
                v = (await cells[1].inner_text()).strip()
                if k and v and len(k) < 80:
                    specs[k] = v
        return _normalize_units(specs)

    # Strategy 2: generic 2-cell table rows
    rows = await page.query_selector_all("table tr")
    for row in rows:
        cells = await row.query_selector_all("td")
        if len(cells) == 2:
            k = (await cells[0].inner_text()).strip()
            v = (await cells[1].inner_text()).strip()
            if k and v and len(k) < 80:
                specs[k] = v

    return _normalize_units(specs)


async def _extract_mfr_info(page) -> dict:
    """Extract MFR # and Brand from page body text."""
    result: dict = {}
    try:
        body = await page.inner_text("body")
        m = re.search(
            r"Mfr\s*#\s*:?\s*([A-Za-z0-9][A-Za-z0-9\-./\s]{0,30}?)(?:\n|$)",
            body,
            re.I | re.M,
        )
        if m:
            result["mfr_number"] = m.group(1).strip()
        m = re.search(
            r"\bBrand\s*:?\s*([A-Za-z][^\n]{2,40}?)(?:\n|$)",
            body,
            re.I | re.M,
        )
        if m:
            result["brand_en"] = m.group(1).strip()
    except Exception:
        pass
    return result


async def get_bh_data(
    product_name: str,
    brand: str = "",
    article: str = "",
) -> dict:
    """
    Look up a product on B&H Photo Video and return full specs.

    Returns:
        {"found": True, "url": ..., "title": ..., "specs": {...}, "specs_count": N,
         "mfr_number": ..., "brand_en": ...}
        {"found": False}
    """
    query = _build_query(brand, article, product_name)
    if not query.strip():
        return {"found": False}

    logger.debug("B&H Playwright search: %s", query)

    try:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            try:
                context = await browser.new_context(
                    user_agent=(
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/124.0.0.0 Safari/537.36"
                    ),
                    locale="en-US",
                )
                page = await context.new_page()
                await Stealth().apply_stealth_async(page)

                product_url, title = await _find_product_url(page, query)
                if not product_url:
                    logger.info("B&H: no product found for %r", query)
                    return {"found": False}

                specs_url = product_url.rstrip("/") + "/specs"
                specs = await _extract_specs_from_page(page, specs_url)
                mfr_info = await _extract_mfr_info(page)

                logger.info("B&H found %d specs for %s", len(specs), product_url)
                return {
                    "found": True,
                    "url": product_url,
                    "title": title,
                    "specs": specs,
                    "specs_count": len(specs),
                    **mfr_info,
                }
            finally:
                await browser.close()
    except Exception as e:
        logger.warning("Playwright B&H error for %r: %s", query, e)
        return {"found": False}
