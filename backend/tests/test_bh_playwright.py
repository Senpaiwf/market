import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from bh_playwright import _build_query, _normalize_units


def test_build_query_brand_and_article():
    assert _build_query("SmallRig", "TA-T18-A", "SmallRig Cage") == "SmallRig TA-T18-A"


def test_build_query_skips_numeric_article():
    result = _build_query("SmallRig", "18385", "SmallRig Cage X100VI")
    assert result.startswith("SmallRig")
    assert "18385" not in result


def test_build_query_fallback_name_tokens():
    result = _build_query("", "", "SmallRig Cage X100VI")
    assert "SmallRig" in result
    assert "X100VI" in result


def test_normalize_units_pounds():
    result = _normalize_units({"Weight": "1.40 lb"})
    assert "0.64 кг" in result["Weight"]


def test_normalize_units_inches():
    result = _normalize_units({"Length": "5.5 in"})
    assert "14.0 см" in result["Length"]


def test_normalize_units_no_change():
    result = _normalize_units({"Color": "Black"})
    assert result["Color"] == "Black"
