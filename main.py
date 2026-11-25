import os
import shutil
import tempfile
from typing import Generator

from fastapi import FastAPI, Query
from fastapi.responses import JSONResponse, StreamingResponse
import yt_dlp

# ---- ffmpeg (mp3 + merge এর জন্য) ----
try:
    import imageio_ffmpeg
    FFMPEG_PATH = imageio_ffmpeg.get_ffmpeg_exe()
except Exception:
    FFMPEG_PATH = None

# ---- cookies ----
COOKIE_SRC = "/etc/secrets/cookies.txt"
COOKIE_RW = "/tmp/cookies.txt"

app = FastAPI(title="AuraGrab Backend")


def _prepare_cookies() -> bool:
    if not os.path.exists(COOKIE_SRC):
        return False
    try:
        shutil.copy2(COOKIE_SRC, COOKIE_RW)
        os.chmod(COOKIE_RW, 0o600)
        return True
    except Exception:
        return False


@app.on_event("startup")
def _startup():
    _prepare_cookies()
    print(f"[startup] cookies={os.path.exists(COOKIE_RW)} "
          f"ffmpeg={bool(FFMPEG_PATH)} deno={shutil.which('deno')}")


def _cookiefile():
    if os.path.exists(COOKIE_RW):
        return COOKIE_RW
    if os.path.exists(COOKIE_SRC):
        return COOKIE_SRC
    return None


def _base_opts():
    opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "no_check_formats": True,
        "retries": 5,
        "fragment_retries": 5,
        "socket_timeout": 30,
        "extractor_args": {
            "youtube": {
                "player_client": ["web", "android", "ios"],
            }
        },
    }
    cf = _cookiefile()
    if cf:
        opts["cookiefile"] = cf
    if FFMPEG_PATH:
        opts["ffmpeg_location"] = FFMPEG_PATH
    deno = shutil.which("deno")
    if deno:
        opts["js_runtimes"] = {"deno": {"path": deno}}
    return opts


# ---------- Routes ----------
@app.get("/")
def root():
    return {
        "status": "ok",
        "deno": shutil.which("deno"),
        "ffmpeg": bool(FFMPEG_PATH),
        "cookies": os.path.exists(COOKIE_RW),
    }


@app.get("/info")
def info(url: str = Query(...)):
    try:
        opts = _base_opts()
        with yt_dlp.YoutubeDL(opts) as ydl:
            data = ydl.extract_info(url, download=False, process=False)
        fmts = list(data.get("formats") or [])
        return {
            "title": data.get("title"),
            "thumbnail": data.get("thumbnail"),
            "author": data.get("uploader") or data.get("channel"),
            "duration": data.get("duration"),
            "provider": data.get("extractor_key"),
            "format_count": len(fmts),
            "has_mp4": any(f.get("ext") == "mp4" for f in fmts),
        }
    except Exception as e:
        return JSONResponse(content={"error": str(e)})


@app.get("/formats")
def formats(url: str = Query(...)):
    """কোন কোন video quality available দেখায়"""
    try:
        opts = _base_opts()
        with yt_dlp.YoutubeDL(opts) as ydl:
            data = ydl.extract_info(url, download=False)
        seen = set()
        out = []
        for f in (data.get("formats") or []):
            if f.get("vcodec") in (None, "none"):
                continue
            h = f.get("height")
            if not h or h in seen:
                continue
            seen.add(h)
            out.append({
                "format_id": f.get("format_id"),
                "height": h,
                "label": f"{h}p",
                "ext": f.get("ext"),
                "filesize": f.get("filesize") or f.get("filesize_approx") or 0,
            })
        out.sort(key=lambda x: x["height"], reverse=True)
        return {"formats": out}
    except Exception as e:
        return JSONResponse(content={"error": str(e)})


@app.get("/download")
def download(url: str = Query(...), format: str = Query("mp4")):
    fmt = (format or "mp4").lower()
    if fmt not in ("mp4", "mp3"):
        return JSONResponse(content={"error": "format must be mp4 or mp3"})

    tmpdir = tempfile.mkdtemp(prefix="auragrab_")
    try:
        opts = _base_opts()
        if fmt == "mp3":
            if not FFMPEG_PATH:
                return JSONResponse(content={"error": "ffmpeg unavailable"})
            opts.update({
                "format": "ba/b",
                "postprocessors": [{
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": "mp3",
                    "preferredquality": "192",
                }],
            })
        else:
            opts.update({
                "format": "bv*+ba/b",
                "merge_output_format": "mp4",
            })
        opts["outtmpl"] = os.path.join(tmpdir, "%(id)s.%(ext)s")

        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
            base = ydl.prepare_filename(info)

        out = base if os.path.exists(base) else None
        if not out:
            for f in os.listdir(tmpdir):
                if f.endswith(".part") or f.endswith(".ytdl"):
                    continue
                out = os.path.join(tmpdir, f)
                break
        if not out or not os.path.exists(out):
            raise FileNotFoundError("downloaded file not found")

        size = os.path.getsize(out)
        mime = "audio/mpeg" if fmt == "mp3" else "video/mp4"
        vid = info.get("id") or "video"
        fname = f"auragrab_{vid}.{fmt}"

        def iterf() -> Generator[bytes, None, None]:
            try:
                with open(out, "rb") as fh:
                    while True:
                        chunk = fh.read(64 * 1024)
                        if not chunk:
                            break
                        yield chunk
            finally:
                shutil.rmtree(tmpdir, ignore_errors=True)

        return StreamingResponse(iterf(), media_type=mime, headers={
            "Content-Disposition": f'attachment; filename="{fname}"',
            "Content-Length": str(size),
            "Cache-Control": "no-store",
        })
    except Exception as e:
        shutil.rmtree(tmpdir, ignore_errors=True)
        return JSONResponse(content={"error": f"download failed: {e}"})
