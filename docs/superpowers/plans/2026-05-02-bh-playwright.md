# B&H Playwright Scraper Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the paid Firecrawl API scraper with a free Playwright-based B&H Photo scraper that exports the same `get_bh_data()` interface.

**Architecture:** New file `bh_playwright.py` uses Playwright + playwright-stealth (Akamai bypass) to search B&H, navigate to the product specs page, and extract specs from the rendered DOM. All callers switch with a one-line import change. `bh_firecrawl.py` stays in repo but is no longer imported.

**Tech Stack:** Python 3.11, playwright (async API), playwright-stealth, Chromium (headless), Docker

---

## File Map

| File | Action |
|------|--------|
| `backend/requirements.txt` | Add `playwright` and `playwright-stealth` |
| `backend/Dockerfile` | Add Chromium install step |
| `backend/bh_playwright.py` | **Create** — new scraper |
| `backend/tests/test_bh_playwright.py` | **Create** — pure-function tests |
| `backend/main.py:30` | Change import |
| `backend/enrich_script.py:20` | Change import |

---

### Task 1: Add Playwright dependencies

**Files:**
- Modify: `backend/requirements.txt`
- Modify: `backend/Dockerfile`

- [ ] **Step 1: Add packages to requirements.txt**

Current `backend/requirements.txt` ends with `PyYAML==6.0.1`. Add two lines at the end:

```
playwright
playwright-stealth
```

Final file:
```
fastapi==0.111.0
uvicorn[standard]==0.29.0
httpx==0.27.0
pydantic==2.7.1
pydantic-settings==2.2.1
aiofiles==23.2.1
python-multipart==0.0.9
python-dotenv==1.0.1
curl_cffi==0.7.4
beautifulsoup4==4.12.3
google-genai
openai>=1.0.0
Pillow==10.4.0
PyYAML==6.0.1
playwright
playwright-stealth
```

- [ ] **Step 2: Add Chromium install to Dockerfile**

Current `backend/Dockerfile`:
```dockerfile
FROM python:3.11-slim
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
RUN mkdir -p /app/media/videos
EXPOSE 8000
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--reload"]
```

Replace with:
```dockerfile
FROM python:3.11-slim
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
RUN playwright install chromium --with-deps
COPY . .
RUN mkdir -p /app/media/videos
EXPOSE 8000
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--reload"]
```

Note: `playwright install chromium --with-deps` installs Chromium binary + all required system libraries (libasound2, libatk-bridge2.0-0, etc.). This adds ~450 MB to the image but is required for headless operation.

- [ ] **Step 3: Verify build succeeds**

```bash
cd market && docker compose build 2>&1 | tail -20
```

Expected: `Successfully built ...` with no errors. The install step will take 2-3 minutes on first run.

- [ ] **Step 4: Commit**

```bash
cd market/backend
git add requirements.txt Dockerfile
git commit -m "feat: add playwright and playwright-stealth dependencies"
```

---

### Task 2: Create bh_playwright.py — pure helper functions (TDD)

**Files:**
- Create: `backend/bh_playwright.py`
- Create: `backend/tests/test_bh_playwright.py`

These helpers have no browser dependency and can be tested locally.

- [ ] **Step 1: Write failing tests**

Create `backend/tests/test_bh_playwright.py`:

```python
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from bh_playwright import _build_query, _normalize_units


def test_build_query_brand_and_article():
    assert _build_query("SmallRig", "TA-T18-A", "SmallRig Cage") == "SmallRig TA-T18-A"


def test_build_query_skips_numeric_article():
    result = _build_query("SmallRig", "18385", "SmallRig Cage X100VI")
    assert result.startswith("SmallRig")
    assert "18385" not in result


def test_build_query_fallback_name_tokens():
    result = _build_query("", "", "SmallRig Cage X100VI")
    assert "SmallRig" in result
    assert "X100VI" in result


def test_normalize_units_pounds():
    result = _normalize_units({"Weight": "1.40 lb"})
    assert "0.64 кг" in result["Weight"]


def test_normalize_units_inches():
    result = _normalize_units({"Length": "5.5 in"})
    assert "14.0 см" in result["Length"]


def test_normalize_units_no_change():
    result = _normalize_units({"Color": "Black"})
    assert result["Color"] == "Black"
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
docker exec market python -m pytest tests/test_bh_playwright.py -v 2>&1 | tail -15
```

Expected: `ModuleNotFoundError: No module named 'bh_playwright'`

- [ ] **Step 3: Create bh_playwright.py with helpers**

Create `backend/bh_playwright.py`:

```python
# backend/bh_playwright.py
# B&H Photo Video product lookup via Playwright + playwright-stealth.
# Replaces bh_firecrawl.py — same get_bh_data() interface, no paid API required.
from __future__ import annotations
import re
import logging

from playwright.async_api import async_playwright
from playwright_stealth import stealth_async

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
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
docker exec market python -m pytest tests/test_bh_playwright.py -v 2>&1 | tail -15
```

Expected: `6 passed`

- [ ] **Step 5: Commit**

```bash
cd market/backend
git add bh_playwright.py tests/test_bh_playwright.py
git commit -m "feat: add bh_playwright.py with pure helpers + tests"
```

