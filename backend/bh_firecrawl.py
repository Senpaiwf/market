# backend/bh_firecrawl.py
# B&H Photo Video product lookup via Firecrawl API.
# Replaces bh_scraper.py (which relied on curl_cffi to bypass Akamai).
#
# Search strategy:
#   1. Build English query from brand + article (both already Latin in MS data)
#   2. Use Firecrawl /v1/search with site:bhphotovideo.com to find the product page
#   3. Scrape the /specs subpage and extract all specs + MFR # and Brand
#
# Env var required: FIRECRAWL_API_KEY

from __future__ import annotations
import asyncio
import os
import re
import logging

import httpx

logger = logging.getLogger(__name__)

_FC_BASE   = "https://api.firecrawl.dev/v1"
_BH_DOMAIN = "www.bhphotovideo.com"

# ─── Unit conversion ──────────────────────────────────────────
_RE_LB = re.compile(r"([\d.]+)\s*lb", re.I)
_RE_OZ = re.compile(r"([\d.]+)\s*oz", re.I)
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


def _key() -> str:
    return os.getenv("FIRECRAWL_API_KEY", "")


async def _fc_search(query: str, limit: int = 5) -> list[dict]:
    """POST /v1/search — returns list of {url, title, markdown, ...}."""
    key = _key()
    if not key:
        return []
    async with httpx.AsyncClient(timeout=35) as client:
        try:
            r = await client.post(
                f"{_FC_BASE}/search",
                headers={"Authorization": f"Bearer {key}"},
                json={
                    "query": query,
                    "limit": limit,
                    "scrapeOptions": {"formats": ["markdown"], "onlyMainContent": True},
                },
            )
            r.raise_for_status()
            return r.json().get("data", [])
        except Exception as e:
            logger.warning("Firecrawl search error (%s): %s", query[:80], e)
            return []


async def _fc_scrape(url: str) -> str:
    """POST /v1/scrape — returns page markdown."""
    key = _key()
    if not key:
        return ""
    async with httpx.AsyncClient(timeout=40) as client:
        try:
            r = await client.post(
                f"{_FC_BASE}/scrape",
                headers={"Authorization": f"Bearer {key}"},
                json={"url": url, "formats": ["markdown"], "onlyMainContent": True},
            )
            r.raise_for_status()
            data = r.json()
            return (data.get("data") or {}).get("markdown") or ""
        except Exception as e:
            logger.warning("Firecrawl scrape error (%s): %s", url, e)
            return ""


async def _fc_extract_specs(url: str) -> dict:
    """
    Use Firecrawl /v1/extract (LLM-based) to extract product specs from a B&H page.
    Submits async job then polls until completed (max ~45s).
    Returns flat {spec_name: value} dict.
    """
    key = _key()
    if not key:
        return {}
    async with httpx.AsyncClient(timeout=60) as client:
        try:
            # Submit extraction job
            r = await client.post(
                f"{_FC_BASE}/extract",
                headers={"Authorization": f"Bearer {key}"},
                json={
                    "urls": [url],
                    "prompt": (
                        "Extract ALL product specifications from the Specs tab. "
                        "Include weight, dimensions, materials, compatibility, mounting options, "
                        "and every other spec listed. Return as key-value pairs."
                    ),
                    "schema": {
                        "type": "object",
                        "properties": {
                            "specs": {
                                "type": "object",
                                "description": "All product specifications as key-value pairs",
                                "additionalProperties": {"type": "string"},
                            }
                        },
                    },
                },
            )
            r.raise_for_status()
            job_id = r.json().get("id")
            if not job_id:
                return {}
        except Exception as e:
            logger.warning("Firecrawl extract submit error (%s): %s", url, e)
            return {}

    # Poll for result
    async with httpx.AsyncClient(timeout=30) as client:
        for _ in range(15):
            await asyncio.sleep(3)
            try:
                r = await client.get(
                    f"{_FC_BASE}/extract/{job_id}",
                    headers={"Authorization": f"Bearer {key}"},
                )
                r.raise_for_status()
                data = r.json()
                status = data.get("status")
                if status == "completed":
                    raw = (data.get("data") or {}).get("specs") or {}
                    return _normalize_units(raw) if isinstance(raw, dict) else {}
                if status == "failed":
                    logger.warning("Firecrawl extract failed for %s", url)
                    return {}
            except Exception as e:
                logger.warning("Firecrawl extract poll error: %s", e)
                return {}

    logger.warning("Firecrawl extract timed out for %s", url)
    return {}


def _build_query(brand: str, article: str, name: str) -> str:
    """
    Construct an English search query.
    Priority: brand + article → brand + ASCII tokens from name → ASCII tokens only.
    Purely numeric articles (internal codes like "06153") are skipped — not MFR part numbers.
    """
    b = (brand or "").strip()
    a = (article or "").strip()
    # Skip article if it's a purely numeric internal code (no letters)
    if a.isdigit():
        a = ""
    if b and a:
        return f"{b} {a}"
    if b:
        # Keep only ASCII/Latin tokens (model numbers, letters) from the Russian name
        tokens = re.findall(r"[A-Za-z0-9][A-Za-z0-9\-./]*", name or "")
        extras = [t for t in tokens if t.lower() != b.lower()][:4]
        return f"{b} {' '.join(extras)}".strip()
    tokens = re.findall(r"[A-Za-z0-9][A-Za-z0-9\-./]*", name or "")
    return " ".join(tokens[:6])


