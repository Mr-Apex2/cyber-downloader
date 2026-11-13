import os
import uuid
import json
import shutil
import threading
from flask import Flask, render_template, request, jsonify, Response, send_file
import yt_dlp

app = Flask(__name__)
DOWNLOAD_FOLDER = os.path.join(os.getcwd(), 'downloads')
os.makedirs(DOWNLOAD_FOLDER, exist_ok=True)

download_status = {}

@app.route('/')
def main_portal():
    return render_template('cyber_portal.html')

@app.route('/api/download', methods=['POST'])
def start_download():
    data = request.json
    url = data.get('url')
    media_type = data.get('media_type')
    format_val = data.get('format')
    is_playlist = data.get('is_playlist', False)

    if not url:
        return jsonify({'error': 'Please provide a valid URL.'}), 400

    job_id = str(uuid.uuid4())
    download_status[job_id] = {'progress': 0, 'status': 'starting', 'filename': '', 'error': None}

    thread = threading.Thread(target=run_downloader, args=(job_id, url, media_type, format_val, is_playlist))
    thread.start()

    return jsonify({'job_id': job_id})

def run_downloader(job_id, url, media_type, format_val, is_playlist):
    job_folder = os.path.join(DOWNLOAD_FOLDER, job_id)
    os.makedirs(job_folder, exist_ok=True)

    def progress_hook(d):
        if d['status'] == 'downloading':
            try:
                p = d.get('_percent_str', '0%').replace('%', '').strip()
                progress_val = float(p)
            except:
                progress_val = 0
            download_status[job_id] = {
                'progress': progress_val,
                'status': 'downloading',
                'speed': d.get('_speed_str', 'N/A'),
                'eta': d.get('_eta_str', 'N/A')
            }
        elif d['status'] == 'finished':
            download_status[job_id] = {
                'progress': 100,
                'status': 'processing',
                'speed': '',
                'eta': ''
            }

    ydl_opts = {
        'outtmpl': os.path.join(job_folder, '%(title)s.%(ext)s'),
        'noplaylist': not is_playlist,
        'quiet': True,
        'no_warnings': True,
        'progress_hooks': [progress_hook],
    }

    if media_type == 'audio':
        ydl_opts['format'] = 'bestaudio/best'
        if format_val == 'mp3':
            ydl_opts['postprocessors'] = [{'key': 'FFmpegExtractAudio', 'preferredcodec': 'mp3', 'preferredquality': '320'}]
        elif format_val == 'm4a':
            ydl_opts['postprocessors'] = [{'key': 'FFmpegExtractAudio', 'preferredcodec': 'm4a', 'preferredquality': '192'}]
        elif format_val == 'wav':
            ydl_opts['postprocessors'] = [{'key': 'FFmpegExtractAudio', 'preferredcodec': 'wav'}]
        elif format_val == 'flac':
            ydl_opts['postprocessors'] = [{'key': 'FFmpegExtractAudio', 'preferredcodec': 'flac'}]
    else:
        if format_val == '2160':
            ydl_opts.update({'format': 'bestvideo[height<=2160]+bestaudio/best[height<=2160]', 'merge_output_format': 'mp4'})
        elif format_val == '1440':
            ydl_opts.update({'format': 'bestvideo[height<=1440]+bestaudio/best[height<=1440]', 'merge_output_format': 'mp4'})
        elif format_val == '1080':
            ydl_opts.update({'format': 'bestvideo[height<=1080]+bestaudio/best[height<=1080]', 'merge_output_format': 'mp4'})
        elif format_val == '720':
            ydl_opts.update({'format': 'bestvideo[height<=720]+bestaudio/best[height<=720]', 'merge_output_format': 'mp4'})
        elif format_val == '480':
            ydl_opts.update({'format': 'bestvideo[height<=480]+bestaudio/best[height<=480]', 'merge_output_format': 'mp4'})
        else:
            ydl_opts.update({'format': 'bestvideo+bestaudio/best', 'merge_output_format': 'mp4'})

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.extract_info(url, download=True)
            
            downloaded_files = os.listdir(job_folder)
            if not downloaded_files:
                download_status[job_id] = {'status': 'error', 'error': 'Failed to download media file.'}
                return

            if len(downloaded_files) > 1 or is_playlist:
                zip_filename = f"playlist_{job_id[:8]}.zip"
                zip_path = os.path.join(DOWNLOAD_FOLDER, zip_filename)
                shutil.make_archive(zip_path.replace('.zip', ''), 'zip', job_folder)
                shutil.rmtree(job_folder)
                download_status[job_id] = {'status': 'completed', 'download_url': f'/get-file/{zip_filename}', 'filename': zip_filename}
            else:
                single_file = downloaded_files[0]
                final_filename = f"{job_id[:6]}_{single_file}"
                shutil.move(os.path.join(job_folder, single_file), os.path.join(DOWNLOAD_FOLDER, final_filename))
                shutil.rmtree(job_folder)
                download_status[job_id] = {'status': 'completed', 'download_url': f'/get-file/{final_filename}', 'filename': single_file}

    except Exception as e:
        shutil.rmtree(job_folder, ignore_errors=True)
        download_status[job_id] = {'status': 'error', 'error': str(e)}

@app.route('/api/progress/<job_id>')
def progress_stream(job_id):
    def generate():
        import time
        while True:
            status = download_status.get(job_id)
            if not status:
                yield f"data: {json.dumps({'status': 'error', 'error': 'Job not found'})}\n\n"
                break
            
            yield f"data: {json.dumps(status)}\n\n"
            
            if status['status'] in ['completed', 'error']:
                break
            time.sleep(0.5)

    return Response(generate(), mimetype='text/event-stream')

@app.route('/get-file/<filename>')
def get_file(filename):
    file_path = os.path.join(DOWNLOAD_FOLDER, filename)
    if os.path.exists(file_path):
        return send_file(file_path, as_attachment=True)
    return "File not found", 404

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)