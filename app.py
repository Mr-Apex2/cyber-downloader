import os
import uuid
import json
import time
import shutil
import threading
import zipfile
from pathlib import Path

import yt_dlp
from flask import Flask, Response, jsonify, render_template, request, send_file
from werkzeug.utils import secure_filename

app = Flask(__name__)

# Railway Volume එකක් සම්බන්ධ කළහොත් DOWNLOAD_DIR=/data/downloads ලෙස Variable එකක් දමන්න.
DOWNLOAD_ROOT = Path(os.environ.get("DOWNLOAD_DIR", "downloads")).resolve()
DOWNLOAD_ROOT.mkdir(parents=True, exist_ok=True)

# Single Gunicorn worker එකක් භාවිත කරන්න. Job තොරතුරු memory තුළ තබා ඇත.
jobs = {}
jobs_lock = threading.Lock()


def update_job(job_id, **values):
    with jobs_lock:
        if job_id in jobs:
            jobs[job_id].update(values)


def audio_codec(value):
    value = str(value or "mp3").strip().lower()
    for codec in ("mp3", "m4a", "wav", "flac", "opus"):
        if value == codec or value.startswith(codec):
            return codec
    return "mp3"


def video_format(value):
    value = str(value or "best").strip().lower()
    if value == "best":
        return "bv*+ba/b"
    if value.isdigit() and value in {"480", "720", "1080", "1440", "2160"}:
        height = int(value)
        return f"bv*[height<={height}]+ba/b[height<={height}]/b"
    return "bv*+ba/b"


def safe_error_message(exc):
    message = str(exc)
    lower = message.lower()

    if any(term in lower for term in (
        "sign in to confirm", "not a bot", "login_required",
        "confirm you're not a bot"
    )):
        return (
            "YouTube විසින් Railway server ඉල්ලීම සීමා කර ඇත: "
            "Sign in to confirm you're not a bot. මෙය GitHub code හෝ "
            "frontend දෝෂයක් නොවේ. මෙම යෙදුමෙන් YouTube ආරක්ෂක/අනන්‍යතා "
            "පරීක්ෂණ මඟහැරිය නොහැක. ඔබට අයිතිය ඇති මාධ්‍ය සඳහා නිල "
            "download/export ක්‍රමයක් හෝ අයිතිකරු ලබාදුන් සෘජු download link "
            "භාවිත කරන්න."
        )

    if "ffmpeg" in lower or "ffprobe" in lower:
        return (
            "FFmpeg හමු නොවීය. Railway build එකේ Aptfile තුළ ffmpeg "
            "තිබෙන බවත් build එක නැවත සිදු කළ බවත් පරීක්ෂා කරන්න."
        )

    return message[:1200] or "Download එක අසාර්ථක විය."


def create_ydl_options(job_dir, media_type, fmt, playlist, job_id):
    def progress_hook(data):
        status = data.get("status")

        if status == "downloading":
            total = data.get("total_bytes") or data.get("total_bytes_estimate") or 0
            downloaded = data.get("downloaded_bytes") or 0
            percent = round(downloaded * 100 / total, 1) if total else 0

            update_job(
                job_id,
                status="downloading",
                progress=percent,
                speed=data.get("_speed_str") or "N/A",
                eta=data.get("_eta_str") or "N/A"
            )

        elif status == "finished":
            update_job(
                job_id,
                status="processing",
                progress=100,
                speed="N/A",
                eta="Processing"
            )

    options = {
        "outtmpl": str(job_dir / "%(title).150B-%(id)s.%(ext)s"),
        "noplaylist": not playlist,
        "ignoreerrors": False,
        "continuedl": True,
        "overwrites": False,
        "quiet": True,
        "no_warnings": True,
        "progress_hooks": [progress_hook],
        "retries": 3,
        "fragment_retries": 3,
    }

    if media_type == "audio":
        codec = audio_codec(fmt)
        options["format"] = "bestaudio/best"
        options["postprocessors"] = [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": codec,
            "preferredquality": "320" if codec == "mp3" else "0",
        }]
    else:
        options["format"] = video_format(fmt)
        options["merge_output_format"] = "mp4"

    return options


