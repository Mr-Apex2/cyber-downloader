"""CYBER X backend (Flask + yt-dlp) for Railway.
API used by templates/cyber_portal.html (frontend is unchanged):
  POST /api/download        {url, media_type, format, is_playlist} -> {job_id}
  GET  /api/progress/<id>   Server-Sent Events: downloading | processing | completed | error
  GET  /api/file/<id>       the finished file (or ZIP for playlists)

Job state is in memory -> run with ONE gunicorn worker (see Procfile / Dockerfile).
Use only media you own or have permission to download.
"""
import html
import ipaddress
import json
import os
import re
import shutil
import socket
import threading
import time
import uuid
import zipfile
from pathlib import Path
from urllib.parse import urlparse

import yt_dlp
from flask import Flask, Response, abort, jsonify, request, send_file

BASE_DIR = Path(__file__).resolve().parent
PAGE = BASE_DIR / "templates" / "cyber_portal.html"

DOWNLOAD_DIR = Path(os.environ.get("DOWNLOAD_DIR", "/tmp/downloads"))
MAX_ACTIVE_JOBS = int(os.environ.get("MAX_ACTIVE_JOBS", "2"))
MAX_PLAYLIST_ITEMS = int(os.environ.get("MAX_PLAYLIST_ITEMS", "100"))
JOB_TTL = int(os.environ.get("JOB_TTL_SECONDS", "3600"))  # keep finished files this long

VIDEO_HEIGHTS = {"best", "2160", "1440", "1080", "720", "480"}
AUDIO_CODECS = {"mp3", "m4a", "wav", "flac"}
JUNK_SUFFIXES = {".part", ".ytdl", ".temp", ".tmp", ".json", ".jpg", ".jpeg", ".png",
                 ".webp", ".vtt", ".srt", ".description"}
ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 32 * 1024  # request bodies are tiny JSON

JOBS = {}
LOCK = threading.Lock()


# ----------------------------------------------------------------- helpers
def now():
    return time.time()


def update_job(job_id, **fields):
    with LOCK:
        job = JOBS.get(job_id)
        if job:
            job.update(fields)
            job["updated"] = now()


def fmt_speed(bps):
    if not bps:
        return "N/A"
    for unit in ("B/s", "KB/s", "MB/s", "GB/s"):
        if bps < 1024 or unit == "GB/s":
            return f"{bps:.1f} {unit}"
        bps /= 1024


def fmt_eta(sec):
    if sec is None:
        return "N/A"
    sec = int(sec)
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h:d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def is_public_host(host):
    """Block localhost / private network targets (SSRF protection)."""
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return False
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved
                or ip.is_multicast or ip.is_unspecified):
            return False
    return True


def clean_error(exc):
    msg = ANSI_RE.sub("", str(exc)).strip()
    msg = re.sub(r"^(ERROR:\s*)+", "", msg)
    low = msg.lower()
    if "sign in to confirm" in low or "confirm you're not a bot" in low or "confirm you’re not a bot" in low:
        msg = ("YouTube is asking for bot verification from this server, so this link "
               "can't be downloaded from here right now. Try another link or try again later.")
    elif "ffmpeg" in low and ("not found" in low or "not installed" in low):
        msg = "FFmpeg is not installed on the server (needed for merging / audio conversion)."
    elif "private video" in low or "video unavailable" in low:
        msg = "This video is private or unavailable."
    if len(msg) > 300:
        msg = msg[:297] + "..."
    return html.escape(msg)  # the frontend inserts errors with innerHTML


def safe_name(text, default="playlist"):
    text = re.sub(r"[^\w\- .]", "", text or "").strip()
    return (text[:80] or default)


