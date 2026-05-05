# backend/spec_extractor.py
# Rule-based spec extraction from product name + description.
# Used as Layer 1 before AI enrichment — gives AI pre-extracted context.

from __future__ import annotations
import re
from typing import Optional

# ─── Patterns ─────────────────────────────────────────────────

_RE_VOLT_RANGE = re.compile(r'(\d+)\s*[-–]\s*(\d+)\s*[ВвVv](?:DC|AC|Ольт)?', re.I)
_RE_VOLT       = re.compile(r'(\d+(?:[.,]\d+)?)\s*(?:[Вв]ольт|[Вв](?=[^\w]|$)|VDC|VAC|V(?=[^\w]|$))', re.I)
_RE_CURRENT_MA = re.compile(r'(\d+(?:[.,]\d+)?)\s*(?:мА|mA)\b', re.I)
_RE_CURRENT_A  = re.compile(r'(\d+(?:[.,]\d+)?)\s*(?:[Аа]мпер|[Аа](?=[^\w]|$)|A(?=[^\w]|$))', re.I)
_RE_POWER      = re.compile(r'(\d+(?:[.,]\d+)?)\s*(?:Вт|ватт|W)\b', re.I)
_RE_CAPACITY_MAH = re.compile(r'(\d+(?:[.,]\d+)?)\s*(?:мА[·\-]?ч|mAh)\b', re.I)
_RE_CAPACITY_WH  = re.compile(r'(\d+(?:[.,]\d+)?)\s*(?:Вт[·\-]?ч|Wh)\b', re.I)
_RE_FREQ       = re.compile(r'(\d+(?:[.,]\d+)?)\s*(ГГц|GHz|МГц|MHz)\b', re.I)
_RE_TEMP_RANGE = re.compile(r'(-?\d+)\s*°?\s*[CcСс]\s*[-–до\s]+\+?\s*(\d+)\s*°?\s*[CcСс]', re.I)
_RE_THREAD     = re.compile(r'(\d+/\d+)["″-]?(?:\s*-\s*\d+)?', re.I)
_RE_FILTER_D   = re.compile(r'\b(\d{2,3})\s*(?:мм|mm)\b', re.I)
_RE_RESOLUTION = re.compile(r'\b(4K|8K|6K|2\.7K|2K|1080p?|720p?|UHD|FHD|HD)\b', re.I)
_RE_COLOR_RU   = re.compile(
    r'\b(чёрн\w*|черн\w*|бел\w{2,}|серебрист\w+|серый|серая|красн\w+|синий|синяя|'
    r'зелён\w+|золотист\w+|оранжев\w+|бронзов\w+|титанов\w+)\b', re.I
)
_RE_COLOR_EN   = re.compile(
    r'\b(black|white|silver|grey|gray|red|blue|green|gold|orange|titanium|bronze|dark)\b', re.I
)
_RE_CONNECTOR  = re.compile(
    r'\b(USB[-\s]?C|USB[-\s]?A|USB[-\s]?3\.?\d?|Micro[-\s]?USB|Mini[-\s]?USB|'
    r'HDMI|Mini[-\s]?HDMI|Micro[-\s]?HDMI|'
    r'XLR|TRS|TRRS|TS\b|SDI|3G[-\s]?SDI|12G[-\s]?SDI|BNC|RCA|'
    r'Lightning|Type[-\s]?C|'
    r'3\.5\s*(?:мм|mm)|6\.3\s*(?:мм|mm))\b', re.I
)
_RE_MATERIAL   = re.compile(
    r'\b(алюмин\w+|пластик\w*|кож\w{1,4}|нейлон|стал\w+|титан\w*|карбон|'
    r'углеволокно|силикон\w*|резин\w+|alumin\w+|carbon|nylon|silicone|leather)\b', re.I
)

# Category keywords
_KW_BATTERY    = re.compile(r'\b(аккумулятор|батарея|батарейка|battery|power\s?bank|powerbank)\b', re.I)
_KW_CHARGER    = re.compile(r'\b(зарядн\w+|зарядное\s+устройство|charger|charging\s+hub|адаптер\s+питания|hub\s+charging)\b', re.I)
_KW_FILTER     = re.compile(r'\b(светофильтр|nd[-\s]?\d+|uv[-\s]?filter|cpl\b|фильтр\b)\b', re.I)
_KW_CABLE      = re.compile(r'\b(кабель|провод|шнур|cable|cord)\b', re.I)
_KW_MICROPHONE = re.compile(r'\b(микрофон|microphone|\bmic\b)\b', re.I)
_KW_LIGHT      = re.compile(r'\b(осветитель|видеосвет|led\s+panel|led\s+light|осветительн\w+)\b', re.I)
_KW_MONITOR    = re.compile(r'\b(монитор\b|field\s+monitor|on[-\s]?camera\s+monitor)\b', re.I)
_KW_GIMBAL     = re.compile(r'\b(стабилизатор|gimbal|stabilizer|стедикам)\b', re.I)

