#!/usr/bin/env python3
"""
Batch product enrichment script for MarketSync.

Usage:
  docker exec -it market-backend-1 python enrich_script.py
  docker exec -it market-backend-1 python enrich_script.py 18385 18389 17543
"""

from __future__ import annotations
import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Optional

# ── Backend imports (same directory) ─────────────────────────
from moysklad import MoySkladClient
from bh_playwright import get_bh_data
from ai_gemini import auto_enrich as gemini_auto_enrich, enrich_category_params
from spec_extractor import extract_specs, get_category_rules, format_for_ai
from yandex_market import YandexMarketClient, get_ym_categories, get_category_parameters_cached
from ozon import OzonClient
from wb import WildberriesClient

# ── File paths ────────────────────────────────────────────────
_DIR           = Path(__file__).parent
ANSWERS_FILE   = _DIR / "answers.json"
OZON_CATS_FILE = _DIR / "ozon_categories.json"
WB_CATS_FILE   = _DIR / "wb_categories.json"

# ── API credentials from environment ─────────────────────────
MS_TOKEN       = os.environ.get("MS_TOKEN", "")
YM_API_KEY     = os.environ.get("YM_API_KEY", "")
YM_CAMPAIGN_ID = os.environ.get("YM_CAMPAIGN_ID", "")
YM_BUSINESS_ID = os.environ.get("YM_BUSINESS_ID", "")
OZON_CLIENT_ID = os.environ.get("OZON_CLIENT_ID", "")
OZON_API_KEY   = os.environ.get("OZON_API_KEY", "")
WB_API_KEY     = os.environ.get("WB_API_KEY", "")
GEMINI_KEY     = os.environ.get("GEMINI_API_KEY", "")


# ─────────────────────────────────────────────────────────────
# Pure utility functions (all testable without external deps)
# ─────────────────────────────────────────────────────────────

