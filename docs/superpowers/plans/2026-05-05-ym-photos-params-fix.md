# YM Photos + parameterValues Fix Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix three YM upload issues: МС photos use full-size URLs, enum `parameterValues` send `valueId` instead of text, and post-upload false "Не заполнено" errors are removed.

**Architecture:** Four targeted edits across three backend files and one frontend file. No new files. Each fix is independent — they can be tested and committed separately. Tests follow the existing `backend/tests/` pattern (pytest, `sys.path.insert`).

**Tech Stack:** Python 3.11, FastAPI, vanilla JS, pytest

---

## File Map

| File | What changes |
|------|-------------|
| `backend/moysklad.py` | `_get_images()`: use `meta.downloadHref` instead of `miniature.href` |
| `backend/yandex_market.py` | `_build_params()`: dispatch to `valueId` for integers, `value` for text |
| `backend/yandex_market.py` | `validate_offer_state()`: remove `marketCategoryId` and `parameterValues` from REQUIRED |
| `backend/main.py` | YM SSE handler: filter non-critical fields from "Не заполнено" log message |
| `frontend/index.html` | `_pmParamRow()`: store numeric `av.id` in select, backward-compat resolve by text |
| `backend/tests/test_ym_fixes.py` | New: unit tests for `_get_images` URL priority and `_build_params` dispatch |

---

## Task 1: Fix full-size photo URL in `_get_images()`

**Files:**
- Modify: `backend/moysklad.py` (~line 242)
- Test: `backend/tests/test_ym_fixes.py` (create)

- [ ] **Step 1: Create test file and write the failing test**

Create `backend/tests/test_ym_fixes.py`:

```python
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from moysklad import MoySkladClient


def _make_client():
    return MoySkladClient("fake-token")


# ── Task 1: _get_images URL priority ──────────────────────────

def test_get_images_prefers_meta_download_href():
    """Full-size downloadHref must take priority over miniature href."""
    client = _make_client()
    fake_rows = {
        "rows": [
            {
                "meta": {
                    "href": "https://api.moysklad.ru/entity/product/123/images/img1",
                    "downloadHref": "https://api.moysklad.ru/download/full-size-id",
                },
                "miniature": {
                    "href": "https://api.moysklad.ru/download/miniature-id",
                },
            }
        ]
    }
    import asyncio

    async def _mock_get(path, params=None):
        return fake_rows

    client._get = _mock_get
    urls = asyncio.run(client._get_images("fake-product-id"))
    assert urls == ["https://api.moysklad.ru/download/full-size-id"]


def test_get_images_falls_back_to_miniature_when_no_download_href():
    """Falls back to miniature.href when meta.downloadHref is absent."""
    client = _make_client()
    fake_rows = {
        "rows": [
            {
                "meta": {
                    "href": "https://api.moysklad.ru/entity/product/123/images/img1",
                    # no downloadHref here
                },
                "miniature": {
                    "href": "https://api.moysklad.ru/download/miniature-id",
                },
            }
        ]
    }
    import asyncio

    async def _mock_get(path, params=None):
        return fake_rows

    client._get = _mock_get
    urls = asyncio.run(client._get_images("fake-product-id"))
    assert urls == ["https://api.moysklad.ru/download/miniature-id"]
```

- [ ] **Step 2: Run the tests to see them fail**

```
cd market/backend
python -m pytest tests/test_ym_fixes.py -v
```

Expected: 2 FAILED — `test_get_images_prefers_meta_download_href` fails because current code returns miniature URL.

- [ ] **Step 3: Fix `_get_images()` in `moysklad.py`**

Find the method at ~line 232. Change the URL selection:

```python
async def _get_images(self, product_id: str) -> list:
    d = await self._get(
        f"/entity/product/{product_id}/images",
        params={"limit": 10}
    )
    if not d or "_err" in d:
        return []
    result = []
    for img in d.get("rows", []):
        url = (
            img.get("meta", {}).get("downloadHref", "") or
            img.get("miniature", {}).get("href", "")
        )
        if url:
            result.append(url)
    return result
```

- [ ] **Step 4: Run the tests again**

```
cd market/backend
python -m pytest tests/test_ym_fixes.py -v
```

Expected: 2 PASSED.

- [ ] **Step 5: Commit**

```
git add backend/moysklad.py backend/tests/test_ym_fixes.py
git commit -m "fix: use full-size downloadHref for МС product images instead of miniature"
```

---

## Task 2: Fix `_build_params()` to send `valueId` for enum values

**Files:**
- Modify: `backend/yandex_market.py` (~line 547)
- Test: `backend/tests/test_ym_fixes.py` (append)

- [ ] **Step 1: Append failing tests to `test_ym_fixes.py`**

Add to the end of `backend/tests/test_ym_fixes.py`:

