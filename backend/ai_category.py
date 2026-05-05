# ai_category.py — AI-подбор категорий (Claude или OpenAI)
# Авто-определение движка по ключу: sk-ant-* → Claude, sk-* → OpenAI
# Стратегия: каскадный выбор за 2-3 небольших вызова

import re
from typing import Optional

try:
    import anthropic
    _HAS_ANTHROPIC = True
except ImportError:
    _HAS_ANTHROPIC = False

try:
    from openai import AsyncOpenAI
    _HAS_OPENAI = True
except ImportError:
    _HAS_OPENAI = False

_CLAUDE_MODEL = "claude-haiku-4-5-20251001"
_OPENAI_MODEL = "gpt-4o-mini"
_MAX_CATS_PER_CALL = 80


def _detect_engine(api_key: str) -> str:
    """Возвращает 'claude' или 'openai' по формату ключа."""
    if api_key.startswith("sk-ant"):
        return "claude"
    if api_key.startswith("sk-"):
        return "openai"
    return "claude"  # по умолчанию


def _build_product_context(name: str, description: str, brand: str) -> str:
    parts = [f"Название товара: {name}"]
    if brand:
        parts.append(f"Бренд: {brand}")
    if description:
        parts.append(f"Описание: {description[:400]}")
    return "\n".join(parts)


def _cats_to_text(cats: list[dict]) -> str:
    lines = []
    for c in cats:
        path = " › ".join(c.get("path", [c["name"]])) if len(c.get("path", [])) > 1 else c["name"]
        lines.append(f'ID={c["id"]} | {path}')
    return "\n".join(lines)


def _make_prompt(product_ctx: str, cats: list[dict], instruction: str) -> str:
    return f"""{product_ctx}

Список категорий (формат ID=... | Название):
{_cats_to_text(cats)}

{instruction}

Ответь ТОЛЬКО одной строкой вида: ID=<значение>
Ничего больше — ни объяснений, ни знаков препинания."""


async def _ask_claude(client, prompt: str) -> Optional[str]:
    msg = await client.messages.create(
        model=_CLAUDE_MODEL,
        max_tokens=64,
        messages=[{"role": "user", "content": prompt}],
    )
    text = msg.content[0].text.strip()
    m = re.search(r'ID=([^\s,]+)', text)
    return m.group(1) if m else None


async def _ask_openai(client, prompt: str) -> Optional[str]:
    resp = await client.chat.completions.create(
        model=_OPENAI_MODEL,
        max_tokens=64,
        temperature=0,
        messages=[{"role": "user", "content": prompt}],
    )
    text = resp.choices[0].message.content.strip()
    m = re.search(r'ID=([^\s,]+)', text)
    return m.group(1) if m else None


async def _ask(engine: str, client, prompt: str) -> Optional[str]:
    if engine == "openai":
        return await _ask_openai(client, prompt)
    return await _ask_claude(client, prompt)


def _children_of(cats_dict: dict, parent_id: Optional[str]) -> list[dict]:
    return sorted(
        [v for v in cats_dict.values() if v.get("parent_id") == parent_id],
        key=lambda c: c.get("name", "")
    )


def _roots(cats_dict: dict) -> list[dict]:
    return sorted(
        [v for v in cats_dict.values() if not v.get("parent_id")],
        key=lambda c: c.get("name", "")
    )


async def suggest_category(
    mp: str,
    name: str,
    description: str,
    brand: str,
    cats_dict: dict,
    api_key: str,
) -> dict:
    """
    Возвращает: {ok, category_id, category_name, path, engine, steps}
    mp: 'ym' | 'oz' | 'wb'
    """
    if not api_key:
        return {"ok": False, "error": "AI ключ не задан"}

    engine = _detect_engine(api_key)

    if engine == "claude" and not _HAS_ANTHROPIC:
        return {"ok": False, "error": "Пакет anthropic не установлен"}
    if engine == "openai" and not _HAS_OPENAI:
        return {"ok": False, "error": "Пакет openai не установлен"}

    try:
        if engine == "claude":
            client = anthropic.AsyncAnthropic(api_key=api_key)
        else:
            client = AsyncOpenAI(api_key=api_key)
    except Exception as e:
        return {"ok": False, "error": f"Ошибка инициализации клиента: {e}"}

    ctx = _build_product_context(name, description, brand)
    steps = []

    # ── Шаг 1: корневой уровень ────────────────────────────
    roots = _roots(cats_dict)
    if not roots:
        return {"ok": False, "error": "Дерево категорий пустое"}

    chosen_id = await _ask(engine, client,
        _make_prompt(ctx, roots[:_MAX_CATS_PER_CALL],
            "Выбери ОДНУ категорию верхнего уровня, которая лучше всего подходит для этого товара."))

    if not chosen_id or chosen_id not in cats_dict:
        return {"ok": False, "error": f"AI вернул неизвестный ID: {chosen_id!r}"}
    steps.append(cats_dict[chosen_id]["name"])

    # ── Шаг 2: дочерние узлы ──────────────────────────────
    children = _children_of(cats_dict, chosen_id)
    if children:
        chosen_id2 = await _ask(engine, client,
            _make_prompt(ctx, children[:_MAX_CATS_PER_CALL],
                "Выбери ОДНУ подкатегорию, которая лучше всего подходит для этого товара."))
        if chosen_id2 and chosen_id2 in cats_dict:
            chosen_id = chosen_id2
            steps.append(cats_dict[chosen_id]["name"])

            # ── Шаг 3: листья / типы ────────────────────────
            grandchildren = _children_of(cats_dict, chosen_id)
            if grandchildren:
                chosen_id3 = await _ask(engine, client,
                    _make_prompt(ctx, grandchildren[:_MAX_CATS_PER_CALL],
                        "Выбери ОДИН тип товара, который точнее всего описывает этот товар."))
                if chosen_id3 and chosen_id3 in cats_dict:
                    chosen_id = chosen_id3
                    steps.append(cats_dict[chosen_id]["name"])

    node = cats_dict.get(chosen_id, {})
    return {
        "ok": True,
        "category_id":   chosen_id,
        "category_name": node.get("name", ""),
        "path":          node.get("path", steps),
        "engine":        engine,
        "desc_cat_id":   node.get("desc_cat_id"),
        "type_id":       node.get("type_id"),
        "int_id":        node.get("int_id"),
        "steps":         steps,
    }
