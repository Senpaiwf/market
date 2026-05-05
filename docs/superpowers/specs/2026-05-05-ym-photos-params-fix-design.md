# Design: YM Photos Full Size + parameterValues Fix + False Errors Fix

**Date:** 2026-05-05  
**Project:** MarketSync v2 — FotoToad  
**Status:** Approved

---

## Summary

Three interrelated fixes for the Yandex Market upload pipeline:

1. **Photos from МС in full resolution** — currently fetching thumbnails
2. **`parameterValues` correct format** — enum params need `valueId: int`, not `value: "text"`
3. **Remove false "Не заполнено" errors** — post-upload state check runs before YM processes async

---

## Fix 1: Full-Size Photos from МойСклад

**File:** `backend/moysklad.py`, method `_get_images()` (~line 242)

**Root cause:** `img.get("miniature", {}).get("href", "")` returns the thumbnail URL. The full-size download URL is at `img["meta"]["downloadHref"]`.

**Change:**
```python
# Before:
url = (
    img.get("miniature", {}).get("href", "") or
    img.get("meta", {}).get("href", "")
)

# After:
url = (
    img.get("meta", {}).get("downloadHref", "") or
    img.get("miniature", {}).get("href", "")
)
```

**Why this works:** `_prepare_images()` in `main.py` already handles auth for `api.moysklad.ru` URLs via `ms_auth = f"Bearer {ms_token}"`. No other changes needed.

**Testing required:** Verify downloaded images are full resolution, not thumbnails.

---

## Fix 2: Correct `parameterValues` Format for YM API

YM Partner API requires enum params as `{"parameterId": id, "valueId": int}`, not `{"parameterId": id, "value": "text"}`. YM returns HTTP 200 but silently discards incorrectly formatted enum values.

### 2a. Frontend: store valueId in select options

**File:** `frontend/index.html`, function `_pmParamRow()` (~line 2344)

**Backward-compatibility note:** Existing saved values (from manual input or AI) are stored as text ("Li-Ion"). After changing option `value` to numeric ID, those text values won't match any option and nothing will be pre-selected. Fix: resolve `curVal` to numeric ID before comparing, using a reverse lookup by display text.

```javascript
// In _pmParamRow, before building the select:
// Resolve curVal to numeric ID if it's currently stored as text
let resolvedVal = curVal;
if (p.type === 'ENUM' && curVal && isNaN(Number(curVal))) {
  const matched = (p.allowed_values || []).find(av => String(av.value) === curVal);
  if (matched && matched.id != null) resolvedVal = String(matched.id);
}

// Then:
p.allowed_values.map(av => {
  const optVal = String(av.id != null ? av.id : av.value);
  return `<option value="${esc(optVal)}" ${optVal === resolvedVal ? 'selected' : ''}>${esc(av.value)}</option>`;
})
```

- `value` attribute = numeric ID (e.g. `"67890"`) — what gets stored in `params_values`
- Display text = human-readable name (e.g. `"Li-Ion"`) — unchanged
- Backward compat: text values from old saves resolve to correct ID before comparison
- Fallback to `av.value` string if `av.id` is null (defensive)

### 2b. Backend: smart dispatch in `_build_params`

**File:** `backend/yandex_market.py`, method `_build_params()` (~line 554)

```python
# Before:
result.append({"parameterId": pid, "value": str(v)})

# After:
try:
    vid = int(v)
    result.append({"parameterId": pid, "valueId": vid})
except (ValueError, TypeError):
    result.append({"parameterId": pid, "value": str(v)})
```

**Backward compatibility:** Old saved text values (e.g. `"Li-Ion"`) hit the `except` branch → sent as `value: "Li-Ion"`. New numeric IDs (e.g. `"67890"`) hit the `try` branch → sent as `valueId: 67890`. Both cases handled correctly.

### 2c. Backend: verify `current_value` in detail-preview returns ID not text

**File:** `backend/main.py`, endpoint `/api/ym/offer/detail-preview`

Check that when building `category_params`, the `current_value` for enum params is set to the numeric ID string (not display text), so the select pre-selects the correct option after the frontend change. If `current_value` is currently set to text, change it to the numeric ID.

---

## Fix 3: Remove False "Не заполнено" Errors

**Root cause:** `validate_offer_state()` is called immediately after upload. YM processes `offer-mappings/update` asynchronously — the state check returns pre-update data.

Additionally, `marketCategoryId` and `parameterValues` are not reliably returned by the `offer-mappings` response endpoint even after processing.

### 3a. Remove unreliable fields from REQUIRED check

**File:** `backend/yandex_market.py`, method `validate_offer_state()` (~line 325)

Remove `marketCategoryId` and `parameterValues` from the `REQUIRED` list:

```python
REQUIRED = [
    ("name",             "Название"),
    ("description",      "Описание"),
    ("vendor",           "Бренд"),
    ("pictures",         "Изображения"),
    ("basicPrice",       "Цена"),
    ("weightDimensions", "Габариты/вес"),
    # Removed: marketCategoryId, parameterValues — not returned stably by YM
]
```

### 3b. Filter log output in SSE stream

**File:** `backend/main.py`, YM upload SSE handler (~line 551)

Only show "Не заполнено" warning for genuinely critical missing fields: `Название`, `Цена`, `Изображения`. Category and characteristics are expected to appear after YM processes the offer asynchronously.

---

## Files Changed

| File | Change |
|------|--------|
| `backend/moysklad.py` | `_get_images()`: use `meta.downloadHref` |
| `backend/yandex_market.py` | `_build_params()`: `valueId` for int, `value` for text |
| `backend/yandex_market.py` | `validate_offer_state()`: remove category/params from REQUIRED |
| `backend/main.py` | SSE log: filter non-critical missing fields |
| `frontend/index.html` | `_pmParamRow()`: store `av.id` in select option value |

---

## Out of Scope

- Ozon and WB upload flows (separate task)
- Persisting `answers_store` to disk
- WB end-to-end upload testing