def run_download(job_id, url, media_type, fmt, playlist):
    job_dir = DOWNLOAD_ROOT / job_id
    job_dir.mkdir(parents=True, exist_ok=True)

    try:
        update_job(job_id, status="downloading", progress=0)

        options = create_ydl_options(
            job_dir, media_type, fmt, playlist, job_id
        )

        with yt_dlp.YoutubeDL(options) as ydl:
            info = ydl.extract_info(url, download=True)

        if not info:
            raise RuntimeError("මාධ්‍ය තොරතුරු ලබාගත නොහැකි විය.")

        # Playlist එකක් නම් එහි සියලුම files ZIP එකකට එකතු කරයි.
        downloaded_files = [
            p for p in job_dir.rglob("*")
            if p.is_file() and p.suffix.lower() not in {".part", ".ytdl"}
        ]

        if not downloaded_files:
            raise FileNotFoundError("Download කළ ගොනුවක් සොයාගත නොහැක.")

        if playlist:
            zip_path = DOWNLOAD_ROOT / f"{job_id}.zip"
            with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as archive:
                for file_path in downloaded_files:
                    archive.write(file_path, arcname=file_path.name)
            final_path = zip_path
        else:
            # Single item: අවසානයේ වෙනස් කළ දිනය අනුව අලුත්ම media file එක ගනී.
            final_path = max(downloaded_files, key=lambda p: p.stat().st_mtime)

        update_job(
            job_id,
            status="completed",
            progress=100,
            filename=final_path.name,
            filepath=str(final_path),
            download_url=f"/download_file/{job_id}",
            speed="Done",
            eta="0"
        )

    except Exception as exc:
        update_job(
            job_id,
            status="error",
            error=safe_error_message(exc)
        )


@app.route("/")
def index():
    # Upload කළ frontend filename එක cyber_portal.html ලෙස තිබිය යුතුය.
    return render_template("cyber_portal.html")


@app.route("/api/download", methods=["POST"])
def api_download():
    data = request.get_json(silent=True) or {}

    url = str(data.get("url") or "").strip()
    media_type = str(data.get("media_type") or "video").lower()
    fmt = str(data.get("format") or "best").strip()
    playlist = bool(data.get("is_playlist", False))

    if not url.startswith(("https://", "http://")):
        return jsonify({"error": "Please enter a valid http/https URL."}), 400

    if media_type not in {"video", "audio"}:
        return jsonify({"error": "Invalid media type."}), 400

    job_id = str(uuid.uuid4())

    with jobs_lock:
        jobs[job_id] = {
            "status": "queued",
            "progress": 0,
            "speed": "N/A",
            "eta": "N/A",
            "error": None,
            "filename": None,
            "filepath": None,
        }

    thread = threading.Thread(
        target=run_download,
        args=(job_id, url, media_type, fmt, playlist),
        daemon=True
    )
    thread.start()

    return jsonify({"job_id": job_id}), 202


@app.route("/api/progress/<job_id>")
def api_progress(job_id):
    def event_stream():
        while True:
            with jobs_lock:
                current = jobs.get(job_id)
                snapshot = dict(current) if current else None

            if snapshot is None:
                payload = {"status": "error", "error": "Job not found."}
                yield f"data: {json.dumps(payload)}\\n\\n"
                break

            yield f"data: {json.dumps(snapshot)}\\n\\n"

            if snapshot.get("status") in {"completed", "error"}:
                break

            time.sleep(0.5)

    return Response(
        event_stream(),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        }
    )


@app.route("/download_file/<job_id>")
def download_file(job_id):
    with jobs_lock:
        job = dict(jobs.get(job_id) or {})

    filepath = job.get("filepath")
    if not filepath:
        return "File not found or download is not complete.", 404

    path = Path(filepath).resolve()

    # DOWNLOAD_ROOT එකෙන් පිටත file එකක් ලබාදීම වැළැක්වීම.
    if DOWNLOAD_ROOT not in path.parents or not path.is_file():
        return "File not found.", 404

    return send_file(
        path,
        as_attachment=True,
        download_name=secure_filename(job.get("filename") or path.name)
    )


@app.get("/health")
def health():
    return jsonify({"status": "ok"})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="0.0.0.0", port=port, threaded=True)
