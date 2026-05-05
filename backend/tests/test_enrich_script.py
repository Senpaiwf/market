# market/backend/tests/test_enrich_script.py
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))


# ── merge_answers ─────────────────────────────────────────────

def test_merge_answers_fills_missing_keys():
    from enrich_script import merge_answers
    existing = {"brand": "SmallRig"}
    new      = {"brand": "WRONG", "description": "New desc"}
    result   = merge_answers(existing, new)
    assert result["brand"] == "SmallRig"        # not overwritten
    assert result["description"] == "New desc"  # filled


def test_merge_answers_fills_empty_string():
    from enrich_script import merge_answers
    existing = {"brand": ""}
    new      = {"brand": "SmallRig"}
    result   = merge_answers(existing, new)
    assert result["brand"] == "SmallRig"


def test_merge_answers_nested_dict_no_overwrite():
    from enrich_script import merge_answers
    existing = {"params_values": {"ym_123": "existing"}}
    new      = {"params_values": {"ym_123": "NEW", "ym_456": "added"}}
    result   = merge_answers(existing, new)
    assert result["params_values"]["ym_123"] == "existing"  # not overwritten
    assert result["params_values"]["ym_456"] == "added"     # new key filled


def test_merge_answers_nested_dict_created_when_missing():
    from enrich_script import merge_answers
    existing = {}
    new      = {"ozon_attrs": {"oz_85": "SmallRig"}}
    result   = merge_answers(existing, new)
    assert result["ozon_attrs"]["oz_85"] == "SmallRig"


# ── _confidence_badge ─────────────────────────────────────────

def test_confidence_badge_high():
    from enrich_script import _confidence_badge
    assert _confidence_badge(0.90) == "✓"
    assert _confidence_badge(0.75) == "✓"


def test_confidence_badge_medium():
    from enrich_script import _confidence_badge
    assert _confidence_badge(0.60) == "?"
    assert _confidence_badge(0.50) == "?"


def test_confidence_badge_low():
    from enrich_script import _confidence_badge
    assert _confidence_badge(0.30) == "✗"
    assert _confidence_badge(0.0)  == "✗"


# ── _parse_correction ─────────────────────────────────────────

def test_parse_correction_with_value():
    from enrich_script import _parse_correction
    result = _parse_correction("18385 ym 90566")
    assert result == ("18385", "ym", "90566")


def test_parse_correction_without_value():
    from enrich_script import _parse_correction
    result = _parse_correction("18385 oz")
    assert result == ("18385", "oz", "")


def test_parse_correction_skip():
    from enrich_script import _parse_correction
    result = _parse_correction("18385 skip")
    assert result == ("18385", "skip", "")


def test_parse_correction_invalid_mp():
    from enrich_script import _parse_correction
    assert _parse_correction("18385 invalid") is None


def test_parse_correction_too_short():
    from enrich_script import _parse_correction
    assert _parse_correction("18385") is None


# ── _truncate ─────────────────────────────────────────────────

def test_truncate_short_string():
    from enrich_script import _truncate
    assert _truncate("hello", 10) == "hello"


def test_truncate_long_string():
    from enrich_script import _truncate
    result = _truncate("hello world", 8)
    assert len(result) == 8
    assert result.endswith("…")


def test_merge_answers_nested_empty_string_overwritten():
    from enrich_script import merge_answers
    existing = {"params_values": {"ym_123": ""}}
    new      = {"params_values": {"ym_123": "filled"}}
    result   = merge_answers(existing, new)
    assert result["params_values"]["ym_123"] == "filled"  # empty string yields to new value


def test_truncate_exact_length():
    from enrich_script import _truncate
    assert _truncate("hello", 5) == "hello"   # len == n → no truncation


def test_parse_correction_with_whitespace():
    from enrich_script import _parse_correction
    result = _parse_correction("  18385 ym 90566  ")
    assert result == ("18385", "ym", "90566")
