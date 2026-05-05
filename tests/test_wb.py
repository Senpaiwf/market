"""Unit tests for WB card building logic."""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'backend'))

import math
import pytest
from wb import WildberriesClient, extract_wb_price


@pytest.fixture
def wb():
    return WildberriesClient("test-token")


# ── vendorCode uses product code, not article ──────────────────

def test_build_card_uses_code_as_vendor_code(wb):
    product = {"code": "18406", "article": "ART-001", "name": "Test", "images": []}
    saved   = {"wb_subject_id": 123}
    card    = wb.build_card(product, saved)
    assert card["variants"][0]["vendorCode"] == "18406"


def test_build_card_falls_back_to_article_when_no_code(wb):
    product = {"article": "ART-001", "name": "Test", "images": []}
    saved   = {"wb_subject_id": 123}
    card    = wb.build_card(product, saved)
    assert card["variants"][0]["vendorCode"] == "ART-001"


# ── brand must NOT be in the card ─────────────────────────────

def test_build_card_has_no_brand_field(wb):
    product = {"code": "18406", "name": "Test", "brand": "Sony", "images": []}
    saved   = {"wb_subject_id": 123, "brand": "Sony"}
    card    = wb.build_card(product, saved)
    variant = card["variants"][0]
    assert "brand" not in variant


# ── photos: user_images take priority over MS images ──────────

def test_build_card_uses_user_images_when_present(wb):
    product = {"code": "18406", "name": "Test",
               "images": ["https://ms.ru/img1.jpg", "https://ms.ru/img2.jpg"]}
    saved   = {"wb_subject_id": 123,
               "user_images": ["https://cdn.example.com/u1.jpg"]}
    card    = wb.build_card(product, saved)
    photos  = card["variants"][0]["photos"]
    assert photos[0] == "https://cdn.example.com/u1.jpg"
    assert "https://ms.ru/img1.jpg" in photos


def test_build_card_falls_back_to_ms_images_when_no_user_images(wb):
    product = {"code": "18406", "name": "Test",
               "images": ["https://ms.ru/img1.jpg"]}
    saved   = {"wb_subject_id": 123}
    card    = wb.build_card(product, saved)
    assert card["variants"][0]["photos"] == ["https://ms.ru/img1.jpg"]


def test_build_card_caps_photos_at_10(wb):
    product = {"code": "18406", "name": "Test",
               "images": [f"https://ms.ru/img{i}.jpg" for i in range(20)]}
    saved   = {"wb_subject_id": 123}
    card    = wb.build_card(product, saved)
    assert len(card["variants"][0]["photos"]) == 10


def test_build_card_user_images_merged_and_capped_at_10(wb):
    product = {"code": "18406", "name": "Test",
               "images": [f"https://ms.ru/ms{i}.jpg" for i in range(8)]}
    saved   = {"wb_subject_id": 123,
               "user_images": [f"https://cdn.example.com/u{i}.jpg" for i in range(5)]}
    card    = wb.build_card(product, saved)
    photos  = card["variants"][0]["photos"]
    assert len(photos) == 10
    assert photos[0].startswith("https://cdn.example.com/")


# ── subjectID ─────────────────────────────────────────────────

def test_build_card_subject_id_zero_when_missing(wb):
    card = wb.build_card({"code": "X", "name": "Test"}, {})
    assert card["subjectID"] == 0


def test_build_card_subject_id_cast_to_int(wb):
    card = wb.build_card({"code": "X", "name": "Test"}, {"wb_subject_id": "456"})
    assert card["subjectID"] == 456


# ── extract_wb_price ──────────────────────────────────────────

def test_extract_wb_price_case_insensitive_lowercase_d():
    """MoySklad returns price name with lowercase 'д' — must still match."""
    product = {"prices": {"для WB (FotoToad)": 1500.5}}
    assert extract_wb_price(product) == 1501  # ceil


def test_extract_wb_price_uppercase_d():
    product = {"prices": {"Для WB (FotoToad)": 2000.0}}
    assert extract_wb_price(product) == 2000


def test_extract_wb_price_falls_back_to_main():
    product = {"price_main": 999.9}
    assert extract_wb_price(product) == 1000  # ceil


def test_extract_wb_price_zero_when_no_prices():
    assert extract_wb_price({}) == 0


def test_extract_wb_price_ceil_fractional():
    product = {"prices": {"Для WB (FotoToad)": 1234.01}}
    assert extract_wb_price(product) == 1235


# ── WB characteristics ────────────────────────────────────────

def test_build_card_characteristics_from_saved(wb):
    product = {"code": "X", "name": "Test"}
    saved   = {"wb_subject_id": 1, "wb_chars": {"wb_123": "Кожа", "wb_456": ["Красный", "Синий"]}}
    card    = wb.build_card(product, saved)
    chars   = card["variants"][0]["characteristics"]
    assert len(chars) == 2
    ids = {c["id"] for c in chars}
    assert 123 in ids
    assert 456 in ids


def test_build_card_skips_empty_characteristics(wb):
    product = {"code": "X", "name": "Test"}
    saved   = {"wb_subject_id": 1, "wb_chars": {"wb_999": "", "wb_100": "Хлопок"}}
    card    = wb.build_card(product, saved)
    chars   = card["variants"][0]["characteristics"]
    assert len(chars) == 1
    assert chars[0]["id"] == 100