_COLOR_MAP_RU = {
    "чёрный": "Чёрный", "чёрная": "Чёрный", "чёрное": "Чёрный",
    "черный": "Чёрный", "черная": "Чёрный", "черное": "Чёрный",
    "белый": "Белый", "белая": "Белый", "белое": "Белый",
    "серебристый": "Серебристый", "серебристая": "Серебристый",
    "серый": "Серый", "серая": "Серый",
    "красный": "Красный", "красная": "Красный",
    "синий": "Синий", "синяя": "Синий",
    "зелёный": "Зелёный", "зелёная": "Зелёный",
    "золотистый": "Золотистый", "золотистая": "Золотистый",
    "оранжевый": "Оранжевый", "оранжевая": "Оранжевый",
    "бронзовый": "Бронзовый", "титановый": "Серый",
}
_COLOR_MAP_EN = {
    "black": "Чёрный", "white": "Белый", "silver": "Серебристый",
    "gray": "Серый", "grey": "Серый", "red": "Красный",
    "blue": "Синий", "green": "Зелёный", "gold": "Золотистый",
    "orange": "Оранжевый", "titanium": "Серый", "bronze": "Бронзовый",
    "dark": "Чёрный",
}

_MATERIAL_NORM = {
    "alumin": "Алюминий", "алюмин": "Алюминий",
    "пластик": "Пластик", "plastic": "Пластик",
    "кож": "Кожа", "leather": "Кожа",
    "нейлон": "Нейлон", "nylon": "Нейлон",
    "сталь": "Сталь", "стал": "Сталь", "steel": "Сталь",
    "карбон": "Карбон", "carbon": "Карбон",
    "углеволокно": "Карбон",
    "силикон": "Силикон", "silicone": "Силикон",
    "резин": "Резина",
    "титан": "Титан",
}


def _num(s: str) -> str:
    return s.replace(",", ".")


