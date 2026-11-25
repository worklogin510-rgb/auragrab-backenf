from fastapi import FastAPI, Query
from fastapi.responses import StreamingResponse, JSONResponse
from urllib.parse import urlparse
import yt_dlp
import requests

app = FastAPI()

# HTTP Session
http = requests.Session()
http.headers.update({
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/130.0.0.0 Safari/537.36"
    )
})


def is_youtube_url(url: str) -> bool:
    try:
        host = (urlparse(url).hostname or "").lower()

        return (
            host == "youtu.be"
            or host == "youtube.com"
            or host.endswith(".youtube.com")
        )
    except Exception:
        return False


# ==============================
# HOME / HEALTH CHECK
# ==============================

@app.get("/")
def root():
    return {
        "status": "AURA GRAB backend is running"
    }


# ==============================
# VIDEO INFO
# ==============================

@app.get("/info")
def info(url: str = Query(...)):

    try:

        # YouTube Metadata using oEmbed
        if is_youtube_url(url):

            response = http.get(
                "https://www.youtube.com/oembed",
                params={
                    "url": url,
                    "format": "json"
                },
                timeout=20
            )

            response.raise_for_status()

            data = response.json()

            return {
                "title": data.get("title"),
                "thumbnail": data.get("thumbnail_url"),
                "author": data.get("author_name"),
                "provider": "YouTube"
            }

        # Other Platforms: Existing yt-dlp method
        ydl_opts = {
            "quiet": True,
            "noplaylist": True,
            "skip_download": True
        }

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            data = ydl.extract_info(
                url,
                download=False
            )

        return {
            "title": data.get("title"),
            "thumbnail": data.get("thumbnail"),
            "duration": data.get("duration")
        }

    except requests.HTTPError as e:

        status = (
            e.response.status_code
            if e.response is not None
            else 502
        )

        return JSONResponse(
            status_code=502,
            content={
                "error": "YouTube metadata request failed",
                "upstream_status": status
            }
        )

    except Exception as e:

        return JSONResponse(
            status_code=500,
            content={
                "error": str(e)
            }
        )


# ==============================
# VIDEO DOWNLOAD
# ==============================

@app.get("/download")
def download(
    url: str = Query(...),
    format: str = Query("mp4")
):

    if format not in ("mp4", "mp3"):
        return JSONResponse(
            status_code=400,
            content={
                "error": "format must be mp4 or mp3"
            }
        )

    ydl_opts = {
        "quiet": True,
        "noplaylist": True,
        "format": (
            "best[ext=mp4]/best"
            if format == "mp4"
            else "bestaudio/best"
        )
    }

    try:

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:

            info_data = ydl.extract_info(
                url,
                download=False
            )

    except Exception as e:

        return JSONResponse(
            status_code=500,
            content={
                "error": f"extract failed: {e}"
            }
        )

    # Find direct media URL
    direct_url = info_data.get("url")

    if (
        not direct_url
        and info_data.get("requested_downloads")
    ):
        rd = info_data["requested_downloads"]

        if rd:
            direct_url = rd[0].get("url")

    if (
        not direct_url
        and info_data.get("formats")
    ):
        formats = info_data["formats"]

        if formats:
            direct_url = formats[-1].get("url")

    if not direct_url:

        return JSONResponse(
            status_code=404,
            content={
                "error": "No downloadable stream found"
            }
        )

    # Download Headers
    headers = info_data.get("http_headers") or {}

    title = info_data.get("title", "download")

    safe_title = "".join(
        c for c in title
        if c.isalnum() or c in " ._-"
    ).strip()[:60] or "download"

    ext = "mp4" if format == "mp4" else "mp3"

    filename = f"{safe_title}.{ext}"

    media_type = (
        "video/mp4"
        if format == "mp4"
        else "audio/mpeg"
    )

    # Stream media
    def stream():

        with requests.get(
            direct_url,
            headers=headers,
            stream=True,
            timeout=120
        ) as response:

            response.raise_for_status()

            for chunk in response.iter_content(
                chunk_size=65536
            ):

                if chunk:
                    yield chunk

    return StreamingResponse(
        stream(),
        media_type=media_type,
        headers={
            "Content-Disposition":
            f'attachment; filename="{filename}"'
        }
            )
