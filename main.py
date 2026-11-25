import os
import shutil
import tempfile
from pathlib import Path
from typing import Generator

from fastapi import FastAPI, Query
from fastapi.responses import JSONResponse, StreamingResponse
import yt_dlp

# ---- ffmpeg (mp3 convert এর জন্য) ----
try:
    import imageio_ffmpeg
    FFMPEG_PATH = imageio_ffmpeg.get_ffmpeg_exe()
except Exception as _e:
    FFMPEG_PATH = None
    print(f"[startup] ffmpeg unavailable: {_e}")

# ---- cookies paths ----
COOKIE_SRC = "/etc/secrets/cookies.txt"   # Render Secret File (READ-ONLY)
COOKIE_RW  = "/tmp/cookies.txt"           # writable copy (yt-dlp rotation এর জন্য)

app = FastAPI(title="AuraGrab Backend")


# ============================================================
# Startup: cookies কে /tmp তে copy করি যাতে yt-dlp লিখতে পারে
# ============================================================
def _prepare_cookies() -> bool:
    if not os.path.exists(COOKIE_SRC):
        return False
    try:
        shutil.copy2(COOKIE_SRC, COOKIE_RW)
        os.chmod(COOKIE_RW, 0o600)
        return True
    except Exception as e:
        print(f"[startup] cookie copy failed: {e}")
        return False


@app.on_event("startup")
def _on_startup():
    ok = _prepare_cookies()
    print(f"[startup] cookie_src_exists={os.path.exists(COOKIE_SRC)} rw_copy={ok}")
    print(f"[startup] ffmpeg={FFMPEG_PATH}")
    print(f"[startup] yt-dlp version={yt_dlp.version.__version__}")


def _cookiefile() -> str | None:
    if os.path.exists(COOKIE_RW):
        return COOKIE_RW
    if os.path.exists(COOKIE_SRC):
        return COOKIE_SRC
    return None


def _base_opts() -> dict:
    opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "retries": 5,
        "fragment_retries": 5,
        "socket_timeout": 30,
        "extractor_args": {
            "youtube": {
                "player_client": ["mweb", "web_safari", "android_vr"],
            }
        },
    }
    cf = _cookiefile()
    if cf:
        opts["cookiefile"] = cf
    if FFMPEG_PATH:
        opts["ffmpeg_location"] = FFMPEG_PATH
    return opts


# ============================================================
# Routes
# ============================================================
@app.get("/")
def root():
    return {"status": "ok", "service": "auragrab-backend"}


@app.get("/debug/cookies")
def debug_cookies():
    res = {
        "src_path": COOKIE_SRC,
        "src_exists": os.path.exists(COOKIE_SRC),
        "rw_path": COOKIE_RW,
        "rw_exists": os.path.exists(COOKIE_RW),
        "ffmpeg": FFMPEG_PATH is not None,
        "yt_dlp_version": yt_dlp.version.__version__,
    }
    if os.path.exists(COOKIE_SRC):
        data = Path(COOKIE_SRC).read_text(errors="replace")
        names = []
        for line in data.splitlines():
            if not line:
                continue
            if line.startswith("#HttpOnly_"):
                line = line[len("#HttpOnly_"):]
            elif line.startswith("#"):
                continue
            parts = line.split("\t")
            if len(parts) >= 7:
                names.append(parts[5])
        res.update({
            "size_bytes": len(data),
            "cookie_count": len(names),
            "has_SAPISID": "SAPISID" in names,
            "has_secure_3PAPISID": "__Secure-3PAPISID" in names,
            "has_LOGIN_INFO": "LOGIN_INFO" in names,
            "has_secure_3PSID": "__Secure-3PSID" in names,
            "src_writable": os.access(COOKIE_SRC, os.W_OK),
            "rw_writable": os.access(COOKIE_RW, os.W_OK) if os.path.exists(COOKIE_RW) else False,
        })
    return res


