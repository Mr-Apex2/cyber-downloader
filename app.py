import os
import uuid
import threading
import time
import json
from flask import Flask, render_template, request, jsonify, send_file, Response
import yt_dlp

app = Flask(__name__)

# ඩවුන්ලෝඩ් වන ගොනු තාවකාලිකව රඳවා ගැනීමට ෆෝල්ඩරයක් සකස් කිරීම
DOWNLOAD_FOLDER = 'downloads'
if not os.path.exists(DOWNLOAD_FOLDER):
    os.makedirs(DOWNLOAD_FOLDER)

# ඩවුන්ලෝඩ් වන ජොබ්ස් වල තත්ත්වය (Progress) තාවකාලිකව ගබඩා කිරීමට
jobs = {}

@app.route('/')
def index():
    return render_template('cyber_portal.html')

@app.route('/api/download', methods=['POST'])
def api_download():
    data = request.get_json() or {}
    url = data.get('url')
    media_type = data.get('media_type', 'video')
    fmt = data.get('format', 'best')
    is_playlist = data.get('is_playlist', False)

    if not url:
        return jsonify({'error': 'Please enter a valid URL!'}), 400

    job_id = str(uuid.uuid4())
    jobs[job_id] = {
        'status': 'downloading',
        'progress': 0,
        'speed': '0 KB/s',
        'eta': 'N/A',
        'error': None,
        'filename': None,
        'filepath': None
    }

    # පසුබිමේ (Background thread) ඩවුන්ලෝඩ් ක්‍රියාවලිය ආරම්භ කිරීම
    threading.Thread(target=run_download_job, args=(job_id, url, media_type, fmt, is_playlist)).start()

    return jsonify({'job_id': job_id})

def run_download_job(job_id, url, media_type, fmt, is_playlist):
    def progress_hook(d):
        if d['status'] == 'downloading':
            total = d.get('total_bytes') or d.get('total_bytes_estimate') or 0
            downloaded = d.get('downloaded_bytes', 0)
            percent = (downloaded / total * 100) if total > 0 else 0
            
            speed = d.get('_speed_str', 'N/A')
            eta = d.get('_eta_str', 'N/A')

            jobs[job_id]['progress'] = percent
            jobs[job_id]['speed'] = speed
            jobs[job_id]['eta'] = eta
        elif d['status'] == 'finished':
            jobs[job_id]['status'] = 'processing'

    try:
        ydl_opts = {
            'progress_hooks': [progress_hook],
            'outtmpl': os.path.join(DOWNLOAD_FOLDER, '%(title)s.%(ext)s'),
            'extractor_args': {'youtube': {'player_client': ['ios', 'android', 'web']}},
        }

        # ඔබ cookies.txt ගොනුව GitHub වෙත දමා ඇත්නම් පහත රේඛාවේ ඇති # ලකුණ ඉවත් කරන්න:
        # ydl_opts['cookiefile'] = 'cookies.txt'

        if not is_playlist:
            ydl_opts['noplaylist'] = True

        if media_type == 'audio':
            ydl_opts['format'] = 'bestaudio/best'
            codec = fmt if fmt in ['mp3', 'm4a', 'wav', 'flac'] else 'mp3'
            ydl_opts['postprocessors'] = [{
                'key': 'FFmpegExtractAudio',
                'preferredcodec': codec,
                'preferredquality': '192',
            }]
        else:
            if fmt == 'best':
                ydl_opts['format'] = 'best/bestvideo+bestaudio'
            else:
                ydl_opts['format'] = f'best[height<={fmt}]/best/bv*[height<={fmt}]+ba/b'
            ydl_opts['merge_output_format'] = 'mp4'

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            if 'entries' in info:
                filename = ydl.prepare_filename(info['entries'][0])
            else:
                filename = ydl.prepare_filename(info)

            if media_type == 'audio':
                codec = fmt if fmt in ['mp3', 'm4a', 'wav', 'flac'] else 'mp3'
                filename = os.path.splitext(filename)[0] + f'.{codec}'
            elif not filename.endswith('.mp4'):
                filename = os.path.splitext(filename)[0] + '.mp4'

            basename = os.path.basename(filename)
            jobs[job_id]['status'] = 'completed'
            jobs[job_id]['filename'] = basename
            jobs[job_id]['filepath'] = filename
            jobs[job_id]['download_url'] = f'/download_file/{job_id}'

    except Exception as e:
        jobs[job_id]['status'] = 'error'
        jobs[job_id]['error'] = str(e)

@app.route('/api/progress/<job_id>')
def api_progress(job_id):
    def generate():
        while job_id in jobs:
            job = jobs[job_id]
            yield f"data: {json.dumps(job)}\n\n"
            if job['status'] in ['completed', 'error']:
                break
            time.sleep(0.5)
    return Response(generate(), mimetype='text/event-stream')

@app.route('/download_file/<job_id>')
def download_file(job_id):
    job = jobs.get(job_id)
    if job and job.get('filepath') and os.path.exists(job['filepath']):
        return send_file(job['filepath'], as_attachment=True, download_name=job['filename'])
    return "File not found or expired.", 404

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
