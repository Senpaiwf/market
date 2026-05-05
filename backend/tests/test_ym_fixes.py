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
