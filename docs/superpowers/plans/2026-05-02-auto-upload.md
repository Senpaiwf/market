# Auto Upload from ⚡ Авто Button — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Extend the existing `⚡ Авто` button (YM and Ozon) to upload the product to the marketplace after successful fill, showing status directly on the card.

**Architecture:** Pure frontend change in `index.html`. After `autoFillCard` fills characteristics and shows the score badge, it calls a new helper `_autoUpload(mp, code)` which shows a confirm dialog and streams from the existing upload endpoint. Upload status appears in a new span next to the score badge.

**Tech Stack:** Vanilla JS, SSE (ReadableStream), existing `/api/ym/upload/stream` and `/api/ozon/upload/stream` endpoints

---

## File Map

| File | Lines | Change |
|------|-------|--------|
| `frontend/index.html` | ~1479–1483 | Add `auto-upload-status-oz-*` span in Ozon card template |
| `frontend/index.html` | ~1942–1946 | Add `auto-upload-status-ym-*` span in YM card template |
| `frontend/index.html` | after line 2852 | Add `_autoUpload(mp, code)` helper function |
| `frontend/index.html` | line ~2842 | Call `await _autoUpload(mp, code)` inside `autoFillCard` |

---

### Task 1: Add upload-status spans to card HTML templates

**Files:**
- Modify: `frontend/index.html` (~line 1481 and ~line 1944)

The Ozon and YM card rows are built as JS template literals. Each already has a score span (`auto-fill-score-*`). Add an upload-status span right after it.

- [ ] **Step 1: Add status span to Ozon card template**

Find this block in `frontend/index.html` (~line 1479):
```html
        <div class="odc-section" style="display:flex;gap:8px;align-items:center;flex-wrap:wrap">
          <button id="auto-fill-btn-oz-${esc(r.code)}" class="btn btn-sm" style="background:rgba(0,180,80,.12);color:#4ecb80;border:1px solid rgba(0,180,80,.3);font-weight:600" onclick="autoFillCard('oz','${esc(r.code)}')">⚡ Авто</button>
          <span id="auto-fill-score-oz-${esc(r.code)}" style="font-size:11px;font-weight:700;display:none"></span>
          <button class="btn btn-sm" style="background:rgba(0,91,255,.15);color:#6b9fff;border:1px solid rgba(0,91,255,.3);font-weight:600" onclick="ozPmOpen('${esc(r.code)}')">🔧 Заполнить характеристики Ozon</button>
        </div>
```

Replace with:
```html
        <div class="odc-section" style="display:flex;gap:8px;align-items:center;flex-wrap:wrap">
          <button id="auto-fill-btn-oz-${esc(r.code)}" class="btn btn-sm" style="background:rgba(0,180,80,.12);color:#4ecb80;border:1px solid rgba(0,180,80,.3);font-weight:600" onclick="autoFillCard('oz','${esc(r.code)}')">⚡ Авто</button>
          <span id="auto-fill-score-oz-${esc(r.code)}" style="font-size:11px;font-weight:700;display:none"></span>
          <span id="auto-upload-status-oz-${esc(r.code)}" style="font-size:11px;font-weight:700;display:none"></span>
          <button class="btn btn-sm" style="background:rgba(0,91,255,.15);color:#6b9fff;border:1px solid rgba(0,91,255,.3);font-weight:600" onclick="ozPmOpen('${esc(r.code)}')">🔧 Заполнить характеристики Ozon</button>
        </div>
```

- [ ] **Step 2: Add status span to YM card template**

Find this block in `frontend/index.html` (~line 1942):
```html
        <div class="odc-section" style="display:flex;gap:8px;align-items:center;flex-wrap:wrap">
          <button id="auto-fill-btn-ym-${esc(r.code)}" class="btn btn-sm" style="background:rgba(0,180,80,.12);color:#4ecb80;border:1px solid rgba(0,180,80,.3);font-weight:600" onclick="autoFillCard('ym','${esc(r.code)}')">⚡ Авто</button>
          <span id="auto-fill-score-ym-${esc(r.code)}" style="font-size:11px;font-weight:700;display:none"></span>
          <button class="btn btn-p btn-sm" onclick="pmOpen('${esc(r.code)}')">🔧 Заполнить характеристики ЯМ</button>
        </div>
```

Replace with:
```html
        <div class="odc-section" style="display:flex;gap:8px;align-items:center;flex-wrap:wrap">
          <button id="auto-fill-btn-ym-${esc(r.code)}" class="btn btn-sm" style="background:rgba(0,180,80,.12);color:#4ecb80;border:1px solid rgba(0,180,80,.3);font-weight:600" onclick="autoFillCard('ym','${esc(r.code)}')">⚡ Авто</button>
          <span id="auto-fill-score-ym-${esc(r.code)}" style="font-size:11px;font-weight:700;display:none"></span>
          <span id="auto-upload-status-ym-${esc(r.code)}" style="font-size:11px;font-weight:700;display:none"></span>
          <button class="btn btn-p btn-sm" onclick="pmOpen('${esc(r.code)}')">🔧 Заполнить характеристики ЯМ</button>
        </div>
```

