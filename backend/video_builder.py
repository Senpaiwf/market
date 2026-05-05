# backend/video_builder.py
# Генерация 16:9 mp4-слайдшоу из фото товара. Использует системный ffmpeg.

import os
import asyncio
import shutil
import tempfile
import time
from typing import List, Optional

import httpx

MEDIA_DIR = os.path.join(os.path.dirname(__file__), "media", "videos")
os.makedirs(MEDIA_DIR, exist_ok=True)

VIDEO_W = 1920
VIDEO_H = 1080
DEFAULT_DURATION = 15  # секунд
VIDEO_MAX_AGE_SECONDS = 24 * 3600  # через сутки sweep удалит


def video_path(code: str) -> str:
    safe = "".join(c for c in code if c.isalnum() or c in "-_")
    return os.path.join(MEDIA_DIR, f"{safe}.mp4")


def video_exists(code: str) -> bool:
    return os.path.isfile(video_path(code))


def video_info(code: str) -> Optional[dict]:
    p = video_path(code)
    if not os.path.isfile(p):
        return None
    st = os.stat(p)
    return {
        "path": p,
        "size_bytes": st.st_size,
        "size_mb": round(st.st_size / (1024 * 1024), 2),
        "mtime": st.st_mtime,
    }


def delete_video(code: str) -> bool:
    p = video_path(code)
    if os.path.isfile(p):
        try:
            os.remove(p)
            return True
        except Exception:
            return False
    return False


def cleanup_old_videos(max_age_seconds: int = VIDEO_MAX_AGE_SECONDS) -> int:
    """Удаляет mp4 старше max_age_seconds. Возвращает число удалённых."""
    now = time.time()
    removed = 0
    try:
        for name in os.listdir(MEDIA_DIR):
            if not name.endswith(".mp4"):
                continue
            fp = os.path.join(MEDIA_DIR, name)
            try:
                if now - os.stat(fp).st_mtime > max_age_seconds:
                    os.remove(fp)
                    removed += 1
            except Exception:
                pass
    except Exception:
        pass
    return removed


async def _download_image(url: str, ms_token: str, out_path: str) -> bool:
    """Скачивает фото. Если URL — МойСклад, отправляем Bearer-авторизацию.
    Если URL относительный (/media/uploads/...) — читаем файл с диска напрямую."""
    # Локальные uploads — читаем с диска (URL может быть относительный или абсолютный через tunnel)
    marker = "/media/uploads/"
    if marker in url:
        rel = url[url.index(marker):].lstrip("/")
        src = os.path.join(os.path.dirname(__file__), rel)
        if os.path.isfile(src):
            try:
                with open(src, "rb") as fsrc, open(out_path, "wb") as fdst:
                    fdst.write(fsrc.read())
                return os.path.getsize(out_path) > 100
            except Exception:
                return False
        return False

    headers = {"Accept-Encoding": "gzip"}
    if "moysklad" in url:
        headers["Authorization"] = f"Bearer {ms_token}"
    try:
        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as c:
            r = await c.get(url, headers=headers)
            if r.status_code != 200:
                return False
            with open(out_path, "wb") as f:
                f.write(r.content)
            return os.path.getsize(out_path) > 100
    except Exception:
        return False


async def _run_ffmpeg(args: List[str]) -> tuple[bool, str]:
    """Запускает ffmpeg, возвращает (ok, stderr)."""
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, err = await proc.communicate()
    return proc.returncode == 0, (err or b"").decode("utf-8", errors="replace")


def _build_ffmpeg_args(
    image_paths: List[str],
    out_path: str,
    duration: int,
) -> List[str]:
    """Собирает ffmpeg-команду: ресайз каждого фото в 1920x1080 (padding чёрным),
    склейка через concat, длительность каждого кадра = duration / N."""
    n = len(image_paths)
    per_frame = duration / n

    args = ["-y"]
    for p in image_paths:
        # Петляющий вход для каждой картинки на нужную длительность
        args += ["-loop", "1", "-t", f"{per_frame:.3f}", "-i", p]

    # filter_complex: для каждого входа — scale + pad до 16:9, fps=30
    # Затем concat всех обработанных в один видеопоток.
    filters = []
    for i in range(n):
        filters.append(
            f"[{i}:v]scale={VIDEO_W}:{VIDEO_H}:force_original_aspect_ratio=decrease,"
            f"pad={VIDEO_W}:{VIDEO_H}:(ow-iw)/2:(oh-ih)/2:color=black,"
            f"setsar=1,fps=30[v{i}]"
        )
    concat_inputs = "".join(f"[v{i}]" for i in range(n))
    filters.append(f"{concat_inputs}concat=n={n}:v=1:a=0,format=yuv420p[vout]")
    filter_complex = ";".join(filters)

    args += [
        "-filter_complex", filter_complex,
        "-map", "[vout]",
        "-c:v", "libx264",
        "-preset", "fast",
        "-crf", "23",
        "-movflags", "+faststart",
        out_path,
    ]
    return args


async def build_slideshow(
    code: str,
    image_urls: List[str],
    ms_token: str,
    duration: int = DEFAULT_DURATION,
) -> dict:
    """
    Скачивает фото товара, собирает из них 16:9 mp4-слайдшоу на duration секунд.

    Возвращает:
      {"ok": True, "path": "...", "size_mb": 4.2, "duration": 15, "frames": 5}
      или {"ok": False, "error": "..."}
    """
    if not image_urls:
        return {"ok": False, "error": "Нет фото для генерации видео"}

    image_urls = image_urls[:10]  # защита от слишком длинных видео
    out_path = video_path(code)
    tmp_dir = tempfile.mkdtemp(prefix="slideshow_")
    try:
        # 1. Скачиваем все фото параллельно
        tasks = []
        local_paths = []
        for i, url in enumerate(image_urls):
            fp = os.path.join(tmp_dir, f"frame_{i:03d}.jpg")
            local_paths.append(fp)
            tasks.append(_download_image(url, ms_token, fp))
        results = await asyncio.gather(*tasks)
        downloaded = [p for p, ok in zip(local_paths, results) if ok]
        if not downloaded:
            return {"ok": False, "error": "Не удалось скачать ни одно фото из МойСклад"}

        # 2. Генерируем через ffmpeg
        args = _build_ffmpeg_args(downloaded, out_path, duration)
        ok, stderr = await _run_ffmpeg(args)
        if not ok:
            return {"ok": False, "error": f"ffmpeg: {stderr[-400:]}"}
        if not os.path.isfile(out_path):
            return {"ok": False, "error": "ffmpeg отработал, но файл не создан"}

        size = os.path.getsize(out_path)
        return {
            "ok": True,
            "path": out_path,
            "size_bytes": size,
            "size_mb": round(size / (1024 * 1024), 2),
            "duration": duration,
            "frames": len(downloaded),
        }
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
