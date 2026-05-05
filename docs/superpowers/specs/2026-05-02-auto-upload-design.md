# Auto Upload from ⚡ Авто Button — Design Spec
Date: 2026-05-02

## Overview

Extend the existing `⚡ Авто` button on product cards (YM and Ozon) to upload the product
to the marketplace immediately after successful characteristic fill, without leaving the page.

---

## Goals

- One click does everything: fill characteristics → confirm → upload
- Status visible directly on the card (no separate page/modal needed)
- No backend changes — reuse existing `/api/ym/upload/stream` and `/api/ozon/upload/stream`

---

## Scope

Only **YM** and **Ozon** — both have `⚡ Авто` buttons. WB does not have this button and
is out of scope.

---

## Architecture

Single file change: `frontend/index.html`

### HTML changes (2 places)

Add an upload-status span after the existing score span in each marketplace's card template:

**Ozon** (~line 1481):
```html
<span id="auto-upload-status-oz-${esc(r.code)}"
      style="font-size:11px;font-weight:700;display:none"></span>
```

**YM** (~line 1944):
```html
<span id="auto-upload-status-ym-${esc(r.code)}"
      style="font-size:11px;font-weight:700;display:none"></span>
```

### JS changes (1 place)

`autoFillCard(mp, code)` (~line 2791) — after the score badge is shown (after line 2842),
add upload flow:

```
1. confirm(`Загрузить ${code} на ${mp === 'oz' ? 'Ozon' : 'Яндекс.Маркет'}?`)
2. If cancelled → return
3. Show status span: "⏳ Загружаем…" (white)
4. Fetch upload/stream endpoint with dry_run: false, codes: [code]
5. Read SSE stream until "finish" event
6. On finish.success > 0 → status: "✓ Загружен" (green)
   On finish.success == 0 → status: "✗ Ошибка" (red)
   On network error → status: "✗ Ошибка сети" (red)
```

---

## Data Flow

```
autoFillCard(mp, code)
  → POST /api/card/fill          (existing — fills characteristics)
  → POST /api/answers/save       (existing — saves to answers_store)
  → opens modal (existing)
  → shows score badge (existing)
  → [NEW] confirm dialog
  → [NEW] POST /api/{mp}/upload/stream  SSE stream
      events: start / progress / log / item / finish
      only "finish" event used for status update
  → [NEW] update status span
```

---

## Upload Endpoint Details

**YM:** `POST /api/ym/upload/stream`
```json
{
  "ms_token": "...", "ym_api_key": "...",
  "ym_campaign_id": "...", "ym_business_id": "...",
  "codes": ["18385"], "dry_run": false
}
```

**Ozon:** `POST /api/ozon/upload/stream`
```json
{
  "ms_token": "...", "ozon_client_id": "...", "ozon_api_key": "...",
  "codes": ["18385"], "dry_run": false, "force_update": false
}
```

Both return SSE stream. Only the `finish` event is needed:
```json
{"type": "finish", "success": 1, "errors": 0, "total": 1}
```

---

## Status Span States

| State | Text | Color |
|-------|------|-------|
| Uploading | ⏳ Загружаем… | white (var(--text)) |
| Success | ✓ Загружен | green (var(--green)) |
| Error | ✗ Ошибка | red (var(--red)) |
| Network error | ✗ Ошибка сети | red (var(--red)) |

---

## Error Handling

| Situation | Behavior |
|-----------|----------|
| User cancels confirm | Return, no upload |
| Upload API returns error JSON | Show ✗ Ошибка, log to console |
| Network failure | Show ✗ Ошибка сети |
| finish.success == 0 (dry_run mismatch etc.) | Show ✗ Ошибка |

The button stays enabled during upload (fill already completed).
The status span persists on the card until page refresh.

---

## Scope Boundaries

- Does NOT add progress bar (just status text — one product uploads fast)
- Does NOT log SSE events to UI (console only)
- Does NOT open a new page or modal for upload
- Does NOT change WB
- Does NOT change backend