```python
# ── Task 2: _build_params dispatch ────────────────────────────

from yandex_market import YandexMarketClient


def _make_ym():
    return YandexMarketClient("fake-key", "fake-campaign", "fake-business")


def test_build_params_numeric_value_uses_value_id():
    """When stored value is a numeric string (enum valueId), send valueId: int."""
    ym = _make_ym()
    resolved = {"params_values": {"ym_12345": "67890"}}
    params = ym._build_params({}, "458", resolved)
    assert params == [{"parameterId": 12345, "valueId": 67890}]


def test_build_params_text_value_uses_value_string():
    """When stored value is text (old save or free-text field), send value: str."""
    ym = _make_ym()
    resolved = {"params_values": {"ym_12345": "Li-Ion"}}
    params = ym._build_params({}, "458", resolved)
    assert params == [{"parameterId": 12345, "value": "Li-Ion"}]


def test_build_params_skips_empty_values():
    """Empty string values must be skipped."""
    ym = _make_ym()
    resolved = {"params_values": {"ym_12345": "", "ym_99999": "67890"}}
    params = ym._build_params({}, "458", resolved)
    assert len(params) == 1
    assert params[0]["parameterId"] == 99999


def test_build_params_multivalue_list():
    """List values produce one entry per item."""
    ym = _make_ym()
    resolved = {"params_values": {"ym_12345": ["11111", "22222"]}}
    params = ym._build_params({}, "458", resolved)
    assert len(params) == 2
    assert {"parameterId": 12345, "valueId": 11111} in params
    assert {"parameterId": 12345, "valueId": 22222} in params


def test_build_params_mixed_list_text_and_id():
    """Mixed list: numeric items → valueId, text items → value."""
    ym = _make_ym()
    resolved = {"params_values": {"ym_12345": ["67890", "some-text"]}}
    params = ym._build_params({}, "458", resolved)
    assert {"parameterId": 12345, "valueId": 67890} in params
    assert {"parameterId": 12345, "value": "some-text"} in params
```

- [ ] **Step 2: Run to see failures**

```
cd market/backend
python -m pytest tests/test_ym_fixes.py::test_build_params_numeric_value_uses_value_id -v
```

Expected: FAILED — currently returns `{"parameterId": 12345, "value": "67890"}` not `valueId`.

- [ ] **Step 3: Fix `_build_params()` in `yandex_market.py`**

Find the method at ~line 547. Replace the `result.append` calls with smart dispatch:

```python
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
                vid = int(v)
                result.append({"parameterId": pid, "valueId": vid})
            except (ValueError, TypeError):
                result.append({"parameterId": pid, "value": str(v)})
    return result
```

- [ ] **Step 4: Run all param tests**

```
cd market/backend
python -m pytest tests/test_ym_fixes.py -k "build_params" -v
```

Expected: 5 PASSED.

- [ ] **Step 5: Run the full test suite to check for regressions**

```
cd market/backend
python -m pytest tests/ -v
```

Expected: all tests pass.

- [ ] **Step 6: Commit**

```
git add backend/yandex_market.py backend/tests/test_ym_fixes.py
git commit -m "fix: send valueId for numeric enum params in YM parameterValues, backward compat for text values"
```

---

## Task 3: Remove false "Не заполнено" from `validate_offer_state()`

**Files:**
- Modify: `backend/yandex_market.py` (~line 325)

- [ ] **Step 1: Open `yandex_market.py` and find the REQUIRED list**

Locate the `validate_offer_state` method. Find the `REQUIRED` list (~line 325):

```python
REQUIRED = [
    ("name", "Название"),
    ("description", "Описание"),
    ("vendor", "Бренд"),
    ("marketCategoryId", "Категория"),
    ("pictures", "Изображения"),
    ("basicPrice", "Цена"),
    ("weightDimensions", "Габариты/вес"),
    ("parameterValues", "Характеристики категории"),
]
```

- [ ] **Step 2: Remove the two unreliable fields**

Replace the REQUIRED list with:

```python
REQUIRED = [
    ("name",             "Название"),
    ("description",      "Описание"),
    ("vendor",           "Бренд"),
    ("pictures",         "Изображения"),
    ("basicPrice",       "Цена"),
    ("weightDimensions", "Габариты/вес"),
]
```

`marketCategoryId` and `parameterValues` are removed — YM does not return these fields reliably in the `offer-mappings` response immediately after upload (async processing). Checking them always produces a false positive.

- [ ] **Step 3: Run tests**

```
cd market/backend
python -m pytest tests/ -v
```

Expected: all pass.

- [ ] **Step 4: Commit**

```
git add backend/yandex_market.py
git commit -m "fix: remove marketCategoryId and parameterValues from post-upload required fields check (YM async processing)"
```

---

## Task 4: Filter non-critical fields from SSE "Не заполнено" warning

**Files:**
- Modify: `backend/main.py` (~line 549)

- [ ] **Step 1: Find the log line in the YM SSE handler**

Locate ~line 549 in the `ym_upload_stream` function:

```python
if missing:
    yield evt("log", level="warn",
              msg=f"    Не заполнено: {', '.join(missing[:6])}{'…' if len(missing)>6 else ''}")
```

- [ ] **Step 2: Replace with filtered version**

