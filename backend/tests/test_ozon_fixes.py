import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from ozon import _to_ozon_values


# ── _to_ozon_values: plain string handling ────────────────────

def test_plain_string_with_comma_is_single_value():
    """A plain string containing a comma must NOT be split — it's one value."""
    result = _to_ozon_values("1 x зарядное устройство, 1 x кабель")
    assert result == [{"dictionary_value_id": 0, "value": "1 x зарядное устройство, 1 x кабель"}]


def test_plain_string_no_comma_is_single_value():
    """A plain string without comma is a single value."""
    result = _to_ozon_values("Класс 9")
    assert result == [{"dictionary_value_id": 0, "value": "Класс 9"}]


def test_plain_string_with_multiple_commas_single_value():
    """Long text with commas (annotation-like) stays as one value."""
    text = "Аккумулятор для камер, совместим с LP-E6, LP-E6N, LP-E6NH"
    result = _to_ozon_values(text)
    assert result == [{"dictionary_value_id": 0, "value": text}]


def test_list_still_produces_multiple_values():
    """A list input should still produce one entry per item (is_collection)."""
    result = _to_ozon_values(["val1", "val2", "val3"])
    assert len(result) == 3
    assert {"dictionary_value_id": 0, "value": "val1"} in result
    assert {"dictionary_value_id": 0, "value": "val2"} in result
    assert {"dictionary_value_id": 0, "value": "val3"} in result


def test_list_with_dicts_preserves_dict_id():
    """List of {value, dict_id} dicts keeps real dictionary_value_id."""
    result = _to_ozon_values([
        {"value": "Li-Ion", "dict_id": 5555},
        {"value": "NiMH", "dict_id": 6666},
    ])
    assert result == [
        {"dictionary_value_id": 5555, "value": "Li-Ion"},
        {"dictionary_value_id": 6666, "value": "NiMH"},
    ]


def test_dict_input_single_entry():
    """A dict input returns a single-item list."""
    result = _to_ozon_values({"value": "Китай", "dict_id": 1234})
    assert result == [{"dictionary_value_id": 1234, "value": "Китай"}]


def test_empty_string_returns_empty():
    """Empty string should return an empty list."""
    result = _to_ozon_values("")
    assert result == []