def extract_specs(name: str, description: str = "", bh_specs: Optional[dict] = None) -> dict:
    """
    Extract deterministic specs from name + description + B&H data.

    Returns flat dict with known keys:
      voltage, current_a, current_ma, power_w,
      capacity_mah, capacity_wh, frequency,
      temperature_min, temperature_max, temperature_range,
      color, connectors (list), resolution, filter_diameter_mm,
      material, thread_size,
      is_battery, is_charger, is_filter, is_cable, is_microphone, is_light, is_monitor
    """
    text = f"{name} {description}"
    out: dict = {}

    # ── Voltage ────────────────────────────────────────────────
    m = _RE_VOLT_RANGE.search(text)
    if m:
        out["voltage"] = f"{m.group(1)}-{m.group(2)} В"
        out["voltage_min"] = m.group(1)
        out["voltage_max"] = m.group(2)
    else:
        m = _RE_VOLT.search(text)
        if m:
            v = _num(m.group(1))
            out["voltage"] = f"{v} В"

    # ── Current ────────────────────────────────────────────────
    m = _RE_CURRENT_MA.search(text)
    if m:
        ma = float(_num(m.group(1)))
        out["current_ma"] = str(int(ma))
        out["current_a"]  = str(round(ma / 1000, 3))
    else:
        m = _RE_CURRENT_A.search(text)
        if m:
            a = float(_num(m.group(1)))
            out["current_a"]  = str(a)
            out["current_ma"] = str(int(a * 1000))

    # ── Power ──────────────────────────────────────────────────
    m = _RE_POWER.search(text)
    if m:
        out["power_w"] = _num(m.group(1))

    # ── Capacity ───────────────────────────────────────────────
    m = _RE_CAPACITY_MAH.search(text)
    if m:
        out["capacity_mah"] = _num(m.group(1))
    m = _RE_CAPACITY_WH.search(text)
    if m:
        out["capacity_wh"] = _num(m.group(1))

    # ── Frequency ──────────────────────────────────────────────
    m = _RE_FREQ.search(text)
    if m:
        out["frequency"] = f"{_num(m.group(1))} {m.group(2)}"

    # ── Temperature range ──────────────────────────────────────
    m = _RE_TEMP_RANGE.search(text)
    if m:
        out["temperature_min"] = m.group(1)
        out["temperature_max"] = m.group(2)
        out["temperature_range"] = f"{m.group(1)}...+{m.group(2)}"

    # ── Color ──────────────────────────────────────────────────
    m = _RE_COLOR_RU.search(text)
    if m:
        out["color"] = _COLOR_MAP_RU.get(m.group(1).lower(), m.group(1).capitalize())
    elif m := _RE_COLOR_EN.search(text):
        out["color"] = _COLOR_MAP_EN.get(m.group(1).lower(), m.group(1).capitalize())

    # ── Connectors ─────────────────────────────────────────────
    found = list(dict.fromkeys(c for c in _RE_CONNECTOR.findall(text)))
    if found:
        out["connectors"] = found

    # ── Resolution ─────────────────────────────────────────────
    m = _RE_RESOLUTION.search(text)
    if m:
        out["resolution"] = m.group(1)

    # ── Filter/lens diameter ───────────────────────────────────
    if _KW_FILTER.search(text):
        m = _RE_FILTER_D.search(text)
        if m:
            out["filter_diameter_mm"] = m.group(1)

    # ── Thread size ────────────────────────────────────────────
    m = _RE_THREAD.search(text)
    if m:
        out["thread_size"] = m.group(1)

    # ── Material ───────────────────────────────────────────────
    m = _RE_MATERIAL.search(text)
    if m:
        mat_raw = m.group(1).lower()
        for key, norm in _MATERIAL_NORM.items():
            if mat_raw.startswith(key):
                out["material"] = norm
                break
        else:
            out["material"] = m.group(1).capitalize()

    # ── Category flags ─────────────────────────────────────────
    out["is_battery"]   = bool(_KW_BATTERY.search(text))
    out["is_charger"]   = bool(_KW_CHARGER.search(text))
    out["is_filter"]    = bool(_KW_FILTER.search(text))
    out["is_cable"]     = bool(_KW_CABLE.search(text))
    out["is_microphone"]= bool(_KW_MICROPHONE.search(text))
    out["is_light"]     = bool(_KW_LIGHT.search(text))
    out["is_monitor"]   = bool(_KW_MONITOR.search(text))

    # ── Merge B&H specs ────────────────────────────────────────
    if bh_specs:
        _merge_bh(out, bh_specs)

    return out


def _merge_bh(out: dict, bh_specs: dict):
    """Pull relevant values from B&H structured specs into the extracted dict."""
    for k, v in bh_specs.items():
        if not v or not isinstance(v, str):
            continue
        kl = k.lower().strip()

        if "power consumption" in kl or ("power" in kl and "W" in v):
            if "power_w" not in out:
                m = re.search(r"([\d.]+)\s*W", v)
                if m:
                    out["power_w"] = m.group(1)

        elif "voltage" in kl or "input voltage" in kl:
            if "voltage" not in out:
                out["voltage"] = v.split("(")[0].strip()

        elif "current" in kl:
            if "current_a" not in out:
                out["current_bh"] = v

        elif "battery capacity" in kl:
            if "capacity_mah" not in out:
                m = re.search(r"([\d.]+)\s*(?:mAh|mA·h)", v, re.I)
                if m:
                    out["capacity_mah"] = m.group(1)

        elif "frequency" in kl or "operating frequency" in kl:
            if "frequency" not in out:
                out["frequency"] = v.split("(")[0].strip()

        elif "color temperature" in kl:
            out["color_temperature"] = v.split("(")[0].strip()

        elif "beam angle" in kl or "viewing angle" in kl:
            out["beam_angle"] = v.split("(")[0].strip()

        elif "channel" in kl and ("count" in kl or "number" in kl):
            out["channels"] = v.split("(")[0].strip()

        elif "operating temperature" in kl:
            if "temperature_range" not in out:
                out["temperature_range_bh"] = v.split("(")[0].strip()

        elif "connector" in kl or "interface" in kl:
            if "connectors" not in out:
                out["connectors_bh"] = v.split("(")[0].strip()

        elif "material" in kl and "material" not in out:
            out["material_bh"] = v.split("(")[0].strip()

        elif "color" in kl and "color" not in out:
            raw = v.split("(")[0].strip()
            mapped = _COLOR_MAP_EN.get(raw.lower())
            out["color"] = mapped or raw

        elif "weight" in kl and "bh_weight" not in out:
            out["bh_weight"] = v.split("(")[0].strip()

        elif "dimension" in kl and "bh_dimensions" not in out:
            out["bh_dimensions"] = v.split("(")[0].strip()