# ------------------------------------------------------------ yt-dlp job
def build_opts(job_id, job_dir, media_type, fmt, is_playlist):
    def progress_hook(d):
        info = d.get("info_dict") or {}
        if d.get("status") == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            done = d.get("downloaded_bytes") or 0
            frac = min(done / total, 1.0) if total else 0.0
            streams = len(info.get("requested_formats") or []) or 1
            items = info.get("n_entries") or info.get("playlist_count") or 1
            with LOCK:
                job = JOBS.get(job_id)
                if not job:
                    return
                overall = (job["_files_done"] + frac) / (items * streams) * 100
                job.update(status="downloading",
                           progress=round(max(job["progress"], min(overall, 99.0)), 1),
                           speed=fmt_speed(d.get("speed")), eta=fmt_eta(d.get("eta")),
                           updated=now())
        elif d.get("status") == "finished":
            with LOCK:
                job = JOBS.get(job_id)
                if job:
                    job["_files_done"] += 1

    def pp_hook(d):
        if d.get("status") in ("started", "processing"):
            update_job(job_id, status="processing")

    template = "%(autonumber)03d - %(title).120B.%(ext)s" if is_playlist else "%(title).150B [%(id)s].%(ext)s"
    opts = {
        "outtmpl": str(job_dir / template),
        "noplaylist": not is_playlist,
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "windowsfilenames": True,
        "retries": 5,
        "fragment_retries": 5,
        "concurrent_fragment_downloads": 4,
        "progress_hooks": [progress_hook],
        "postprocessor_hooks": [pp_hook],
    }
    if is_playlist:
        opts["playlistend"] = MAX_PLAYLIST_ITEMS
        opts["ignoreerrors"] = "only_download"  # skip unavailable items, keep going

    if media_type == "audio":
        pp = {"key": "FFmpegExtractAudio", "preferredcodec": fmt}
        if fmt == "mp3":
            pp["preferredquality"] = "320"
        opts["format"] = "bestaudio/best"
        opts["postprocessors"] = [pp]
    else:
        if fmt == "best":
            opts["format"] = "bv*+ba/b"
        else:
            h = int(fmt)
            opts["format"] = f"bv*[height<={h}]+ba/b[height<={h}]"
        opts["format_sort"] = ["res", "ext:mp4:m4a"]
        opts["merge_output_format"] = "mp4"
    return opts


def run_job(job_id, url, media_type, fmt, is_playlist):
    job_dir = DOWNLOAD_DIR / job_id
    try:
        job_dir.mkdir(parents=True, exist_ok=True)
        with yt_dlp.YoutubeDL(build_opts(job_id, job_dir, media_type, fmt, is_playlist)) as ydl:
            info = ydl.extract_info(url, download=True)

        files = sorted(p for p in job_dir.iterdir()
                       if p.is_file() and p.suffix.lower() not in JUNK_SUFFIXES and p.suffix.lower() != ".zip")
        if not files:
            raise RuntimeError("Nothing was downloaded (the items may be unavailable or restricted).")

        update_job(job_id, status="processing")
        if len(files) > 1:
            title = safe_name((info or {}).get("title"))
            final = job_dir / f"{title}.zip"
            with zipfile.ZipFile(final, "w", zipfile.ZIP_STORED) as zf:  # media is already compressed
                for f in files:
                    zf.write(f, arcname=f.name)
            for f in files:
                f.unlink(missing_ok=True)
        else:
            final = files[0]

        update_job(job_id, status="completed", progress=100.0, path=str(final), filename=final.name)
    except Exception as exc:  # noqa: BLE001 - report any failure to the UI
        update_job(job_id, status="error", error=clean_error(exc))
        shutil.rmtree(job_dir, ignore_errors=True)


# ----------------------------------------------------------------- cleanup
def cleanup_loop():
    while True:
        time.sleep(300)
        cutoff = now() - JOB_TTL
        with LOCK:
            old = [jid for jid, j in JOBS.items()
                   if j["status"] in ("completed", "error") and j["updated"] < cutoff]
            for jid in old:
                JOBS.pop(jid, None)
        for jid in old:
            shutil.rmtree(DOWNLOAD_DIR / jid, ignore_errors=True)


