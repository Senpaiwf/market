import asyncio, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest
from unittest.mock import AsyncMock, MagicMock, patch


def _make_client():
    from wb import WildberriesClient
    return WildberriesClient("fake-jwt-token")


def test_upload_photo_returns_url():
    """upload_photo must POST multipart and return the URL from response data."""
    client = _make_client()

    fake_response = MagicMock()
    fake_response.status_code = 200
    fake_response.json.return_value = {"data": {"url": "https://cdn.wildberries.ru/img/test.jpg"}}

    async def run():
        with patch("httpx.AsyncClient") as mock_cls:
            mock_client = AsyncMock()
            mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)
            mock_client.post = AsyncMock(return_value=fake_response)

            result = await client.upload_photo(b"fake-png-bytes")
            assert result == "https://cdn.wildberries.ru/img/test.jpg"

            call_args = mock_client.post.call_args
            assert "/content/v2/media/file" in call_args[0][0]
            assert "files" in call_args[1]

    asyncio.run(run())


def test_upload_photo_raises_on_error():
    """upload_photo must raise RuntimeError when API returns non-200."""
    client = _make_client()

    fake_response = MagicMock()
    fake_response.status_code = 400
    fake_response.json.return_value = {"errorText": "Bad request"}

    async def run():
        with patch("httpx.AsyncClient") as mock_cls:
            mock_client = AsyncMock()
            mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)
            mock_client.post = AsyncMock(return_value=fake_response)

            with pytest.raises(RuntimeError, match="WB фото upload"):
                await client.upload_photo(b"fake-png-bytes")

    asyncio.run(run())
