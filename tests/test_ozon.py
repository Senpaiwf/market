"""Unit tests for Ozon build_item and _build_attrs_from_saved."""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'backend'))

import pytest
from ozon import OzonClient


@pytest.fixture
def oz():
    return OzonClient("test-client", "test-key")


# ── build_item: type_id sent to Ozon ──────────────────────────

def test_build_item_includes_type_id_when_saved(oz):
    product = {"name": "Test", "article": "ART", "weight_kg": 0.5}
    saved   = {"ozon_category_id": 17027823, "ozon_type_id": 970491011}
    item    = oz.build_item(product, saved)
    assert item["type_id"] == 970491011


def test_build_item_omits_type_id_when_not_saved(oz):
    product = {"name": "Test", "article": "ART", "weight_kg": 0.5}
    saved   = {"ozon_category_id": 17027823}
    item    = oz.build_item(product, saved)
    assert "type_id" not in item


def test_build_item_description_category_id(oz):
    product = {"name": "Test", "article": "ART", "weight_kg": 0}
    saved   = {"ozon_category_id": 12345678, "ozon_type_id": 999}
    item    = oz.build_item(product, saved)
    assert item["description_category_id"] == 12345678


# ── _build_attrs_from_saved: custom ozon_attrs included ───────

def test_custom_ozon_attrs_are_included(oz):
    product = {"name": "Test", "weight_kg": 0.1}
    saved   = {"ozon_attrs": {"oz_200": "Кожа", "oz_300": "Чёрный"}}
    attrs   = oz._build_attrs_from_saved(product, saved)
    attr_ids = {a["id"] for a in attrs}
    assert 200 in attr_ids
    assert 300 in attr_ids


def test_custom_attrs_override_defaults(oz):
    """If user explicitly set brand (id=85), default brand logic must not override."""
    product = {"name": "Test", "brand": "Sony", "weight_kg": 0.1}
    saved   = {"brand": "Sony", "ozon_attrs": {"oz_85": "Canon"}}
    attrs   = oz._build_attrs_from_saved(product, saved)
    brand_attrs = [a for a in attrs if a["id"] == 85]
    assert len(brand_attrs) == 1
    assert brand_attrs[0]["values"][0]["value"] == "Canon"


def test_empty_ozon_attrs_skipped(oz):
    product = {"name": "Test", "weight_kg": 0.1}
    saved   = {"ozon_attrs": {"oz_200": "", "oz_300": None}}
    attrs   = oz._build_attrs_from_saved(product, saved)
    attr_ids = {a["id"] for a in attrs}
    assert 200 not in attr_ids
    assert 300 not in attr_ids


def test_collection_attr_comma_separated(oz):
    """Comma-separated values should become multiple values list."""
    product = {"name": "Test", "weight_kg": 0.1}
    saved   = {"ozon_attrs": {"oz_400": "Красный, Синий, Зелёный"}}
    attrs   = oz._build_attrs_from_saved(product, saved)
    attr_400 = next(a for a in attrs if a["id"] == 400)
    assert len(attr_400["values"]) == 3
    assert attr_400["values"][1]["value"] == "Синий"


def test_list_attr_becomes_multiple_values(oz):
    product = {"name": "Test", "weight_kg": 0.1}
    saved   = {"ozon_attrs": {"oz_500": ["X", "Y"]}}
    attrs   = oz._build_attrs_from_saved(product, saved)
    attr_500 = next(a for a in attrs if a["id"] == 500)
    assert len(attr_500["values"]) == 2


def test_default_description_always_included(oz):
    product = {"name": "Camera", "weight_kg": 0.1}
    saved   = {}
    attrs   = oz._build_attrs_from_saved(product, saved)
    assert any(a["id"] == 4191 for a in attrs)


def test_saved_description_used_over_default(oz):
    product = {"name": "Camera", "weight_kg": 0.1, "description": "Short"}
    saved   = {"description": "Custom long description here"}
    attrs   = oz._build_attrs_from_saved(product, saved)
    desc_attr = next(a for a in attrs if a["id"] == 4191)
    assert "Custom" in desc_attr["values"][0]["value"]


# ── build_item: dimensions and weight ─────────────────────────

def test_build_item_dimensions_in_mm(oz):
    product = {
        "name": "Test", "article": "X", "weight_kg": 1.0,
        "dims_cm": {"depth_mm": 300, "width_mm": 200, "height_mm": 100},
    }
    item = oz.build_item(product, {})
    assert item["depth"] == 300
    assert item["width"] == 200
    assert item["height"] == 100
    assert item["dimension_unit"] == "mm"


def test_build_item_weight_converted_to_grams(oz):
    product = {"name": "Test", "article": "X", "weight_kg": 1.5}
    item    = oz.build_item(product, {})
    assert item["weight"] == 1500
    assert item["weight_unit"] == "g"


def test_build_item_minimum_weight_1g(oz):
    product = {"name": "Test", "article": "X", "weight_kg": 0}
    item    = oz.build_item(product, {})
    assert item["weight"] >= 1
