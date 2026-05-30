from flask import Flask, request, jsonify, render_template, send_file, send_from_directory
from flask_socketio import SocketIO
import yt_dlp
import os
import uuid
import threading
import glob
import datetime
import re

app = Flask(__name__)
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='threading')

DOWNLOAD_FOLDER = 'downloads'
os.makedirs(DOWNLOAD_FOLDER, exist_ok=True)

YDL_OPTIONS = {
    'quiet': True,
    'noplaylist': True,
    'no_warnings': True,
    'user_agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36',
    'geo_bypass': True,
    'nocheckcertificate': True,
    'socket_timeout': 30
}

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/search', methods=['POST'])
def search_youtube():
    query = request.json.get('query')
    if not query: return jsonify({'error': 'Query required'}), 400
    try:
        search_opts = {'quiet': True, 'extract_flat': True}
        with yt_dlp.YoutubeDL(search_opts) as ydl:
            if 'http' in query:
                search_query = query
            else:
                search_query = f"ytsearch10:{query}"
                
            info = ydl.extract_info(search_query, download=False)
            results = []
            entries = info.get('entries', [info]) if 'entries' in info else [info]
            
            for entry in entries:
                if entry:
                    results.append({
                        'title': entry.get('title'),
                        'thumbnail': entry.get('thumbnails', [{'url': ''}])[0]['url'] if entry.get('thumbnails') else '',
                        'url': entry.get('url'),
                        'duration': entry.get('duration_string', 'N/A'),
                        'uploader': entry.get('uploader', 'YouTube')
                    })
            return jsonify({'results': results})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/get-formats', methods=['POST'])
def get_formats():
    url = request.json.get('url')
    if not url: return jsonify({'error': 'URL required'}), 400
    try:
        with yt_dlp.YoutubeDL(YDL_OPTIONS) as ydl:
            info = ydl.extract_info(url, download=False)
            formats = info.get('formats', [])
            
            music_options = []
            video_options = []
            seen_video_res = set()
            seen_audio_abr = set()

            for f in formats:
                if f.get('vcodec') == 'none' and f.get('acodec') != 'none':
                    abr = f.get('abr')
                    if not abr or abr in seen_audio_abr: continue
                    seen_audio_abr.add(abr)
                    size = f.get('filesize') or f.get('filesize_approx') or 0
                    if size > 0:
                        music_options.append({
                            'id': f.get('format_id'),
                            'quality': f"{int(abr)}K",
                            'format': 'MP3',
                            'size': f"{size / (1024 * 1024):.1f}MB",
                            'is_audio': True
                        })
                elif f.get('vcodec') != 'none':
                    height = f.get('height')
                    if not height or height < 144: continue
                    res_label = f"{height}P"
                    if height >= 720: res_label += " HD"
                    if res_label in seen_video_res: continue
                    seen_video_res.add(res_label)
                    size = f.get('filesize') or f.get('filesize_approx') or 0
                    video_options.append({
                        'id': f.get('format_id'),
                        'quality': res_label,
                        'format': 'Auto', 
                        'size': f"{size / (1024 * 1024):.1f}MB" if size > 0 else "Auto",
                        'is_audio': False
                    })
            
            music_options = sorted(music_options, key=lambda x: int(x['quality'].replace('K', '')), reverse=True)
            video_options = sorted(video_options, key=lambda x: int(x['quality'].split('P')[0]), reverse=True)
            
            return jsonify({
                'title': info.get('title', 'Video'),
                'thumbnail': info.get('thumbnail'),
                'platform': info.get('extractor_key', 'Web').lower(),
                'music': music_options,
                'video': video_options
            })
    except Exception as e:
        return jsonify({'error': str(e)}), 500

def progress_hook(d, socket_id):
    if d['status'] == 'downloading':
        total_bytes = d.get('total_bytes') or d.get('total_bytes_estimate')
        downloaded = d.get('downloaded_bytes', 0)
        
        if total_bytes:
            percent = (downloaded / total_bytes) * 100
            socketio.emit('progress', {'percent': round(percent, 1), 'status': 'Downloading...'}, room=socket_id)
        else:
            mb_downloaded = downloaded / (1024 * 1024)
            socketio.emit('progress', {'percent': 50, 'status': f'Downloading... ({mb_downloaded:.1f} MB)'}, room=socket_id)
            
    elif d['status'] == 'finished':
        socketio.emit('progress', {'percent': 100, 'status': 'Instant Merging...'}, room=socket_id)