@app.get("/formats")
def formats(url: str = Query(...)):
    """Diagnostic: কোন কোন format available তা দেখায়"""
    try:
        opts = _base_opts()
        opts.pop("format", None)
        with yt_dlp.YoutubeDL(opts) as ydl:
            data = ydl.extract_info(url, download=False)
        fmts = []
        for f in (data.get("formats") or []):
            fmts.append({
                "format_id": f.get("format_id"),
                "ext": f.get("ext"),
                "vcodec": f.get("vcodec"),
                "acodec": f.get("acodec"),
                "height": f.get("height"),
                "filesize": f.get("filesize"),
            })
        return {"title": data.get("title"), "count": len(fmts), "formats": fmts}
    except Exception as e:
        return JSONResponse(status_code=200, content={"error": str(e)})


@app.get("/info")
def info(url: str = Query(..., description="video url")):
    try:
        opts = _base_opts()
        with yt_dlp.YoutubeDL(opts) as ydl:
            data = ydl.extract_info(url, download=False)
        return {
            "title": data.get("title"),
            "thumbnail": data.get("thumbnail"),
            "author": data.get("uploader") or data.get("channel"),
            "duration": data.get("duration"),
            "provider": data.get("extractor_key") or data.get("extractor"),
        }
    except Exception as e:
        return JSONResponse(status_code=200, content={"error": f"extract failed: {e}"})


def _stream_file(path: str, chunk: int = 64 * 1024) -> Generator[bytes, None, None]:
    with open(path, "rb") as f:
        while True:
            data = f.read(chunk)
            if not data:
                break
            yield data


@app.get("/download")
def download(
    url: str = Query(..., description="video url"),
    format: str = Query("mp4", description="mp4 or mp3"),
):
    fmt = (format or "mp4").lower().strip()
    if fmt not in ("mp4", "mp3"):
        return JSONResponse(status_code=400, content={"error": "format must be mp4 or mp3"})

    tmpdir = tempfile.mkdtemp(prefix="auragrab_")
    try:
        opts = _base_opts()

        if fmt == "mp3":
            if not FFMPEG_PATH:
                return JSONResponse(
                    status_code=200,
                    content={"error": "ffmpeg missing on server (mp3 unavailable)"},
                )
            opts.update({
                "format": "bestaudio*/best",
                "postprocessors": [{
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": "mp3",
                    "preferredquality": "192",
                }],
            })
        else:
            # সবচেয়ে permissive selector — merge করে mp4 বানাবে
            opts.update({
                "format": "bestvideo*+bestaudio/best",
                "merge_output_format": "mp4",
            })

        opts["outtmpl"] = os.path.join(tmpdir, "%(id)s.%(ext)s")

        with yt_dlp.YoutubeDL(opts) as ydl:
            data = ydl.extract_info(url, download=True)
            base = ydl.prepare_filename(data)

        # final file খুঁজি
        out_path = None
        if os.path.exists(base):
            out_path = base
        else:
            for f in os.listdir(tmpdir):
                if f.endswith(".part") or f.endswith(".ytdl"):
                    continue
                out_path = os.path.join(tmpdir, f)
                break
        if not out_path or not os.path.exists(out_path):
            raise FileNotFoundError("downloaded file not found on server")

        size = os.path.getsize(out_path)
        mime = "audio/mpeg" if fmt == "mp3" else "video/mp4"
        vid_id = data.get("id") or "video"
        filename = f"auragrab_{vid_id}.{fmt}"

        def _iter_and_cleanup():
            try:
                for chunk in _stream_file(out_path):
                    yield chunk
            finally:
                shutil.rmtree(tmpdir, ignore_errors=True)

        headers = {
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Content-Length": str(size),
            "Cache-Control": "no-store",
        }
        return StreamingResponse(_iter_and_cleanup(), media_type=mime, headers=headers)
    except Exception as e:
        shutil.rmtree(tmpdir, ignore_errors=True)
        return JSONResponse(status_code=200, content={"error": f"download failed: {e}"})
