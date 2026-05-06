import asyncio, os, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest


def _make_1px_png() -> bytes:
    """Return a minimal valid PNG in bytes."""
    import io
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (100, 100), (255, 255, 255)).save(buf, "PNG")
    return buf.getvalue()


def test_prepare_images_returns_three_tuple(tmp_path, monkeypatch):
    """_prepare_images must return (urls, master_disk_paths, warnings)."""
    import main

    monkeypatch.setattr(main, "UPLOADS_DIR", str(tmp_path))
    monkeypatch.setattr(main, "PUBLIC_BASE_URL", "http://localhost:8000")

    png_bytes = _make_1px_png()

    async def fake_download(url, auth_header=None):
        return png_bytes

    monkeypatch.setattr(main, "_download_bytes", fake_download)

    product = {"article": "TESTART", "images": ["https://api.moysklad.ru/download/full-img"]}
    saved = {}

    urls, master_paths, warns = asyncio.run(
        main._prepare_images("fake-token", product, saved, "12345", subfolder="ym_proc")
    )

    assert len(urls) == 1
    assert len(master_paths) == 1
    assert master_paths[0].endswith("img_0.png")
    assert os.path.exists(master_paths[0])
    # subfolder JPEG must also exist
    jpeg_path = master_paths[0].replace("master", "ym_proc").replace(".png", ".jpg")
    assert os.path.exists(jpeg_path)


def test_prepare_images_reuses_existing_master(tmp_path, monkeypatch):
    """If master PNG already on disk, do not re-download."""
    import main

    monkeypatch.setattr(main, "UPLOADS_DIR", str(tmp_path))
    monkeypatch.setattr(main, "PUBLIC_BASE_URL", "http://localhost:8000")

    call_count = 0

    async def counting_download(url, auth_header=None):
        nonlocal call_count
        call_count += 1
        return _make_1px_png()

    monkeypatch.setattr(main, "_download_bytes", counting_download)

    product = {"article": "TESTART", "images": ["https://api.moysklad.ru/download/full-img"]}
    saved = {}

    asyncio.run(main._prepare_images("tok", product, saved, "12345", subfolder="ym_proc"))
    asyncio.run(main._prepare_images("tok", product, saved, "12345", subfolder="ym_proc"))

    assert call_count == 1  # second call reused disk master