def get_category_rules(extracted: dict, attr_name_lower: str,
                       allowed_values: Optional[list] = None) -> Optional[str]:
    """
    Return a fixed value for a specific attribute based on product type rules.
    Used as Layer 2 before AI.

    Returns the value string or None if no rule applies.
    Picks the best match from allowed_values if provided.
    """
    def _pick(candidates: list[str], allowed: Optional[list]) -> Optional[str]:
        """Pick first candidate that matches an allowed value (case-insensitive)."""
        if not allowed:
            return candidates[0] if candidates else None
        allowed_lower = {a.lower(): a for a in allowed}
        for cand in candidates:
            for keyword in cand.lower().split():
                for av_low, av_orig in allowed_lower.items():
                    if keyword in av_low or av_low in keyword:
                        return av_orig
        return None

    a = attr_name_lower

    # ── Battery / accumulator rules ────────────────────────────
    if extracted.get("is_battery"):
        if "класс опасности" in a or "опасн" in a:
            return _pick(["Класс 9", "9"], allowed_values)
        if "химический тип" in a or "тип аккумулятора" in a or "тип батареи" in a:
            return _pick(["Li-Ion", "Литий-ионный", "LiIon", "Li-ion"], allowed_values)
        if "упаковка" in a:
            return _pick(["Коробка", "Картонная коробка"], allowed_values)

    # ── Charger rules ──────────────────────────────────────────
    if extracted.get("is_charger"):
        if "упаковка" in a:
            return _pick(["Коробка", "Картонная коробка"], allowed_values)

    # ── Light rules ────────────────────────────────────────────
    if extracted.get("is_light"):
        if "тип источника" in a or "источник света" in a:
            return _pick(["LED", "ЛЕД", "Светодиодный"], allowed_values)

    return None


def format_for_ai(extracted: dict) -> str:
    """Render extracted specs as a compact string to inject into AI prompt context."""
    lines = []
    if v := extracted.get("voltage"):
        lines.append(f"Напряжение: {v}")
    if v := extracted.get("current_a"):
        lines.append(f"Ток: {v} А")
    if v := extracted.get("power_w"):
        lines.append(f"Мощность: {v} Вт")
    if v := extracted.get("capacity_mah"):
        lines.append(f"Ёмкость: {v} мАч")
    if v := extracted.get("capacity_wh"):
        lines.append(f"Ёмкость: {v} Вт·ч")
    if v := extracted.get("frequency"):
        lines.append(f"Частота: {v}")
    if v := extracted.get("temperature_range"):
        lines.append(f"Рабочая температура: {v} °C")
    if v := extracted.get("color"):
        lines.append(f"Цвет: {v}")
    if v := extracted.get("connectors"):
        lines.append(f"Разъёмы: {', '.join(v) if isinstance(v, list) else v}")
    if v := extracted.get("connectors_bh"):
        lines.append(f"Разъёмы (B&H): {v}")
    if v := extracted.get("resolution"):
        lines.append(f"Разрешение: {v}")
    if v := extracted.get("filter_diameter_mm"):
        lines.append(f"Диаметр фильтра: {v} мм")
    if v := extracted.get("thread_size"):
        lines.append(f"Резьба: {v}\"")
    if v := extracted.get("material") or extracted.get("material_bh"):
        lines.append(f"Материал: {v}")
    if v := extracted.get("color_temperature"):
        lines.append(f"Цветовая температура: {v}")
    if v := extracted.get("beam_angle"):
        lines.append(f"Угол пучка: {v}")
    if v := extracted.get("channels"):
        lines.append(f"Каналов: {v}")
    if v := extracted.get("bh_weight"):
        lines.append(f"Вес: {v}")
    if v := extracted.get("bh_dimensions"):
        lines.append(f"Габариты: {v}")
    cats = [k[3:] for k in ("is_battery","is_charger","is_filter","is_cable","is_microphone","is_light","is_monitor")
            if extracted.get(k)]
    if cats:
        lines.append(f"Тип товара: {', '.join(cats)}")
    return "\n".join(lines)
