# Enrich Script — Design Spec
Date: 2026-05-01

## Overview

A standalone CLI script (`market/backend/enrich_script.py`) that takes a list of product
codes, fetches data from МойСклад + B&H Photo, runs Gemini AI to suggest categories, lets
the user confirm/correct them, then fills all marketplace characteristics and saves to
`answers.json`.

Run via Docker:
```bash
docker exec -it market-backend-1 python enrich_script.py
```

---

## Goals

- Replace the manual "AI заполнить" button workflow with a batch CLI tool
- Determine correct categories for all 3 marketplaces (YM, Ozon, WB)
- Fill characteristics as thoroughly as possible (3-layer: regex → rules → Gemini)
- Save results to `answers.json` so the main app picks them up on next load
- Print a report showing what was filled and estimated ratings

---

## Architecture

Single file: `market/backend/enrich_script.py`
Reuses existing backend modules without modification:
- `moysklad.MoySkladClient` — product fetch
- `bh_firecrawl.get_bh_data` — B&H specs
- `ai_gemini.auto_enrich` — first-pass category + basic params
- `ai_gemini.enrich_category_params` — second-pass full attributes
- `spec_extractor.extract_specs`, `get_category_rules`, `format_for_ai` — layers 1+2
- `yandex_market.YandexMarketClient` — YM category params
- `ozon.OzonClient` — Ozon category attributes
- `wb.WildberriesClient` — WB category characteristics

Reads all API keys from environment (Docker already loads `.env`).
Reads/writes `answers.json` in `/app/` (same directory as `main.py`).

---

## 4-Phase Flow

### Phase 1 — Parallel data loading

For all N input codes, concurrently:
- `ms_client.get_product_data(code)` — full product with attributes, dims, photos
- `bh_firecrawl.get_bh_data(name, brand, article)` — B&H specs

Progress printed as tasks complete:
```
Загружаем данные для 10 товаров...
  МС: ✓ 18385  ✓ 18389  ✗ 03191 (не найден)  ...
  B&H: ✓ 17543  ✓ 17683  ✗ 18369 (не найдено)  ...
```

### Phase 2 — Parallel AI category suggestion

For all successfully loaded products, concurrently run `gemini_auto_enrich` with:
- YM / Ozon / WB category trees (from cache files)
- B&H specs (if found)
- MS product data

Display results as a table:
```
╔══════╦════════════════════════╦══════════════════════╦════════════════════════╦════════════════════╗
║ Код  ║ Товар                  ║ ЯМ (conf)            ║ Ozon (conf)            ║ WB (conf)          ║
╠══════╬════════════════════════╬══════════════════════╬════════════════════════╬════════════════════╣
║18385 ║ SmallRig Cage...       ║ Клетки [0.87]        ║ Кино аксессуары [0.82] ║ Фото/видео [0.75]  ║
║03191 ║ Kingma DR-LPE5...      ║ Аккумуляторы [0.95]  ║ Аккумуляторы [0.90]    ║ Аккумуляторы[0.85] ║
╚══════╩════════════════════════╩══════════════════════╩════════════════════════╩════════════════════╝
Legend: [B&H: ✗] = B&H not found, categories determined from MS data only
```

Confidence display: `✓` if ≥ 0.75, `?` if 0.50–0.74, `✗` if < 0.50.

### Phase 3 — Interactive category confirmation

```
Введите Enter чтобы принять все категории, или укажите правки:
  18385 ym <id или часть названия>  — поменять категорию ЯМ
  18385 oz                          — выбрать категорию Ozon из списка
  18385 wb                          — выбрать категорию WB из списка
  18385 skip                        — пропустить этот товар
  help                              — показать эту справку
> _
```

When user requests manual selection (e.g. `18389 oz`):
- Print top-20 Ozon leaf categories matching product keywords
- User picks by number or types partial name

When no input (Enter): accept all proposed categories and proceed.

Already-saved categories in `answers.json` are shown as `[saved]` and kept unless user
explicitly overrides them.

### Phase 4 — Parallel characteristics fill + save

For each confirmed product/marketplace pair, concurrently:
1. Fetch full attribute list from marketplace API (or cache)
2. `spec_extractor.extract_specs()` — layer 1: regex extraction
3. `spec_extractor.get_category_rules()` — layer 2: type-based rules
4. `ai_gemini.enrich_category_params()` — layer 3: Gemini full fill

Merge results: rules override nothing from MS, AI fills remaining gaps.

Save to `answers.json`:
```python
{
  "brand": "...",
  "description": "...",
  "category": "ym_category_id",
  "params_values": {"ym_123": "value"},
  "ozon_category_key": "...",
  "ozon_category_id": 123,
  "ozon_type_id": 456,
  "ozon_attrs": {"oz_85": "SmallRig"},
  "wb_category_key": "s_123",
  "wb_subject_id": 123,
  "wb_chars": {"wb_456": "value"}
}
```

Existing user-set values are **not overwritten** (same policy as `product_load`).

Print final report:
```
=== РЕЗУЛЬТАТ ===
✓ 18385  SmallRig Cage for Fujifilm X100VI
    ЯМ:    12 характеристик  | рейтинг ~87
    Ozon:  18 характеристик  | score 8.5
    WB:     6 характеристик
    → answers.json ✓

✗ 03191  Kingma DR-LPE5  [B&H: не найден]
    ЯМ:     8 характеристик  | рейтинг ~71
    Ozon:  14 характеристик  | score 7.0
    WB:     4 характеристики
    → answers.json ✓
```

---

## Error Handling

| Situation | Behavior |
|-----------|----------|
| Товар не найден в МС | Skip with error message, continue |
| B&H не найден | Continue without B&H specs, note `[B&H: ✗]` in table |
| Gemini ошибка/квота | Log warning, save MS-only data, continue |
| Firecrawl 429 (rate limit) | Wait 10s, retry once |
| Marketplace API недоступен | Use cached attributes if available, else skip that MP |
| Товар уже в answers.json | Keep existing values, fill only missing fields |

---

## Input / Output

**Input:** Interactive prompt asking for codes (space or comma separated):
```
Введите коды товаров через пробел или запятую:
> 18385 18389 17543 17683 18369 18370 03191 18368 18386 18387
```

Or pass as CLI args: `python enrich_script.py 18385 18389 17543`

**Output files:**
- `answers.json` — merged enriched answers (persistent, read by main app)

**No new dependencies** — all imports already in `requirements.txt`.

---

## Scope Boundaries

- Does NOT upload to marketplaces (upload remains in main app)
- Does NOT modify category cache files
- Does NOT change any existing backend modules
- WB fill saves chars but upload endpoint remains a stub