---

### Task 3: Implement get_bh_data() with Playwright

**Files:**
- Modify: `backend/bh_playwright.py` — add `_find_product_url`, `_extract_specs_from_page`, `_extract_mfr_info`, `get_bh_data`

No unit tests for browser functions — covered by smoke test in Step 4.

- [ ] **Step 1: Append browser functions to bh_playwright.py**

Append to the end of `backend/bh_playwright.py`:

```python

# ─── Browser helpers ──────────────────────────────────────────

async def _find_product_url(page, query: str) -> tuple[str, str]:
    """Search B&H and return (product_url, title) or ("", "")."""
    try:
        await page.goto(
            f"{_BH_SEARCH}?q={query}",
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
            context = await browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0.0.0 Safari/537.36"
                ),
                locale="en-US",
            )
            page = await context.new_page()
            await stealth_async(page)

            product_url, title = await _find_product_url(page, query)
            if not product_url:
                await browser.close()
                logger.info("B&H: no product found for %r", query)
                return {"found": False}

            specs_url = product_url.rstrip("/") + "/specs"
            specs = await _extract_specs_from_page(page, specs_url)
            mfr_info = await _extract_mfr_info(page)

            await browser.close()

            logger.info("B&H found %d specs for %s", len(specs), product_url)
            return {
                "found": True,
                "url": product_url,
                "title": title,
                "specs": specs,
                "specs_count": len(specs),
                **mfr_info,
            }
    except Exception as e:
        logger.warning("Playwright B&H error for %r: %s", query, e)
        return {"found": False}
```

- [ ] **Step 2: Verify import inside Docker**

```bash
docker exec market python -c "from bh_playwright import get_bh_data; print('OK')"
```

Expected: `OK`

- [ ] **Step 3: Run all tests still pass**

```bash
docker exec market python -m pytest tests/test_bh_playwright.py -v 2>&1 | tail -10
```

Expected: `6 passed`

- [ ] **Step 4: Smoke test against real B&H**

```bash
docker exec market python -c "
import asyncio, json
from bh_playwright import get_bh_data
result = asyncio.run(get_bh_data('SmallRig Cage', 'SmallRig', 'TA-T18-A'))
print('found:', result.get('found'))
print('specs_count:', result.get('specs_count', 0))
print('url:', result.get('url', ''))
if result.get('specs'):
    first = list(result['specs'].items())[:3]
    for k, v in first:
        print(f'  {k}: {v}')
"
```

Expected: `found: True`, `specs_count: > 0`, at least one spec printed.

If `found: False` — B&H may have blocked headless Chromium. Investigate by temporarily running with `headless=False` locally, or check if `playwright-stealth` was applied correctly.

- [ ] **Step 5: Commit**

```bash
cd market/backend
git add bh_playwright.py
git commit -m "feat: implement get_bh_data via Playwright with stealth"
```

---

### Task 4: Switch imports in main.py and enrich_script.py

**Files:**
- Modify: `backend/main.py:30`
- Modify: `backend/enrich_script.py:20`

- [ ] **Step 1: Update main.py**

In `backend/main.py`, line 30:

Old:
```python
from bh_firecrawl import get_bh_data
```

New:
```python
from bh_playwright import get_bh_data
```

- [ ] **Step 2: Update enrich_script.py**

In `backend/enrich_script.py`, line 20:

Old:
```python
from bh_firecrawl import get_bh_data
```

New:
```python
from bh_playwright import get_bh_data
```

- [ ] **Step 3: Verify both files import cleanly**

```bash
docker exec market python -c "import main; print('main OK')"
docker exec market python -c "import enrich_script; print('enrich_script OK')"
```

Expected: `main OK` and `enrich_script OK` with no errors.

- [ ] **Step 4: Commit**

```bash
cd market/backend
git add main.py enrich_script.py
git commit -m "feat: switch B&H scraping from Firecrawl to Playwright"
```

---

### Task 5: End-to-end test with enrich_script

**Files:** No changes — verification only.

- [ ] **Step 1: Run enrich_script with one product that has a B&H presence**

`enrich_script.py` takes МойСклад codes (numeric internal IDs), not article numbers. Use code `18385` (SmallRig Cage — likely on B&H):

```bash
docker exec -it market python enrich_script.py 18385
```

Expected flow:
- Phase 1: MS data loaded, B&H search launches Playwright, finds SmallRig product
- Table shows WB category with confidence badge
- Phase 3: interactive prompt appears

If B&H returns `found: False` for this product — that's acceptable (not all products are on B&H). The script continues with description-only enrichment.

- [ ] **Step 2: Verify answers.json updated**

```bash
docker exec market python -c "
import json
d = json.load(open('answers.json'))
code = '18385'
if code in d:
    a = d[code]
    print('wb_chars:', len(a.get('wb_chars', {})), 'entries')
    print('brand:', a.get('brand'))
else:
    print('not in answers.json yet')
"
```

Expected: code present with wb_chars populated.

- [ ] **Step 3: Final commit (if any fixups needed)**

If any small fixes were needed during smoke test, commit them:

```bash
git add -p
git commit -m "fix: bh_playwright smoke test fixes"
```
