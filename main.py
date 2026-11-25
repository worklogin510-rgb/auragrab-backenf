from fastapi import FastAPI, Query
from fastapi.responses import StreamingResponse, JSONResponse
import yt_dlp
import requests

app = FastAPI()


@app.get("/")
def root():
    # Render এর health-check আর তুমি নিজে টেস্ট করার জন্য
    return {"status": "AURA GRAB backend is running"}


@app.get("/info")
def info(url: str = Query(...)):
    """ভিডিওর টাইটেল/থাম্বনেইল আনে, প্রিভিউ কার্ডে দেখানোর জন্য।"""
    ydl_opts = {"quiet": True, "noplaylist": True, "skip_download": True}
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
def download(url: str = Query(...), format: str = Query("mp4")):
    """
    YouTube/TikTok/Instagram/Facebook — যেকোনো লিঙ্ক থেকে yt-dlp দিয়ে
    আসল স্ট্রিম URL বের করে, সেটা সরাসরি Android অ্যাপের কাছে
    byte-by-byte স্ট্রিম করে পাঠায়। Android অ্যাপ এই endpoint-কেই
    একটা সাধারণ ফাইল-ডাউনলোড লিঙ্কের মতো ব্যবহার করে।
    """
    ydl_opts = {
        "quiet": True,
        "noplaylist": True,
        "format": "best[ext=mp4]/best" if format == "mp4" else "bestaudio/best",
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info_data = ydl.extract_info(url, download=False)
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": f"extract failed: {e}"})

    # ফরম্যাট অনুযায়ী আসল ডাউনলোডযোগ্য URL বের করা
    direct_url = info_data.get("url")
    if not direct_url and "requested_downloads" in info_data:
        rd = info_data["requested_downloads"]
        if rd:
            direct_url = rd[0].get("url")
    if not direct_url and "formats" in info_data and info_data["formats"]:
        direct_url = info_data["formats"][-1].get("url")

    if not direct_url:
        return JSONResponse(status_code=404, content={"error": "No downloadable stream found"})

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