def startup_cleanup():
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    cutoff = now() - JOB_TTL
    for child in DOWNLOAD_DIR.iterdir():  # leftovers from a previous deploy/volume
        try:
            if child.is_dir() and child.stat().st_mtime < cutoff:
                shutil.rmtree(child, ignore_errors=True)
        except OSError:
            pass


startup_cleanup()
threading.Thread(target=cleanup_loop, daemon=True).start()


# ------------------------------------------------------------------ routes
@app.get("/")
def index():
    return send_file(PAGE, mimetype="text/html")  # served as-is, no templating


@app.get("/healthz")
def healthz():
    return jsonify(status="ok")


@app.post("/api/download")
def api_download():
    data = request.get_json(silent=True) or {}
    url = str(data.get("url", "")).strip()
    media_type = data.get("media_type", "video")
    fmt = str(data.get("format", "best"))
    is_playlist = bool(data.get("is_playlist", False))

    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return jsonify(error="Please enter a valid http(s) URL."), 400
    if not is_public_host(parsed.hostname):
        return jsonify(error="This address can't be downloaded."), 400
    if media_type == "audio":
        if fmt not in AUDIO_CODECS:
            return jsonify(error="Unsupported audio format."), 400
    elif media_type == "video":
        if fmt not in VIDEO_HEIGHTS:
            return jsonify(error="Unsupported video quality."), 400
    else:
        return jsonify(error="Unsupported media type."), 400

    job_id = uuid.uuid4().hex
    with LOCK:
        active = sum(1 for j in JOBS.values() if j["status"] in ("downloading", "processing"))
        if active >= MAX_ACTIVE_JOBS:
            return jsonify(error="Server is busy with other downloads. Please try again in a minute."), 429
        JOBS[job_id] = {"status": "downloading", "progress": 0.0, "speed": "N/A", "eta": "N/A",
                        "path": None, "filename": None, "error": None,
                        "_files_done": 0, "created": now(), "updated": now()}

    threading.Thread(target=run_job, args=(job_id, url, media_type, fmt, is_playlist),
                     daemon=True).start()
    return jsonify(job_id=job_id)


def public_view(job_id):
    with LOCK:
        job = JOBS.get(job_id)
        if not job:
            return None
        status = job["status"]
        if status == "completed":
            return {"status": "completed", "download_url": f"/api/file/{job_id}",
                    "filename": html.escape(job["filename"] or "download")}
        if status == "error":
            return {"status": "error", "error": job["error"] or "Unknown error."}
        return {"status": status, "progress": job["progress"], "speed": job["speed"], "eta": job["eta"]}


@app.get("/api/progress/<job_id>")
def api_progress(job_id):
    if public_view(job_id) is None:
        return jsonify(error="Job not found."), 404

    def stream():
        last, last_beat = None, now()
        while True:
            snap = public_view(job_id)
            if snap is None:
                yield f"data: {json.dumps({'status': 'error', 'error': 'Job expired.'})}\n\n"
                return
            payload = json.dumps(snap)
            if payload != last:
                yield f"data: {payload}\n\n"
                last, last_beat = payload, now()
            elif now() - last_beat > 15:
                yield ": keepalive\n\n"  # stops proxies closing an idle stream
                last_beat = now()
            if snap["status"] in ("completed", "error"):
                return
            time.sleep(0.5)

    return Response(stream(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no",
                             "Connection": "keep-alive"})


@app.get("/api/file/<job_id>")
def api_file(job_id):
    with LOCK:
        job = JOBS.get(job_id)
        path = job["path"] if job and job["status"] == "completed" else None
    if not path or not Path(path).is_file():
        abort(404)
    return send_file(path, as_attachment=True, download_name=Path(path).name)


if __name__ == "__main__":  # local dev only; Railway uses gunicorn
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), threaded=True)
