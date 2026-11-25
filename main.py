import os

from fastapi import FastAPI, Query
from fastapi.responses import StreamingResponse, JSONResponse
import yt_dlp
import requests

app = FastAPI()

# Render এর "Secret Files" ফিচার দিয়ে আপলোড করা হলে এই পাথেই পাওয়া যাবে
COOKIE_FILE = "/etc/secrets/cookies.txt"


def base_opts():
    opts = {
        "quiet": True,
        "noplaylist": True,
        # cookies থাকার পরেও কিছু ফরম্যাট PO-token এর কারণে লুকানো থাকে —
        # এটা সেগুলোকেও candidate হিসেবে বিবেচনা করতে বলে
        "extractor_args": {"youtube": {"formats": ["missing_pot"]}},
    }
    if os.path.exists(COOKIE_FILE):
        opts["cookiefile"] = COOKIE_FILE
    return opts


def extract_with_fallback(url: str, format_candidates):
    """একাধিক format selector ক্রমানুসারে try করে, প্রথমটা যেটা কাজ করে সেটা দিয়ে info ফেরত দেয়।"""
    last_error = None
    for fmt in format_candidates:
        try:
            opts = base_opts()
            opts["format"] = fmt
            with yt_dlp.YoutubeDL(opts) as ydl:
                return ydl.extract_info(url, download=False), None
        except Exception as e:
            last_error = str(e)
            continue
    return None, last_error


@app.get("/")
def root():
    return {"status": "AURA GRAB backend is running", "cookies_loaded": os.path.exists(COOKIE_FILE)}


@app.get("/info")
def info(url: str = Query(...)):
    """ভিডিওর টাইটেল/থাম্বনেইল আনে, প্রিভিউ কার্ডে দেখানোর জন্য।"""
    try:
        opts = base_opts()
        opts["skip_download"] = True
        with yt_dlp.YoutubeDL(opts) as ydl:
            data = ydl.extract_info(url, download=False)
        return {
            "title": data.get("title"),
            "thumbnail": data.get("thumbnail"),
            "duration": data.get("duration"),
        }
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})


@app.get("/download")
def download(url: str = Query(...), format: str = Query("mp4")):
    """
    YouTube/TikTok/Instagram/Facebook — যেকোনো লিঙ্ক থেকে yt-dlp দিয়ে
    আসল স্ট্রিম URL বের করে, সেটা সরাসরি Android অ্যাপের কাছে
    byte-by-byte স্ট্রিম করে পাঠায়।
    """
    if format == "mp4":
        format_candidates = ["best[ext=mp4]/best", "best", "bestvideo/best"]
    else:
        format_candidates = ["bestaudio[ext=m4a]/bestaudio", "bestaudio", "best"]

    info_data, err = extract_with_fallback(url, format_candidates)
    if info_data is None:
        return JSONResponse(status_code=500, content={"error": f"extract failed: {err}"})

    # ফরম্যাট অনুযায়ী আসল ডাউনলোডযোগ্য URL বের করা
    direct_url = info_data.get("url")
    if not direct_url and "requested_downloads" in info_data:
        rd = info_data["requested_downloads"]
        if rd:
            direct_url = rd[0].get("url")
    if not direct_url and "formats" in info_data and info_data["formats"]:
        # শেষ ভরসা হিসেবে যেকোনো formats এন্ট্রি থেকে সরাসরি url খোঁজা
        for f in reversed(info_data["formats"]):
            if f.get("url"):
                direct_url = f["url"]
                break

    if not direct_url:
        available = [
            {"itag": f.get("format_id"), "ext": f.get("ext"), "note": f.get("format_note")}
            for f in info_data.get("formats", [])
        ]
        return JSONResponse(
            status_code=404,
            content={"error": "No downloadable stream found", "available_formats": available},
        )

    headers = info_data.get("http_headers") or {}
    title = info_data.get("title", "download")
    safe_title = "".join(c for c in title if c.isalnum() or c in " ._-").strip()[:60] or "download"
    ext = "mp4" if format == "mp4" else "mp3"
    filename = f"{safe_title}.{ext}"
    media_type = "video/mp4" if format == "mp4" else "audio/mpeg"

    def stream():
        with requests.get(direct_url, headers=headers, stream=True, timeout=120) as r:
            for chunk in r.iter_content(chunk_size=65536):
                if chunk:
                    yield chunk

    return StreamingResponse(
        stream(),
        media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
