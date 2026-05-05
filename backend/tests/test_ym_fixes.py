import asyncio
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from moysklad import MoySkladClient


def _make_client():
    return MoySkladClient("fake-token")


# ── Task 1: _get_images URL priority ──────────────────────────

def test_get_images_prefers_meta_download_href():
    """Full-size downloadHref must take priority over miniature href."""
    client = _make_client()
    fake_rows = {
        "rows": [
            {
                "meta": {
                    "href": "https://api.moysklad.ru/entity/product/123/images/img1",
                    "downloadHref": "https://api.moysklad.ru/download/full-size-id",
                },
                "miniature": {
                    "href": "https://api.moysklad.ru/download/miniature-id",
                },
            }
        ]
    }
    async def _mock_get(path, params=None):
        return fake_rows

    client._get = _mock_get
    urls = asyncio.run(client._get_images("fake-product-id"))
    assert urls == ["https://api.moysklad.ru/download/full-size-id"]


def test_get_images_falls_back_to_miniature_when_no_download_href():
    """Falls back to miniature.href when meta.downloadHref is absent."""
    client = _make_client()
    fake_rows = {
        "rows": [
            {
                "meta": {
                    "href": "https://api.moysklad.ru/entity/product/123/images/img1",
                    # no downloadHref here
                },
                "miniature": {
                    "href": "https://api.moysklad.ru/download/miniature-id",
                },
            }
        ]
    }
    async def _mock_get(path, params=None):
        return fake_rows

    client._get = _mock_get
    urls = asyncio.run(client._get_images("fake-product-id"))
    assert urls == ["https://api.moysklad.ru/download/miniature-id"]


# ── Task 2: _build_params dispatch ────────────────────────────

from yandex_market import YandexMarketClient


def _make_ym():
    return YandexMarketClient("fake-key", "fake-campaign", "fake-business")


def test_build_params_numeric_value_uses_value_id():
    """When stored value is a numeric string (enum valueId), send valueId: int."""
    ym = _make_ym()
    resolved = {"params_values": {"ym_12345": "67890"}}
    params = ym._build_params({}, "458", resolved)
    assert params == [{"parameterId": 12345, "valueId": 67890}]


def test_build_params_text_value_uses_value_string():
    """When stored value is text (old save or free-text field), send value: str."""
    ym = _make_ym()
    resolved = {"params_values": {"ym_12345": "Li-Ion"}}
    params = ym._build_params({}, "458", resolved)
    assert params == [{"parameterId": 12345, "value": "Li-Ion"}]


def test_build_params_skips_empty_values():
    """Empty string values must be skipped."""
    ym = _make_ym()
    resolved = {"params_values": {"ym_12345": "", "ym_99999": "67890"}}
    params = ym._build_params({}, "458", resolved)
    assert len(params) == 1
    assert params[0]["parameterId"] == 99999


def test_build_params_multivalue_list():
    """List values produce one entry per item, all numeric → valueId."""
    ym = _make_ym()
    resolved = {"params_values": {"ym_12345": ["11111", "22222"]}}
    params = ym._build_params({}, "458", resolved)
    assert len(params) == 2
    assert {"parameterId": 12345, "valueId": 11111} in params
    assert {"parameterId": 12345, "valueId": 22222} in params


def test_build_params_mixed_list_text_and_id():
    """Mixed list: numeric items → valueId, text items → value."""
    ym = _make_ym()
    resolved = {"params_values": {"ym_12345": ["67890", "some-text"]}}
    params = ym._build_params({}, "458", resolved)
    assert {"parameterId": 12345, "valueId": 67890} in params
    assert {"parameterId": 12345, "value": "some-text"} in params