def _load_json(path: Path) -> dict:
    """Safely read a JSON file; return {} on any error."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            print(f"[warn] {path.name} содержит не-dict JSON, пропущено", file=sys.stderr)
            return {}
        return data
    except FileNotFoundError:
        return {}
    except Exception as exc:
        print(f"[warn] не удалось прочитать {path.name}: {exc}", file=sys.stderr)
        return {}


def load_answers() -> dict:
    """Load answers.json; return {} if file missing or corrupt."""
    return _load_json(ANSWERS_FILE)


def save_answers(answers: dict) -> None:
    """Write answers dict to answers.json."""
    ANSWERS_FILE.write_text(
        json.dumps(answers, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def merge_answers(existing: dict, new: dict) -> dict:
    """
    Merge new values into existing without overwriting non-empty keys.
    For nested dicts (params_values, ozon_attrs, wb_chars): add new keys only.
    """
    result = dict(existing)
    for k, v in new.items():
        cur = result.get(k)
        if cur in (None, "", {}, []):
            result[k] = v
        elif isinstance(v, dict) and isinstance(cur, dict):
            inner = dict(cur)  # start with existing
            for ik, iv in v.items():
                if inner.get(ik) in (None, "", {}, []):
                    inner[ik] = iv   # fill empty nested keys from new
            result[k] = inner
        # else: existing non-empty scalar wins, do nothing
    return result


def _confidence_badge(conf: float) -> str:
    """Return ✓ / ? / ✗ based on confidence threshold."""
    if conf >= 0.75:
        return "✓"
    if conf >= 0.50:
        return "?"
    return "✗"


def _truncate(s: str, n: int) -> str:
    """Truncate string to n characters, appending … if cut."""
    return s[: n - 1] + "…" if len(s) > n else s


def _parse_correction(line: str) -> Optional[tuple[str, str, str]]:
    """
    Parse user correction command.
    Returns (code, marketplace, value) or None.

    Valid forms:
      "18385 ym 90566"   → ("18385", "ym", "90566")
      "18385 oz"         → ("18385", "oz", "")
      "18385 skip"       → ("18385", "skip", "")
    """
    parts = line.strip().split(maxsplit=2)
    if len(parts) < 2:
        return None
    code = parts[0]
    mp   = parts[1].lower()
    val  = parts[2] if len(parts) > 2 else ""
    if mp not in ("ym", "oz", "wb", "skip"):
        return None
    return (code, mp, val)


# ─────────────────────────────────────────────────────────────
# Phase 1: Parallel MS + B&H data loading
# ─────────────────────────────────────────────────────────────

async def load_all_data(codes: list[str]) -> list[dict]:
    """
    Fetch МойСклад product data + B&H specs for every code in parallel.

    Returns list of item dicts:
      ok=True:  {code, ok, product, bh_data, bh_found, bh_specs}
      ok=False: {code, ok, error}
    """
    ms = MoySkladClient(MS_TOKEN)

    async def _load_one(code: str) -> dict:
        product = await ms.get_product_data(code)
        if not product.get("ok"):
            print(f"  МС: ✗ {code}  ({product.get('error', '?')})")
            return {"code": code, "ok": False, "error": product.get("error", "?")}

        print(f"  МС: ✓ {code}", flush=True)

        attrs  = product.get("attributes") or {}
        brand  = attrs.get("Бренд") or product.get("brand", "")
        art    = product.get("article", "")
        try:
            bh_raw = await get_bh_data(product["name"], brand=brand, article=art)
        except Exception:
            bh_raw = {"found": False}

        bh_found = bool(bh_raw and bh_raw.get("found"))
        bh_flag  = "✓" if bh_found else "✗"
        print(f"  B&H: {bh_flag} {code}", flush=True)

        return {
            "code":     code,
            "ok":       True,
            "product":  product,
            "bh_data":  bh_raw or {"found": False},
            "bh_found": bh_found,
            "bh_specs": (bh_raw or {}).get("specs") or {},
        }

    print(f"\nЗагружаем данные для {len(codes)} товаров...")
    results = await asyncio.gather(*[_load_one(c) for c in codes])
    ok_count  = sum(1 for r in results if r.get("ok"))
    bh_count  = sum(1 for r in results if r.get("bh_found"))
    print(f"\n  Итого: МС ✓{ok_count}/{len(codes)}  B&H ✓{bh_count}/{len(codes)}")
    return list(results)


# ─────────────────────────────────────────────────────────────
# Phase 2: AI category suggestion + table display
# ─────────────────────────────────────────────────────────────

async def suggest_categories(items: list[dict]) -> list[dict]:
    """
    Run gemini_auto_enrich in parallel for all successfully loaded items.
    Returns items list with 'ai' key added to each ok item.
    """
    ym_cats = get_ym_categories() or {}
    oz_cats = _load_json(OZON_CATS_FILE)
    wb_cats = _load_json(WB_CATS_FILE)

    ok_items = [i for i in items if i.get("ok")]
    if not ok_items:
        print("\nНет товаров для обработки.")
        return items

    print(f"\nGemini: подбор категорий для {len(ok_items)} товаров...")

    async def _enrich_one(item: dict) -> dict:
        result = await gemini_auto_enrich(
            product         = item["product"],
            saved           = {},
            ym_categories   = ym_cats,
            ym_params       = [],
            ozon_categories = oz_cats,
            wb_categories   = wb_cats,
            bh_specs        = item["bh_specs"],
            api_key         = GEMINI_KEY,
        )
        if result.get("error"):
            print(f"  Gemini ✗ {item['code']}: {result['error'][:80]}")
        else:
            print(f"  Gemini ✓ {item['code']}")
        return {**item, "ai": result}

    enriched = await asyncio.gather(*[_enrich_one(i) for i in ok_items])
    enriched_map = {e["code"]: e for e in enriched}
    return [enriched_map.get(i["code"], i) for i in items]


def build_table_rows(items: list[dict]) -> list[dict]:
    """Build display rows from loaded+enriched items."""
    ym_cats = get_ym_categories() or {}
    oz_cats = _load_json(OZON_CATS_FILE)
    wb_cats = _load_json(WB_CATS_FILE)
    rows = []

    for item in items:
        if not item.get("ok"):
            rows.append({
                "code":  item["code"],
                "name":  f"[ОШИБКА: {item.get('error', '?')}]",
                "error": True,
            })
            continue

        ai   = item.get("ai") or {}
        conf = float(ai.get("overall_confidence") or 0)

        ym_cat_id  = ai.get("ym_category_id")
        oz_cat_key = ai.get("ozon_category_key")
        wb_cat_key = ai.get("wb_category_key")

        ym_name = ym_cats.get(str(ym_cat_id), {}).get("name", "") if ym_cat_id else ""
        oz_name = oz_cats.get(oz_cat_key, {}).get("name", "")      if oz_cat_key else ""
        wb_name = wb_cats.get(wb_cat_key, {}).get("name", "")      if wb_cat_key else ""

        rows.append({
            "code":       item["code"],
            "name":       item["product"].get("name", ""),
            "bh_found":   item.get("bh_found", False),
            "ym_cat_id":  ym_cat_id,
            "ym_name":    ym_name,
            "ym_conf":    conf,
            "oz_cat_key": oz_cat_key,
            "oz_name":    oz_name,
            "oz_conf":    conf,
            "wb_cat_key": wb_cat_key,
            "wb_name":    wb_name,
            "wb_conf":    conf,
        })

    return rows


def print_category_table(rows: list[dict]) -> None:
    """Print formatted category suggestion table."""
    W_CODE = 6
    W_NAME = 28
    W_CAT  = 24

    def _fmt_cat(name: str, conf: float) -> str:
        if not name:
            return _truncate("—", W_CAT)
        badge = _confidence_badge(conf)
        return _truncate(f"{name} [{conf:.2f}]{badge}", W_CAT)

    sep = "─" * (W_CODE + 1 + W_NAME + 1 + W_CAT * 3 + 2)
    print(f"\n{sep}")
    print(
        f"{'Код':<{W_CODE}} {'Товар':<{W_NAME}} "
        f"{'ЯМ (conf)':<{W_CAT}} {'Ozon (conf)':<{W_CAT}} {'WB (conf)':<{W_CAT}}"
    )
    print(sep)

    for r in rows:
        if r.get("error"):
            print(f"{r['code']:<{W_CODE}} {_truncate(r['name'], W_NAME + W_CAT * 3 + 2)}")
            continue

        bh_flag = "" if r.get("bh_found") else "[B&H:✗] "
        name_str = _truncate(bh_flag + r.get("name", ""), W_NAME)

        print(
            f"{r['code']:<{W_CODE}} {name_str:<{W_NAME}} "
            f"{_fmt_cat(r.get('ym_name',''), r.get('ym_conf',0)):<{W_CAT}} "
            f"{_fmt_cat(r.get('oz_name',''), r.get('oz_conf',0)):<{W_CAT}} "
            f"{_fmt_cat(r.get('wb_name',''), r.get('wb_conf',0)):<{W_CAT}}"
        )

    print(sep)


# ─────────────────────────────────────────────────────────────
# Phase 3: Interactive category confirmation
# ─────────────────────────────────────────────────────────────

def _show_leaf_categories(cats: dict, mp: str, name_hint: str = "", limit: int = 25) -> None:
    """
    Print a numbered list of leaf categories filtered by name_hint keywords.
    mp: 'oz' uses has_children flag; 'wb' uses s_* key prefix.
    """
    if mp == "wb":
        leaves = [(k, v) for k, v in cats.items() if k.startswith("s_")]
    else:
        leaves = [(k, v) for k, v in cats.items() if not v.get("has_children", True)]

    if name_hint:
        keywords = [w.lower() for w in name_hint.split() if len(w) > 2]
        scored = []
        for k, v in leaves:
            path_str = " ".join(v.get("path") or [v.get("name", "")]).lower()
            score    = sum(1 for kw in keywords if kw in path_str)
            scored.append((score, k, v))
        scored.sort(reverse=True)
        shown = [(k, v) for _, k, v in scored[:limit]]
    else:
        shown = leaves[:limit]

    print(f"\nТоп {len(shown)} категорий ({mp.upper()}):")
    for i, (k, v) in enumerate(shown, 1):
        path_str = " > ".join(v.get("path") or [v.get("name", "")])
        print(f"  {i:3}. [{k}]  {path_str}")
    print()


def confirm_categories(rows: list[dict], items_by_code: dict[str, dict]) -> dict[str, dict]:
    """
    Interactively confirm or correct AI-suggested categories.

    Returns dict: {code: {ym_cat_id, oz_cat_key, wb_cat_key}}
    Codes not in this dict were skipped.
    """
    confirmed: dict[str, dict] = {}
    for r in rows:
        if r.get("error"):
            continue
        confirmed[r["code"]] = {
            "ym_cat_id":  r.get("ym_cat_id"),
            "oz_cat_key": r.get("oz_cat_key"),
            "wb_cat_key": r.get("wb_cat_key"),
        }

    print("\nВведите Enter чтобы принять все категории, или укажите правки:")
    print("  18385 ym 90566   — установить категорию ЯМ")
    print("  18385 oz         — выбрать категорию Ozon из списка")
    print("  18385 wb         — выбрать категорию WB из списка")
    print("  18385 skip       — пропустить товар")

    oz_cats = _load_json(OZON_CATS_FILE)
    wb_cats = _load_json(WB_CATS_FILE)

    while True:
        try:
            line = input("\n> ").strip()
        except (EOFError, KeyboardInterrupt):
            break

        if not line:
            break

        parsed = _parse_correction(line)
        if not parsed:
            print("  Неверный формат. Пример: 18385 ym 90566")
            continue

        code, mp, val = parsed

        if code not in confirmed:
            print(f"  Код {code!r} не найден в списке")
            continue

        if mp == "skip":
            del confirmed[code]
            print(f"  {code} пропущен")
            continue

        prod_name = items_by_code.get(code, {}).get("product", {}).get("name", "")

        if mp == "ym":
            confirmed[code]["ym_cat_id"] = val or None
            ym_cats = get_ym_categories()
            name    = ym_cats.get(str(val), {}).get("name", val) if val else "—"
            print(f"  ЯМ категория {code}: {name}")

        elif mp == "oz":
            if val:
                confirmed[code]["oz_cat_key"] = val
                name = oz_cats.get(val, {}).get("name", val)
                print(f"  Ozon категория {code}: {name}")
            else:
                _show_leaf_categories(oz_cats, "oz", prod_name)
                key = input("  Введите key категории (например 17027823_970491011): ").strip()
                confirmed[code]["oz_cat_key"] = key or None
                if key:
                    print(f"  Ozon категория {code}: {oz_cats.get(key, {}).get('name', key)}")

        elif mp == "wb":
            if val:
                confirmed[code]["wb_cat_key"] = val
                name = wb_cats.get(val, {}).get("name", val)
                print(f"  WB категория {code}: {name}")
            else:
                _show_leaf_categories(wb_cats, "wb", prod_name)
                key = input("  Введите key категории (например s_5462): ").strip()
                confirmed[code]["wb_cat_key"] = key or None
                if key:
                    print(f"  WB категория {code}: {wb_cats.get(key, {}).get('name', key)}")

    print(f"\nПодтверждено: {len(confirmed)} товаров")
    return confirmed


# ─────────────────────────────────────────────────────────────
# Phase 4: Characteristics fill (3 layers) + answers.json save
# ─────────────────────────────────────────────────────────────

async def _fill_marketplace(
    product:   dict,
    mp:        str,
    attrs:     list,
    bh_specs:  dict,
    extracted: dict,
) -> dict:
    """
    Apply rule-based (layer 2) and Gemini (layer 3) enrichment.
    Returns merged params dict. Rules win over AI on conflict.
    """
    prefix = {"ym": "ym_", "ozon": "oz_", "wb": "wb_"}[mp]

    # Layer 2: rule-based
    rules: dict = {}
    for a in attrs:
        pid = a.get("id")
        if not pid:
            continue
        aname   = a.get("name", "").lower()
        allowed = [
            v.get("value") or v.get("name")
            for v in (a.get("values") or a.get("dictionary") or [])
            if isinstance(v, dict) and (v.get("value") or v.get("name"))
        ]
        val = get_category_rules(extracted, aname, allowed or None)
        if val:
            rules[f"{prefix}{pid}"] = val

    # Layer 3: Gemini
    ai_params: dict = {}
    if GEMINI_KEY and attrs:
        try:
            result    = await enrich_category_params(
                product         = product,
                marketplace     = mp,
                attributes      = attrs,
                bh_specs        = bh_specs,
                extracted_specs = extracted,
                api_key         = GEMINI_KEY,
            )
            ai_params = result.get("params") or {}
        except Exception as e:
            print(f"    Gemini fill error ({mp}): {e}")

    return {**ai_params, **rules}   # rules win


async def fill_and_save(items: list[dict], confirmed: dict[str, dict]) -> list[dict]:
    """
    For each confirmed product: fetch marketplace category attributes,
    run 3-layer fill for YM/Ozon/WB, merge into answers.json.
    """
    ym  = YandexMarketClient(YM_API_KEY, YM_CAMPAIGN_ID, YM_BUSINESS_ID)
    oz  = OzonClient(OZON_CLIENT_ID, OZON_API_KEY)
    wb  = WildberriesClient(WB_API_KEY)
    oz_cats = _load_json(OZON_CATS_FILE)
    wb_cats = _load_json(WB_CATS_FILE)

    to_process = [i for i in items if i.get("ok") and i["code"] in confirmed]
    print(f"\nЗаполняем характеристики для {len(to_process)} товаров...")

    async def _process_one(item: dict) -> dict:
        code     = item["code"]
        product  = item["product"]
        bh_specs = item.get("bh_specs") or {}
        ai       = item.get("ai") or {}
        cats     = confirmed[code]

        name = product.get("name", "")
        desc = product.get("description", "") or ""
        extracted = extract_specs(name, desc, bh_specs)

        new_ans: dict = {
            "brand": (
                ai.get("brand")
                or (product.get("attributes") or {}).get("Бренд", "")
                or product.get("brand", "")
            ),
            "description": ai.get("description") or desc,
        }

        ym_cat_id  = cats.get("ym_cat_id")
        oz_cat_key = cats.get("oz_cat_key")
        wb_cat_key = cats.get("wb_cat_key")

        ym_filled = oz_filled = wb_filled = 0

        # ── YM ────────────────────────────────────────────────
        if ym_cat_id:
            new_ans["category"] = str(ym_cat_id)
            try:
                attrs = await get_category_parameters_cached(ym, str(ym_cat_id))
                if attrs:
                    params = await _fill_marketplace(product, "ym", attrs, bh_specs, extracted)
                    new_ans["params_values"] = params
                    ym_filled = len(params)
            except Exception as e:
                print(f"  {code} ЯМ ошибка: {e}")

        # ── Ozon ──────────────────────────────────────────────
        if oz_cat_key:
            entry = oz_cats.get(oz_cat_key) or {}
            new_ans["ozon_category_key"] = oz_cat_key
            if entry.get("desc_cat_id"):
                new_ans["ozon_category_id"] = entry["desc_cat_id"]
            if entry.get("type_id"):
                new_ans["ozon_type_id"] = entry["type_id"]
            try:
                parts       = str(oz_cat_key).split("_")
                desc_cat_id = int(parts[0])
                type_id     = int(parts[1]) if len(parts) > 1 else None
                r = await oz.get_category_attributes(desc_cat_id, type_id)
                if r.get("ok"):
                    attrs = r.get("attributes", [])
                    params = await _fill_marketplace(product, "ozon", attrs, bh_specs, extracted)
                    new_ans["ozon_attrs"] = params
                    oz_filled = len(params)
            except Exception as e:
                print(f"  {code} Ozon ошибка: {e}")

        # ── WB ────────────────────────────────────────────────
        if wb_cat_key:
            entry      = wb_cats.get(wb_cat_key) or {}
            subject_id = entry.get("int_id")
            new_ans["wb_category_key"] = wb_cat_key
            if subject_id:
                new_ans["wb_subject_id"] = subject_id
            if subject_id:
                try:
                    r = await wb.get_characteristics(int(subject_id))
                    if r.get("ok"):
                        attrs = r.get("characteristics", [])
                        params = await _fill_marketplace(product, "wb", attrs, bh_specs, extracted)
                        new_ans["wb_chars"] = params
                        wb_filled = len(params)
                except Exception as e:
                    print(f"  {code} WB ошибка: {e}")

        print(f"  ✓ {code}  ЯМ:{ym_filled}  Ozon:{oz_filled}  WB:{wb_filled}")
        return {
            "code":      code,
            "name":      name,
            "new_ans":   new_ans,
            "ym_filled": ym_filled,
            "oz_filled": oz_filled,
            "wb_filled": wb_filled,
            "bh_found":  item.get("bh_found", False),
        }

    results = await asyncio.gather(*[_process_one(i) for i in to_process])

    # Merge into answers.json
    answers = load_answers()
    for r in results:
        code     = r["code"]
        existing = answers.get(code, {})
        answers[code] = merge_answers(existing, r["new_ans"])
    save_answers(answers)
    print(f"\nСохранено в {ANSWERS_FILE}")

    return list(results)


def print_report(results: list[dict]) -> None:
    """Print final summary of what was filled."""
    print("\n" + "=" * 60)
    print("РЕЗУЛЬТАТ")
    print("=" * 60)
    for r in results:
        bh_note = "" if r.get("bh_found") else "  [B&H: не найден]"
        total   = r.get("ym_filled", 0) + r.get("oz_filled", 0) + r.get("wb_filled", 0)
        icon    = "✓" if total > 0 else "?"
        print(f"\n{icon} {r['code']}  {_truncate(r.get('name',''), 50)}{bh_note}")
        print(f"    ЯМ:    {r.get('ym_filled', 0)} характеристик")
        print(f"    Ozon:  {r.get('oz_filled', 0)} характеристик")
        print(f"    WB:    {r.get('wb_filled', 0)} характеристик")
        print(f"    → answers.json ✓")
    print()


# ─────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────

def get_input_codes() -> list[str]:
    """Read codes from CLI args or interactive prompt."""
    if len(sys.argv) > 1:
        return [c.strip() for c in sys.argv[1:] if c.strip()]
    print("MarketSync — пакетное обогащение товаров")
    raw = input("Введите коды товаров через пробел или запятую:\n> ")
    return [c.strip() for c in raw.replace(",", " ").split() if c.strip()]


async def main() -> None:
    codes = get_input_codes()
    if not codes:
        print("Коды не указаны. Выход.")
        return

    if not MS_TOKEN:
        print("ОШИБКА: MS_TOKEN не задан в .env")
        sys.exit(1)
    if not GEMINI_KEY:
        print("ПРЕДУПРЕЖДЕНИЕ: GEMINI_API_KEY не задан — AI заполнение пропускается")

    # Phase 1: load MS + B&H data in parallel
    items = await load_all_data(codes)

    # Phase 2: Gemini category suggestion in parallel
    items = await suggest_categories(items)

    # Display category table
    rows = build_table_rows(items)
    print_category_table(rows)

    # Phase 3: interactive confirmation
    items_by_code = {i["code"]: i for i in items}
    confirmed = confirm_categories(rows, items_by_code)
    if not confirmed:
        print("Нет подтверждённых товаров. Выход.")
        return

    # Phase 4: fill characteristics + save
    results = await fill_and_save(items, confirmed)

    # Print report
    print_report(results)


if __name__ == "__main__":
    asyncio.run(main())
