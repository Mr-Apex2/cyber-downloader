import os
from flask import Flask, render_template, request, send_file
import yt_dlp

app = Flask(__name__)

# ඩවුන්ලෝඩ් වන ගොනු තාවකාලිකව රඳවා ගැනීමට ෆෝල්ඩරයක් සකස් කිරීම
DOWNLOAD_FOLDER = 'downloads'
if not os.path.exists(DOWNLOAD_FOLDER):
    os.makedirs(DOWNLOAD_FOLDER)

@app.route('/')
def index():
    # templates ෆෝල්ඩරයේ ඇති cyber_portal.html ගොනුව ලෝඩ් කරයි
    return render_template('cyber_portal.html')

@app.route('/download', methods=['POST'])
def download():
    url = request.form.get('url')
    media_type = request.form.get('media_type', 'video')  # video හෝ audio
    
    if not url:
        return "URL එකක් ලබා දෙන්න!", 400

    try:
        # yt-dlp options (FFmpeg හරහා වීඩියෝ සහ ඕඩියෝ මර්ජ් කිරීමට)
        if media_type == 'audio':
            ydl_opts = {
                'format': 'bestaudio/best',
                'outtmpl': os.path.join(DOWNLOAD_FOLDER, '%(title)s.%(ext)s'),
                'postprocessors': [{
                    'key': 'FFmpegExtractAudio',
                    'preferredcodec': 'mp3',
                    'preferredquality': '192',
                }],
            }
        else:
            ydl_opts = {
                'format': 'bestvideo+bestaudio/best',
                'outtmpl': os.path.join(DOWNLOAD_FOLDER, '%(title)s.%(ext)s'),
                'merge_output_format': 'mp4',
            }

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            filename = ydl.prepare_filename(info)
            
            # ඕඩියෝ ලෙස ඩවුන්ලෝඩ් වන්නේ නම් එක්ස්ටෙන්ෂන් එක mp3 ලෙස වෙනස් වේ
            if media_type == 'audio':
                filename = os.path.splitext(filename)[0] + '.mp3'

        return send_file(filename, as_attachment=True)

    except Exception as e:
        return f"දෝෂයක් ඇති විය: {str(e)}", 500

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
