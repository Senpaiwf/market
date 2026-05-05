# B&H Playwright Scraper — Design Spec
Date: 2026-05-02

## Overview

Replace `bh_firecrawl.py` (paid Firecrawl API, currently returning 402) with a free
Playwright-based scraper `bh_playwright.py`. The new module exports the same
`get_bh_data(product_name, brand, article)` interface so all callers (`main.py`,
`enrich_script.py`) switch with a one-line import change.

---

## Goals

- Free B&H Photo scraping with no external API dependency
- Same return format as `bh_firecrawl.get_bh_data` — zero changes in callers beyond import
- Reliable Akamai bypass via `playwright-stealth`
- Works inside the existing Docker container

---

## Architecture

### Files

| File | Action | Description |
|------|--------|-------------|
| `backend/bh_playwright.py` | **Create** | New scraper, exports `get_bh_data()` |
| `backend/bh_firecrawl.py` | Keep (unused) | Not deleted, not imported anywhere |
| `backend/main.py` | **Modify** | Switch import: `bh_firecrawl` → `bh_playwright` |
| `backend/enrich_script.py` | **Modify** | Switch import: `bh_firecrawl` → `bh_playwright` |
| `backend/requirements.txt` | **Modify** | Add `playwright`, `playwright-stealth` |
| `backend/Dockerfile` | **Modify** | Add `RUN playwright install chromium --with-deps` |

---

## Data Flow

1. `_build_query(brand, article, name)` — same logic as `bh_firecrawl._build_query`
2. Launch Playwright Chromium (headless), apply `playwright_stealth` patch to bypass Akamai
3. Navigate to `https://www.bhphotovideo.com/c/search?q={query}` — wait for `networkidle`
4. Find first result link containing `/c/product/` — if none found, return `{"found": False}`
5. Navigate to `{product_url}/specs` — wait for specs table selector
6. Extract specs from DOM rows → `{key: value}` dict
7. Apply `_normalize_units()` (copied from `bh_firecrawl.py`) for unit conversions
8. Close browser, return result dict

Browser is created and closed per `get_bh_data()` call. Parallel calls from
`enrich_script.py` launch separate browser instances — acceptable for ≤10 products.

---

## Return Format

Identical to `bh_firecrawl.get_bh_data`:

```python
# Success
{
    "found": True,
    "url": "https://www.bhphotovideo.com/c/product/...",
    "title": "SmallRig ...",
    "specs": {"Weight": "1.40 lb (0.64 кг)", "Color": "Black", ...},
    "specs_count": 12,
    "mfr_number": "TA-T18-A",   # extracted from page if present
    "brand_en": "SmallRig",      # extracted from page if present
}

# Not found / blocked
{"found": False}
```

`None` is never returned (unlike `bh_firecrawl` which returns `None` when API key missing).

---

## Error Handling

| Situation | Behavior |
|-----------|----------|
| Akamai CAPTCHA / search returns 0 results | `{"found": False}` |
| Product not found in search | `{"found": False}` |
| `/specs` tab empty / no table rendered | `{"found": True, ..., "specs": {}, "specs_count": 0}` |
| Navigation timeout (30s per page) | Log warning → `{"found": False}` |
| Any unexpected exception | Log warning with traceback → `{"found": False}` |
| Chromium not installed | `playwright install` error at container startup |

---

## Docker Changes

```dockerfile
# After pip install requirements:
RUN playwright install chromium --with-deps
```

Image size increase: ~400–500 MB (Chromium binary + system libs).

---

## Dependencies Added

```
playwright          # browser automation
playwright-stealth  # Akamai bypass: patches navigator.webdriver, canvas, UA
```

`FIRECRAWL_API_KEY` env var is no longer needed. Not removed from `.env` to avoid
breaking existing setups — it simply goes unused.

---

## Scope Boundaries

- Does NOT replace Firecrawl for any other use case
- Does NOT add retry logic beyond Playwright's built-in waits
- Does NOT persist a browser session across requests
- `bh_firecrawl.py` remains in repo (not deleted) in case Firecrawl is re-enabled