Only warn for fields that are genuinely critical and can be verified immediately (name, price, images). Category and characteristics take time to process on YM's side.

```python
CRITICAL_MISSING = {"Название", "Цена", "Изображения"}
critical = [f for f in missing if f in CRITICAL_MISSING]
if critical:
    yield evt("log", level="warn",
              msg=f"    Не заполнено: {', '.join(critical)}")
elif missing:
    yield evt("log", level="info",
              msg=f"    ЯМ обрабатывает асинхронно, проверьте кабинет через несколько минут")
```

- [ ] **Step 3: Run tests**

```
cd market/backend
python -m pytest tests/ -v
```

Expected: all pass.

- [ ] **Step 4: Commit**

```
git add backend/main.py
git commit -m "fix: suppress false YM post-upload warnings for category/characteristics (async processing)"
```

---

## Task 5: Fix frontend `_pmParamRow()` — store `valueId` in select, backward compat

**Files:**
- Modify: `frontend/index.html` (~line 2343)

- [ ] **Step 1: Find `_pmParamRow` in `index.html`**

Locate the function at ~line 2343. Find the ENUM select block (~line 2357):

```javascript
if (p.type === 'ENUM' && (p.allowed_values || []).length) {
    const opts = ['<option value="">— не указано —</option>'].concat(
      p.allowed_values.map(av => `<option value="${esc(av.value)}" ${av.value === curVal ? 'selected' : ''}>${esc(av.value)}</option>`)
    ).join('');
    ctrl = `<select ${onch}>${opts}</select>`;
}
```

- [ ] **Step 2: Replace the ENUM block with the fixed version**

```javascript
if (p.type === 'ENUM' && (p.allowed_values || []).length) {
    // Resolve curVal to numeric ID for backward compat:
    // old saves store display text ("Li-Ion"), new saves store numeric ID ("67890").
    let resolvedVal = curVal;
    if (curVal && isNaN(Number(curVal))) {
      const matched = (p.allowed_values || []).find(av => String(av.value) === curVal);
      if (matched && matched.id != null) resolvedVal = String(matched.id);
    }
    const opts = ['<option value="">— не указано —</option>'].concat(
      p.allowed_values.map(av => {
        const optVal = av.id != null ? String(av.id) : esc(av.value);
        return `<option value="${optVal}" ${optVal === resolvedVal ? 'selected' : ''}>${esc(av.value)}</option>`;
      })
    ).join('');
    ctrl = `<select ${onch}>${opts}</select>`;
}
```

Key changes:
- `optVal` = numeric `av.id` (the YM valueId) when available, otherwise display text as fallback
- `resolvedVal` = backward-compat resolution: text like "Li-Ion" → numeric ID by reverse lookup
- Display text between `<option>` tags is unchanged (`av.value`)

- [ ] **Step 3: Manually test in the browser**

Start the server:
```
cd market
docker compose up --build
```

Open `http://localhost:8000`, load a product, open the YM characteristics modal (🔧 кнопка).

Check these scenarios:
1. **New selection**: open a product with no saved params → select an enum value (e.g. "Li-Ion") → Save → reopen modal → correct option pre-selected ✓
2. **Old text value**: if a product has an old text-based saved value → modal opens → option is still pre-selected (backward compat) ✓
3. **Boolean params**: still show Да/Нет correctly (not affected by this change) ✓
4. **Text input params**: still editable text fields (not affected) ✓

- [ ] **Step 4: Verify the stored value is numeric after a new save**

Open browser DevTools → Network tab → save params → look at the `POST /api/answers/save` request body. The `params_values` should contain entries like `{"ym_12345": "67890"}` (numeric string), not `{"ym_12345": "Li-Ion"}`.

- [ ] **Step 5: Test full upload flow**

Load a product that has saved enum params → run upload to YM (Dry Run first to see parameterValues count). Then Live upload → check YM cabinet after a few minutes to confirm characteristics applied.

- [ ] **Step 6: Commit**

```
git add frontend/index.html
git commit -m "fix: store numeric valueId in YM enum select options for correct parameterValues format"
```

---

## Task 6: Verify full-size photo download end-to-end

**Files:** No code changes — this is a verification task.

- [ ] **Step 1: Load a product with photos**

Open `http://localhost:8000`, add a product code that has images in МС. Check the log: the SSE should show `✓ Фото: N шт.`

- [ ] **Step 2: Check downloaded file size**

After upload, the images are saved to `backend/media/uploads/{article}/ym_proc/`. Check the file size:

```
# In docker container or directly on disk:
ls -lh backend/media/uploads/
```

Full-size images should be significantly larger than thumbnails (typically 200KB–5MB vs 5–30KB for thumbnails).

- [ ] **Step 3: If images fail to download (401/403)**

МС `downloadHref` URLs require Bearer auth — `_prepare_images` already handles this via:
```python
need_auth = "api.moysklad.ru" in url or "moysklad.ru" in url
raw = await _download_bytes(url, ms_auth if need_auth else None)
```

If you see auth errors, verify `MS_TOKEN` is set correctly in `.env`.