def _extract_mfr_brand(md: str) -> dict:
    """
    Parse B&H product page markdown and extract MFR # and Brand.

    B&H formats seen in markdown:
      Mfr #: TA-T18-A
      **Mfr #:** TA-T18-A
      | Mfr # | TA-T18-A |
    """
    result: dict = {}

    # MFR #
    m = re.search(
        r"mfr\s*#\s*[:\|]\**\s*([A-Za-z0-9][A-Za-z0-9\-./\s]{0,40}?)(?:\n|\||\*\*|\Z)",
        md,
        re.IGNORECASE,
    )
    if m:
        result["mfr_number"] = m.group(1).strip(" \t|*")

    # Brand — "Brand: Tilta" / "**Brand:** Tilta" / "| Brand | Tilta |"
    m = re.search(
        r"\*{0,2}Brand\*{0,2}\s*[:\|]\**\s*([^\n\|\*]{2,60})",
        md,
        re.IGNORECASE,
    )
    if m:
        result["brand_en"] = m.group(1).strip(" \t|*")

    return result


def _extract_specs(md: str) -> dict:
    """
    Parse B&H /specs page markdown and return a flat {spec_name: value} dict.

    Handles three formats Firecrawl may render:
      1. Markdown table  : | Weight | 1.40 lb |
      2. Bold key-value  : **Weight:** 1.40 lb
      3. Plain key-value : Weight: 1.40 lb  (indented or bare)
    """
    specs: dict = {}
    lines = md.splitlines()

    # Skip lines that are table separators or empty
    _sep = re.compile(r"^\s*\|?[\s\-:]+\|")

    for line in lines:
        line = line.strip()
        if not line or _sep.match(line):
            continue

        # --- Format 1: markdown table row  | Key | Value |
        if line.startswith("|"):
            parts = [p.strip(" \t*") for p in line.split("|") if p.strip(" \t*|")]
            if len(parts) == 2:
                k, v = parts
                if k and v and len(k) < 80 and len(v) < 300:
                    specs[k] = v
            continue

        # --- Format 2: **Key:** Value  or  **Key**: Value
        m = re.match(r"\*{1,2}([^*]{2,70}?)\*{1,2}\s*:+\s*(.+)", line)
        if m:
            k, v = m.group(1).strip(), m.group(2).strip(" \t*")
            if k and v and len(v) < 300:
                specs[k] = v
            continue

        # --- Format 3: Key: Value  (key must be title-case or multi-word, not a sentence)
        m = re.match(r"([A-Z][A-Za-z0-9 /()&,\-]{1,70}?)\s*:\s*(.{1,280})", line)
        if m:
            k, v = m.group(1).strip(), m.group(2).strip()
            # Ignore lines that look like URL fragments or navigation
            if k and v and "/" not in k and len(k.split()) <= 8:
                specs[k] = v

    return _normalize_units(specs)


async def get_bh_data(
    product_name: str,
    brand: str = "",
    article: str = "",
) -> dict | None:
    """
    Look up a product on B&H Photo Video and return full specs.

    Args:
        product_name: Russian product name from MoySklad (used as fallback for query).
        brand:   Brand from MS custom attr "Бренд" — already in English/Latin.
        article: Manufacturer article from MS — often equals MFR #.

    Returns:
        {"found": True,  "url": ..., "title": ..., "mfr_number": ..., "brand_en": ...,
         "specs": {"Weight": "1.40 lb (0.64 кг)", "Color": "Black", ...},
         "specs_count": N}
        {"found": False}
        None  — Firecrawl API key not configured
    """
    if not _key():
        logger.info("FIRECRAWL_API_KEY not set — B&H lookup skipped")
        return None

    query = _build_query(brand, article, product_name)
    if not query.strip():
        return {"found": False}

    site_query = f"site:{_BH_DOMAIN} {query}"
    logger.debug("B&H search: %s", site_query)

    results = await _fc_search(site_query, limit=5)

    # Pick the first result that looks like a B&H product page
    product_url: str = ""
    product_title: str = ""
    inline_md: str = ""

    for item in results:
        url = item.get("url", "")
        if _BH_DOMAIN in url and "/c/product/" in url:
            product_url   = re.sub(r"[?#].*$", "", url)   # strip query/fragment
            product_title = item.get("title", "")
            inline_md     = item.get("markdown", "") or ""
            break

    if not product_url:
        return {"found": False}

    # Try to extract mfr/brand from search snippet (saves one API call)
    extracted = _extract_mfr_brand(inline_md)

    # Use Firecrawl Extract API (LLM-based) to get full specs from the /specs subpage
    specs_url = product_url.rstrip("/") + "/specs"
    logger.debug("B&H extract specs: %s", specs_url)
    specs = await _fc_extract_specs(specs_url)

    # If mfr/brand not found in snippet, also scrape main page for those fields
    if not extracted.get("mfr_number") or not extracted.get("brand_en"):
        page_md = await _fc_scrape(product_url)
        if page_md:
            extracted.update(_extract_mfr_brand(page_md))

    logger.info("B&H found %d specs for %s", len(specs), product_url)

    return {
        "found":       True,
        "url":         product_url,
        "title":       product_title,
        "specs":       specs,
        "specs_count": len(specs),
        **extracted,
    }