- [ ] **Step 3: Verify spans appear in DOM**

Open `http://localhost:8000` in browser, load any product, open the Ozon or YM detail card. Open browser DevTools → Console, run:

```javascript
// Should find the span (replace 18385 with any code you see on screen)
document.getElementById('auto-upload-status-oz-18385')
```

Expected: a `<span>` element (not `null`). If `null`, the template literal wasn't updated correctly — check for syntax errors.

---

### Task 2: Add `_autoUpload` helper and wire it into `autoFillCard`

**Files:**
- Modify: `frontend/index.html` (~line 2852, after `autoFillCard` function)
- Modify: `frontend/index.html` (~line 2842, inside `autoFillCard` try block)

- [ ] **Step 1: Add `_autoUpload` helper after `autoFillCard`**

Find this line in `frontend/index.html` (line ~2852):
```javascript
// ════════════════ OZON PARAMS MODAL ════════════════
```

Insert the following function BEFORE that comment:

```javascript
async function _autoUpload(mp, code) {
  const mpName = mp === 'oz' ? 'Ozon' : 'Яндекс.Маркет';
  if (!confirm(`Загрузить ${code} на ${mpName}?`)) return;

  const statusEl = document.getElementById(`auto-upload-status-${mp}-${code}`);
  if (statusEl) { statusEl.textContent = '⏳ Загружаем…'; statusEl.style.color = 'var(--text)'; statusEl.style.display = 'inline'; }

  try {
    const endpoint = mp === 'oz' ? '/api/ozon/upload/stream' : '/api/ym/upload/stream';
    const body = mp === 'oz'
      ? JSON.stringify({ ms_token: S.ms_token, ozon_client_id: S.ozon_client_id, ozon_api_key: S.ozon_api_key, codes: [code], dry_run: false, force_update: false })
      : JSON.stringify({ ms_token: S.ms_token, ym_api_key: S.ym_api_key, ym_campaign_id: S.ym_campaign_id, ym_business_id: S.ym_business_id, codes: [code], dry_run: false });

    const res = await fetch(endpoint, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body });
    const reader = res.body.getReader();
    const dec = new TextDecoder();
    let buf = '';

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buf += dec.decode(value, { stream: true });
      const parts = buf.split('\n\n'); buf = parts.pop();
      for (const part of parts) {
        if (!part.startsWith('data:')) continue;
        try {
          const d = JSON.parse(part.slice(5).trim());
          if (d.type === 'finish') {
            const ok = d.success > 0;
            if (statusEl) {
              statusEl.textContent = ok ? '✓ Загружен' : '✗ Ошибка';
              statusEl.style.color = ok ? 'var(--green)' : 'var(--red)';
            }
          }
        } catch(e) {}
      }
    }
  } catch(e) {
    console.error('[AutoUpload]', e);
    if (statusEl) { statusEl.textContent = '✗ Ошибка сети'; statusEl.style.color = 'var(--red)'; }
  }
}

```

- [ ] **Step 2: Call `_autoUpload` at end of `autoFillCard` try block**

Find this block inside `autoFillCard` (~line 2836):
```javascript
    if (btn) {
      btn.textContent = '⚡ Авто';
      const badge = document.getElementById(`auto-fill-score-${mp}-${code}`);
      if (badge) {
        badge.textContent = `${score}%`;
        badge.style.color = scoreColor;
        badge.style.display = 'inline';
      }
    }

    if (result.warnings && result.warnings.length) {
      console.warn('[AutoFill] warnings:', result.warnings);
    }
```

Replace with:
```javascript
    if (btn) {
      btn.textContent = '⚡ Авто';
      const badge = document.getElementById(`auto-fill-score-${mp}-${code}`);
      if (badge) {
        badge.textContent = `${score}%`;
        badge.style.color = scoreColor;
        badge.style.display = 'inline';
      }
    }

    if (result.warnings && result.warnings.length) {
      console.warn('[AutoFill] warnings:', result.warnings);
    }

    await _autoUpload(mp, code);
```

- [ ] **Step 3: Verify in browser — happy path**

Open `http://localhost:8000`, load a product with a valid Ozon or YM category set.

Click `⚡ Авто` on an Ozon card:
1. Button shows `⏳…` while filling
2. Modal opens with filled characteristics
3. Confirm dialog appears: "Загрузить {code} на Ozon?"
4. Click OK
5. Status span shows `⏳ Загружаем…`
6. After stream completes: `✓ Загружен` (green) or `✗ Ошибка` (red)
7. Button re-enables as `⚡ Авто`

- [ ] **Step 4: Verify cancel path**

Click `⚡ Авто`, confirm dialog appears — click Отмена (Cancel).

Expected: no upload attempt, no status span visible, button re-enables normally.

Open DevTools Network tab — no request to `/api/ozon/upload/stream` should appear.

- [ ] **Step 5: Verify no category path**

Click `⚡ Авто` on a card where no category is selected.

Expected: alert "Сначала выберите категорию для этого товара" — same as before. No upload dialog. Function returns early before reaching `_autoUpload`.