@app.route('/process-download', methods=['POST'])
def process_download():
    data = request.json
    url = data.get('url')
    format_id = data.get('format_id')
    is_audio = data.get('is_audio')
    socket_id = data.get('socket_id')
    task_id = str(uuid.uuid4())
    
    def download_task():
        try:
            ydl_opts = YDL_OPTIONS.copy()
            ydl_opts['outtmpl'] = os.path.join(DOWNLOAD_FOLDER, f'{task_id}.%(ext)s')
            ydl_opts['progress_hooks'] = [lambda d: progress_hook(d, socket_id)]
            
            if is_audio:
                ydl_opts['format'] = format_id
                ydl_opts['postprocessors'] = [{'key': 'FFmpegExtractAudio', 'preferredcodec': 'mp3'}]
            else:
                ydl_opts['format'] = f'{format_id}+bestaudio/best' if format_id != 'best' else 'best'

            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=True)
                final_title = info.get('title', 'Media')
            
            downloaded_files = glob.glob(os.path.join(DOWNLOAD_FOLDER, f'{task_id}.*'))
            if downloaded_files:
                actual_ext = os.path.splitext(downloaded_files[0])[1]
                clean_title = re.sub(r'[\\/*?:"<>|]', "", final_title)
                new_path = os.path.join(DOWNLOAD_FOLDER, f"{clean_title[:50]}{actual_ext}")
                try:
                    os.rename(downloaded_files[0], new_path)
                except Exception as e:
                    pass 
            else:
                actual_ext = '.mp4'
                clean_title = "Media"
            
            # Browser ko clean title bhej rahe hain
            socketio.emit('download_complete', {'task_id': task_id, 'ext': actual_ext, 'title': clean_title[:50]}, room=socket_id)

        except Exception as e:
            socketio.emit('progress', {'percent': 0, 'status': f'Error! Refresh & Try Again'}, room=socket_id)
            print(f"ERROR: {str(e)}")

    thread = threading.Thread(target=download_task)
    thread.daemon = True
    thread.start()
    return jsonify({"status": "started"})

@app.route('/api/files', methods=['GET'])
def list_files():
    files_list = []
    if os.path.exists(DOWNLOAD_FOLDER):
        for f in os.listdir(DOWNLOAD_FOLDER):
            file_path = os.path.join(DOWNLOAD_FOLDER, f)
            if os.path.isfile(file_path):
                size_mb = os.path.getsize(file_path) / (1024 * 1024)
                ext = f.split('.')[-1].upper()
                mod_time = os.path.getmtime(file_path)
                date_str = datetime.datetime.fromtimestamp(mod_time).strftime('%Y-%m-%d')
                is_audio = ext in ['MP3', 'M4A', 'OPUS']
                
                files_list.append({
                    'name': f,
                    'size': f"{size_mb:.1f} MB",
                    'type': ext,
                    'date': date_str,
                    'is_audio': is_audio,
                    'mod_time': mod_time
                })
    files_list.sort(key=lambda x: x['mod_time'], reverse=True)
    return jsonify({'files': files_list})

@app.route('/play/<filename>')
def play_file(filename):
    return send_from_directory(DOWNLOAD_FOLDER, filename)

# Cloud se browser mein direct file bhejne ka route
@app.route('/get-file', methods=['GET'])
def get_file():
    task_id = request.args.get('task_id')
    ext = request.args.get('ext')
    title = request.args.get('title')
    
    possible_file = os.path.join(DOWNLOAD_FOLDER, f"{title}{ext}")
    if not os.path.exists(possible_file):
        possible_file = os.path.join(DOWNLOAD_FOLDER, f"{task_id}{ext}")
        
    if os.path.exists(possible_file):
        return send_file(possible_file, as_attachment=True, download_name=f"{title}{ext}")
    return "File not found", 404

if __name__ == '__main__':
    # Yeh line cloud deployment ke liye zaroori hai!
    port = int(os.environ.get('PORT', 5000))
    socketio.run(app, host='0.0.0.0', port=port)