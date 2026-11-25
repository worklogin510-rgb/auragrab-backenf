import os
import shutil
from urllib.parse import urlparse

from fastapi import FastAPI, Query
from fastapi.responses import StreamingResponse, JSONResponse
import yt_dlp
import requests

app = FastAPI()

# Render Secret Files are read-only. Copy the file to /tmp before yt-dlp uses it.
SECRET_COOKIE_FILE = "/etc/secrets/cookies.txt"
TEMP_COOKIE_FILE = "/tmp/auragrab-cookies.txt"

COOKIES_LOADED = False

try:
    if os.path.isfile(SECRET_COOKIE_FILE):
        shutil.copyfile(SECRET_COOKIE_FILE, TEMP_COOKIE_FILE)
        os.chmod(TEMP_COOKIE_FILE, 0o600)
        COOKIES_LOADED = True
except OSError:
    # Keep the API available; requests can still run without a cookie file.
    COOKIES_LOADED = False


def is_youtube_url(url: str) -> bool:
    try:
        host = (urlparse(url).hostname or "").lower()
        return host == "youtu.be" or host == "youtube.com" or host.endswith(".youtube.com")
    except Exception:
        return False


def make_ydl_options(url: str, *, skip_download: bool = False, format_selector: str | None = None):
    options = {
        "quiet": True,
        "noplaylist": True,
    }

    if skip_download:
        options["skip_download"] = True

    if format_selector:
        options["format"] = format_selector

    # Use the temporary writable copy only for YouTube URLs.
    if COOKIES_LOADED and is_youtube_url(url):
        options["cookiefile"] = TEMP_COOKIE_FILE

    return options


@app.get("/")
def root():
    return {
        "status": "AURA GRAB backend is running",
        "cookies_loaded": COOKIES_LOADED,
    }


@app.get("/info")
def info(url: str = Query(...)):
    """Return video title, thumbnail, and duration for the preview card."""
    ydl_opts = make_ydl_options(url, skip_download=True)

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            data = ydl.extract_info(url, download=False)

        return {
            "title": data.get("title"),
            "thumbnail": data.get("thumbnail"),
            "duration": data.get("duration"),
        }
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})


@app.get("/download")
def download(
    url: str = Query(...),
    format: str = Query("mp4", pattern="^(mp4|mp3)$"),
):
    """
    Resolve a media URL with yt-dlp and stream the selected media response.
    Only download content that you own or are authorized to download.
    """
    format_selector = "best[ext=mp4]/best" if format == "mp4" else "bestaudio/best"
    ydl_opts = make_ydl_options(url, format_selector=format_selector)

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info_data = ydl.extract_info(url, download=False)
    except Exception as e:
        return JSONResponse(
            status_code=500,
            content={"error": f"extract failed: {e}"},
        )

    direct_url = info_data.get("url")

    if not direct_url and info_data.get("requested_downloads"):
        direct_url = info_data["requested_downloads"][0].get("url")

    if not direct_url and info_data.get("formats"):
        direct_url = info_data["formats"][-1].get("url")

    if not direct_url:
        return JSONResponse(
            status_code=404,
            content={"error": "No downloadable stream found"},
        )

    headers = info_data.get("http_headers") or {}
    title = info_data.get("title") or "download"
    safe_title = "".join(
        c for c in title if c.isalnum() or c in " ._-"
    ).strip()[:60] or "download"

    ext = "mp4" if format == "mp4" else "mp3"
    filename = f"{safe_title}.{ext}"
    media_type = "video/mp4" if format == "mp4" else "audio/mpeg"

    def stream():
        with requests.get(
            direct_url,
            headers=headers,
            stream=True,
            timeout=(15, 120),
        ) as response:
            response.raise_for_status()
            for chunk in response.iter_content(chunk_size=65536):
                if chunk:
                    yield chunk

    return StreamingResponse(
        stream(),
        media_type=media_type,
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"'
        },
    )
