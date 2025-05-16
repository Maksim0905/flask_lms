import os
import uuid
from datetime import datetime, timedelta
from flask import Flask, request, jsonify, render_template, redirect, url_for, session, send_file, Response, flash
import sqlite3
from functools import wraps
import secrets
import logging
from werkzeug.utils import secure_filename
import subprocess
import threading
import time
import glob
from ctypes import windll

logging.basicConfig(level=logging.DEBUG, 
                   format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
                   handlers=[
                       logging.StreamHandler(),
                       logging.FileHandler('app.log')
                   ])
logger = logging.getLogger(__name__)

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", secrets.token_hex(16))


DB_PATH = 'teacher_student.db'

clients = {}      
teachers = {}     
sessions = {}     

TEACHER_USERNAME = "teacher"  
TEACHER_PASSWORD = "password"  
TOKEN_LENGTH = 32
SESSION_EXPIRY = timedelta(hours=24)  
CLIENT_EXPIRY = timedelta(minutes=5)  

ffmpeg_processes = {}  

PROXY_PORT_START = 8100  
next_proxy_port = PROXY_PORT_START

def get_next_proxy_port():
    global next_proxy_port
    port = next_proxy_port
    next_proxy_port += 1
    return port

def init_db():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS clients (
        client_id TEXT PRIMARY KEY,
        token TEXT NOT NULL,
        created_at TEXT NOT NULL
    )
    ''')
    
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS commands (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        client_id TEXT NOT NULL,
        command TEXT NOT NULL,
        status TEXT DEFAULT 'pending',
        created_at TEXT NOT NULL,
        FOREIGN KEY (client_id) REFERENCES clients (client_id)
    )
    ''')
    
    conn.commit()
    conn.close()

init_db()

def validate_token(client_id, token):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT token FROM clients WHERE client_id = ?", (client_id,))
    result = cursor.fetchone()
    conn.close()
    
    if result and result[0] == token:
        return True
    return False

def require_token(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        client_id = kwargs.get('client_id') or request.args.get('client_id')
        token = request.args.get('token')
        
        if not client_id or not token or not validate_token(client_id, token):
            return jsonify({"error": "Invalid client_id or token"}), 401
            
        return f(*args, **kwargs)
    return decorated_function

def init_demo_data():
    teachers[TEACHER_USERNAME] = {
        "password": TEACHER_PASSWORD  
    }
    logger.info(f"Инициализированы демо-данные. Учитель: {TEACHER_USERNAME}, пароль: {TEACHER_PASSWORD}")

init_demo_data()

def cleanup_data():
    now = datetime.now()
    
    inactive_clients = []
    for client_id, client_data in clients.items():
        last_seen = client_data.get("last_seen", datetime.min)
        if now - last_seen > CLIENT_EXPIRY:
            inactive_clients.append(client_id)
    
    for client_id in inactive_clients:
        stop_ffmpeg_proxy(client_id)
        del clients[client_id]
        logger.info(f"Удален неактивный клиент: {client_id}")
    
    expired_sessions = []
    for session_token, session_data in sessions.items():
        expires = session_data.get("expires", datetime.min)
        if now > expires:
            expired_sessions.append(session_token)
    
    for session_token in expired_sessions:
        del sessions[session_token]
        logger.info(f"Удалена истекшая сессия")

def require_client_auth(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        client_id = kwargs.get('client_id')
        token = request.args.get('token')
        
        if not client_id or not token:
            return jsonify({"error": "Missing client_id or token"}), 401
        
        if client_id not in clients:
            return jsonify({"error": "Unknown client_id"}), 401
            
        client = clients[client_id]
        if client.get('token') != token:
            return jsonify({"error": "Invalid token"}), 401
            
        client['last_seen'] = datetime.now()
        
        return f(*args, **kwargs)
    return decorated_function

def require_teacher_auth(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        session_token = request.cookies.get('session_token')
        
        if not session_token or session_token not in sessions:
            return redirect(url_for('login'))
            
        session_data = sessions[session_token]
        now = datetime.now()
        
        if now > session_data.get('expires', datetime.min):
            del sessions[session_token]
            return redirect(url_for('login'))
            
        session_data['expires'] = now + SESSION_EXPIRY
        
        return f(*args, **kwargs)
    return decorated_function

@app.before_request
def before_request():
    cleanup_data()

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        
        if username in teachers and teachers[username]['password'] == password:
            session_token = secrets.token_urlsafe(TOKEN_LENGTH)
            sessions[session_token] = {
                'username': username,
                'expires': datetime.now() + SESSION_EXPIRY
            }
            
            response = redirect(url_for('dashboard'))
            response.set_cookie('session_token', session_token, httponly=True, samesite='Lax')
            return response
        else:
            flash('Неверное имя пользователя или пароль')
    
    return render_template('login.html')

@app.route('/logout')
def logout():
    session_token = request.cookies.get('session_token')
    if session_token and session_token in sessions:
        del sessions[session_token]
    
    response = redirect(url_for('login'))
    response.delete_cookie('session_token')
    return response

@app.route('/')
@require_teacher_auth
def dashboard():
    active_clients = []
    for client_id, client_data in clients.items():
        last_seen = client_data.get('last_seen', datetime.min)
        if datetime.now() - last_seen <= CLIENT_EXPIRY:
            has_stream = bool(client_data.get('stream_info'))
            proxy_url = None
            
            if client_id in ffmpeg_processes and ffmpeg_processes[client_id].get('proxy_port'):
                proxy_url = f"/stream/{client_id}"
            
            active_clients.append({
                'client_id': client_id,
                'last_seen': last_seen,
                'has_stream': has_stream,
                'proxy_url': proxy_url
            })
    
    active_clients.sort(key=lambda x: x['last_seen'], reverse=True)
    
    return render_template('dashboard.html', clients=active_clients)

@app.route('/view/<client_id>')
@require_teacher_auth
def view_client(client_id):
    if client_id not in clients:
        flash('Клиент не найден')
        return redirect(url_for('dashboard'))
    
    client_data = clients.get(client_id, {})
    
    return render_template('view.html', client_id=client_id, client_data=client_data)

@app.route('/send-command/<client_id>', methods=['POST'])
@require_teacher_auth
def send_command(client_id):
    command = request.form.get('command')
    if not command:
        return jsonify({'success': False, 'error': 'Missing command'})
    
    if client_id not in clients:
        return jsonify({'success': False, 'error': 'Client not found'})
    
    command_id = str(uuid.uuid4())
    
    if 'commands' not in clients[client_id]:
        clients[client_id]['commands'] = []
    
    clients[client_id]['commands'].append({'id': command_id, 'command': command, 'status': 'pending'})
    
    return jsonify({'success': True, 'command_id': command_id})

@app.route('/api/register', methods=['POST'])
def register_client():
    client_id = str(uuid.uuid4())
    token = secrets.token_urlsafe(TOKEN_LENGTH)
    
    clients[client_id] = {
        'token': token,
        'last_seen': datetime.now(),
        'commands': [],
        'stream_info': None
    }
    
    logger.info(f"Зарегистрирован новый клиент: {client_id}")
    
    return jsonify({
        'client_id': client_id,
        'token': token
    })

@app.route('/api/register-stream/<client_id>', methods=['POST'])
@require_client_auth
def register_stream(client_id):
    try:
        stream_data = request.json
        if not stream_data:
            return jsonify({"error": "No stream data provided"}), 400
        
        stream_type = stream_data.get('stream_type')
        stream_url = stream_data.get('stream_url')
        
        if not stream_type or not stream_url:
            return jsonify({"error": "Invalid stream data"}), 400
        
        clients[client_id]['stream_info'] = {
            'type': stream_type,
            'url': stream_url,
            'registered_at': datetime.now().isoformat()
        }
        
        start_ffmpeg_proxy(client_id, stream_url)
        
        logger.info(f"Клиент {client_id} зарегистрировал стрим: {stream_type}, {stream_url}")
        
        return jsonify({"success": True, "message": "Stream registered successfully"})
    except Exception as e:
        logger.error(f"Ошибка при регистрации стрима: {e}")
        return jsonify({"error": f"Error registering stream: {str(e)}"}), 500

def start_ffmpeg_proxy(client_id, source_url):
    stop_ffmpeg_proxy(client_id)
    
    try:
        try:
            ffmpeg_version = subprocess.run(['ffmpeg', '-version'], capture_output=True, text=True)
            logger.info(f"FFmpeg версия: {ffmpeg_version.stdout.split('\\n')[0]}")
        except Exception as e:
            logger.error(f"FFmpeg не установлен или недоступен: {e}")
            return False
            
        proxy_port = get_next_proxy_port()
        hls_path = os.path.join('static', 'streams', client_id)
        
        os.makedirs(hls_path, exist_ok=True)
        logger.info(f"Создана директория для HLS: {hls_path}")
        
        abs_hls_path = os.path.abspath(hls_path)
        logger.info(f"Абсолютный путь к директории HLS: {abs_hls_path}")
        
        cmd = [
            'ffmpeg',
            '-i', source_url,                
            '-c:v', 'copy',                  
            '-f', 'hls',                     
            '-hls_time', '0.2',              
            '-hls_list_size', '3',           
            '-hls_flags', 'delete_segments+append_list+discont_start+omit_endlist+independent_segments', 
            '-hls_segment_type', 'mpegts',   
            '-hls_init_time', '0',           
            '-hls_allow_cache', '0',         
            '-hls_segment_filename', f"{hls_path}/segment_%03d.ts",  
            f"{hls_path}/playlist.m3u8"      
        ]
        
        logger.info(f"Запуск FFmpeg прокси для клиента {client_id}: {' '.join(cmd)}")
        
        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=10**8
        )
        
        if os.name == 'nt':
            try:
                windll.kernel32.SetPriorityClass(int(process._handle), 0x00008000)
                logger.info("Установлен приоритет процесса FFmpeg: ВЫСОКИЙ")
            except Exception as e:
                logger.error(f"Не удалось установить приоритет процесса: {e}")
        
        threading.Thread(target=read_ffmpeg_output, args=(process.stdout, client_id), daemon=True).start()
        threading.Thread(target=read_ffmpeg_output, args=(process.stderr, client_id), daemon=True).start()
        
        ffmpeg_processes[client_id] = {
            'process': process,
            'proxy_port': proxy_port,
            'hls_path': hls_path,
            'start_time': datetime.now().isoformat()
        }
        
        clients[client_id]['stream_info']['proxy_url'] = f"/stream/{client_id}"
        
        threading.Thread(target=monitor_hls_files, args=(client_id, hls_path), daemon=True).start()
        
        return True
    except Exception as e:
        logger.error(f"Ошибка при запуске FFmpeg прокси: {e}", exc_info=True)
        return False

def monitor_hls_files(client_id, hls_path):
    logger.info(f"Запущен мониторинг HLS файлов для клиента {client_id} в пути {hls_path}")
    
    end_time = time.time() + 60
    
    while time.time() < end_time:
        try:
            playlist_file = os.path.join(hls_path, "playlist.m3u8")
            segment_files = glob.glob(os.path.join(hls_path, "segment_*.ts"))
            
            logger.info(f"Состояние HLS для {client_id}: playlist существует: {os.path.exists(playlist_file)}, сегментов: {len(segment_files)}")
            
            if os.path.exists(playlist_file):
                with open(playlist_file, 'r') as f:
                    content = f.read()
                    logger.debug(f"Содержимое playlist.m3u8 для {client_id}:\n{content}")
            
            time.sleep(5)
            
            if client_id not in ffmpeg_processes or ffmpeg_processes[client_id].get('process').poll() is not None:
                logger.warning(f"FFmpeg процесс для клиента {client_id} завершился, мониторинг остановлен")
                break
                
        except Exception as e:
            logger.error(f"Ошибка при мониторинге HLS файлов: {e}")
            time.sleep(5)

def read_ffmpeg_output(pipe, client_id):
    for line in iter(pipe.readline, b''):
        try:
            line_text = line.decode('utf-8').strip()
            if line_text:
                if "error" in line_text.lower() or "failed" in line_text.lower():
                    logger.error(f"FFmpeg [{client_id}]: {line_text}")
                else:
                    logger.debug(f"FFmpeg [{client_id}]: {line_text}")
        except Exception as e:
            logger.error(f"Ошибка при чтении вывода FFmpeg: {e}")

def stop_ffmpeg_proxy(client_id):
    if client_id in ffmpeg_processes:
        try:
            process = ffmpeg_processes[client_id].get('process')
            if process and process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()
                
                logger.info(f"FFmpeg прокси для клиента {client_id} остановлен")
            
            hls_path = ffmpeg_processes[client_id].get('hls_path')
            if hls_path and os.path.exists(hls_path):
                for file in os.listdir(hls_path):
                    try:
                        os.remove(os.path.join(hls_path, file))
                    except:
                        pass
                try:
                    os.rmdir(hls_path)
                except:
                    pass
            
            del ffmpeg_processes[client_id]
            
            if client_id in clients and clients[client_id].get('stream_info'):
                clients[client_id]['stream_info'].pop('proxy_url', None)
            
            return True
        except Exception as e:
            logger.error(f"Ошибка при остановке FFmpeg прокси: {e}")
            return False
    return False

@app.route('/api/commands/<client_id>', methods=['GET'])
@require_client_auth
def get_commands(client_id):
    client = clients[client_id]
    
    pending_commands = [cmd for cmd in client.get('commands', []) if cmd.get('status') == 'pending']
    
    return jsonify({'commands': pending_commands})

@app.route('/api/commands/<client_id>/ack', methods=['POST'])
@require_client_auth
def ack_commands(client_id):
    client = clients[client_id]
    data = request.json
    
    command_ids = data.get('command_ids', [])
    
    if not command_ids:
        for cmd in client.get('commands', []):
            if cmd.get('status') == 'pending':
                cmd['status'] = 'completed'
                cmd['completed_at'] = datetime.now().isoformat()
    else:
        for cmd in client.get('commands', []):
            if cmd.get('id') in command_ids and cmd.get('status') == 'pending':
                cmd['status'] = 'completed'
                cmd['completed_at'] = datetime.now().isoformat()
    
    return jsonify({'success': True})

@app.route('/api/command-result/<client_id>', methods=['POST'])
@require_client_auth
def command_result(client_id):
    client = clients[client_id]
    data = request.json
    
    command_id = data.get('command_id')
    if not command_id:
        return jsonify({"error": "Missing command_id"}), 400
        
    stdout = data.get('stdout', '')
    stderr = data.get('stderr', '')
    exit_code = data.get('exit_code', -1)
    
    for cmd in client.get('commands', []):
        if cmd.get('id') == command_id:
            cmd['stdout'] = stdout
            cmd['stderr'] = stderr
            cmd['exit_code'] = exit_code
            cmd['status'] = 'completed'
            cmd['completed_at'] = datetime.now().isoformat()
            
            logger.info(f"Получен результат выполнения команды {command_id} от клиента {client_id}")
            break
    else:
        logger.warning(f"Команда {command_id} не найдена для клиента {client_id}")
    
    return jsonify({'success': True})

@app.route('/api/command-status/<client_id>')
@require_teacher_auth
def command_status(client_id):
    if client_id not in clients:
        return jsonify({"error": "Клиент не найден"}), 404
        
    client = clients[client_id]
    commands = client.get('commands', [])
    
    return jsonify({
        'client_id': client_id,
        'commands': commands
    })

@app.route('/api/command-details/<client_id>/<command_id>')
@require_teacher_auth
def command_details(client_id, command_id):
    if client_id not in clients:
        return jsonify({"error": "Клиент не найден"}), 404
        
    client = clients[client_id]
    for cmd in client.get('commands', []):
        if cmd.get('id') == command_id:
            return jsonify({
                'command': cmd
            })
    
    return jsonify({"error": "Команда не найдена"}), 404

@app.route('/stream/<client_id>')
@require_teacher_auth
def stream_client(client_id):
    if client_id not in clients:
        flash('Клиент не найден')
        logger.warning(f"Попытка доступа к несуществующему клиенту {client_id}")
        return redirect(url_for('dashboard'))
    
    if client_id not in ffmpeg_processes:
        flash('Стрим не настроен')
        logger.warning(f"Стрим не настроен для клиента {client_id}")
        return redirect(url_for('dashboard'))
    
    hls_path = ffmpeg_processes[client_id].get('hls_path', '')
    if not hls_path:
        flash('Стрим не настроен')
        logger.warning(f"Путь к HLS не указан для клиента {client_id}")
        return redirect(url_for('dashboard'))
    
    playlist_url = f"/hls/{client_id}/playlist.m3u8"
    
    playlist_file = os.path.join(hls_path, "playlist.m3u8")
    if not os.path.exists(playlist_file):
        logger.warning(f"Файл плейлиста не существует: {playlist_file}")
    else:
        logger.info(f"Плейлист существует: {playlist_file}, размер: {os.path.getsize(playlist_file)} байт")
    
    segment_files = glob.glob(os.path.join(hls_path, "segment_*.ts"))
    logger.info(f"Найдено {len(segment_files)} сегментов в {hls_path}")
    
    diagnostic_info = {
        'client_id': client_id,
        'stream_url': clients[client_id].get('stream_info', {}).get('url', 'Не указан'),
        'playlist_exists': os.path.exists(playlist_file),
        'segment_count': len(segment_files),
        'proxy_start_time': ffmpeg_processes[client_id].get('start_time', 'Не указано'),
        'playlist_url': playlist_url
    }
    
    return render_template('stream.html', 
                           client_id=client_id, 
                           playlist_url=playlist_url, 
                           diagnostic_info=diagnostic_info)

@app.route('/diagnostic/<client_id>')
@require_teacher_auth
def diagnostic(client_id):
    if client_id not in clients:
        return jsonify({'error': 'Client not found'}), 404
    
    result = {
        'client': {
            'id': client_id,
            'last_seen': clients[client_id].get('last_seen', datetime.min).isoformat(),
            'stream_info': clients[client_id].get('stream_info')
        },
        'ffmpeg': {}
    }
    
    if client_id in ffmpeg_processes:
        process_info = ffmpeg_processes[client_id]
        hls_path = process_info.get('hls_path', '')
        result['ffmpeg'] = {
            'proxy_port': process_info.get('proxy_port'),
            'hls_path': hls_path,
            'start_time': process_info.get('start_time'),
            'process_running': process_info.get('process').poll() is None
        }
        
        if hls_path:
            playlist_file = os.path.join(hls_path, "playlist.m3u8")
            result['hls'] = {
                'playlist_exists': os.path.exists(playlist_file),
                'segments': []
            }
            
            if os.path.exists(playlist_file):
                result['hls']['playlist_size'] = os.path.getsize(playlist_file)
                try:
                    with open(playlist_file, 'r') as f:
                        result['hls']['playlist_content'] = f.read()
                except Exception as e:
                    result['hls']['playlist_error'] = str(e)
            
            segment_files = glob.glob(os.path.join(hls_path, "segment_*.ts"))
            for segment in segment_files:
                result['hls']['segments'].append({
                    'name': os.path.basename(segment),
                    'size': os.path.getsize(segment),
                    'mtime': datetime.fromtimestamp(os.path.getmtime(segment)).isoformat()
                })
    
    return jsonify(result)

os.makedirs(os.path.join('static', 'streams'), exist_ok=True)

@app.route('/hls/<client_id>/<path:filename>')
def serve_hls(client_id, filename):
    file_path = os.path.join('static', 'streams', client_id, filename)
    
    if not os.path.exists(file_path):
        logger.warning(f"Запрошенный HLS файл не найден: {file_path}")
        return "File not found", 404
        
    logger.debug(f"Отправка HLS файла: {file_path}, размер: {os.path.getsize(file_path)}")
    
    if filename.endswith('.m3u8'):
        mimetype = 'application/vnd.apple.mpegurl'
        
        with open(file_path, 'r') as f:
            content = f.read()
            
        modified_content = content
        for line in content.split('\n'):
            if line.endswith('.ts') and not line.startswith('http') and not line.startswith('/'):
                segment_name = line.strip()
                modified_content = modified_content.replace(
                    segment_name, 
                    f"/hls/{client_id}/{segment_name}"
                )
        
        response = Response(modified_content, mimetype=mimetype)
    elif filename.endswith('.ts'):
        mimetype = 'video/mp2t'
        response = send_file(file_path, mimetype=mimetype)
    else:
        mimetype = 'application/octet-stream'
        response = send_file(file_path, mimetype=mimetype)
    
    response.headers['Access-Control-Allow-Origin'] = '*'
    response.headers['Access-Control-Allow-Methods'] = 'GET, OPTIONS'
    response.headers['Access-Control-Allow-Headers'] = 'Content-Type'
    
    if filename.endswith('.m3u8'):
        response.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
    else:
        response.headers['Cache-Control'] = 'public, max-age=3600'
        
    return response

@app.route('/api/send-notification/<client_id>', methods=['POST'])
@require_teacher_auth
def send_notification(client_id):
    try:
        data = request.get_json()
        message = data.get('message')
        
        if not message:
            return jsonify({'success': False, 'error': 'Missing message'})
        
        if client_id not in clients:
            return jsonify({'success': False, 'error': 'Client not found'})
        
        if 'notifications' not in clients[client_id]:
            clients[client_id]['notifications'] = []
        
        clients[client_id]['notifications'].append({
            'id': str(uuid.uuid4()),
            'message': message,
            'timestamp': datetime.now().isoformat()
        })
        
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})

@app.route('/api/check-notifications/<client_id>')
def check_notifications(client_id):
    try:
        token = request.args.get('token')
        if not token or client_id not in clients or clients[client_id].get('token') != token:
            return jsonify({'success': False, 'error': 'Invalid token'}), 401
            
        since_str = request.args.get('since', None)
        
        if not since_str:
            since_time = datetime.min
        else:
            try:
                since_time = datetime.fromisoformat(since_str)
            except ValueError:
                since_time = datetime.now() - timedelta(minutes=5)
        
        notifications = clients[client_id].get('notifications', [])
        
        new_notifications = []
        for notification in notifications:
            try:
                notification_time = datetime.fromisoformat(notification['timestamp'])
                if notification_time > since_time:
                    new_notifications.append(notification)
            except (ValueError, KeyError):
                pass
        
        if new_notifications:
            clients[client_id]['notifications'] = [
                n for n in clients[client_id].get('notifications', []) 
                if n not in new_notifications
            ]
            
        return jsonify({'success': True, 'notifications': new_notifications})
    except Exception as e:
        logger.error(f"Ошибка при получении уведомлений: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/heartbeat/<client_id>', methods=['POST'])
def heartbeat(client_id):
    try:
        if client_id not in clients:
            return jsonify({'success': False, 'error': 'Client not found'}), 404
        
        clients[client_id]['last_seen'] = datetime.now()
        
        system_info = request.json
        if system_info:
            if 'system_info' not in clients[client_id]:
                clients[client_id]['system_info'] = {}
            
            clients[client_id]['system_info'].update(system_info)
        
        return jsonify({'success': True})
    except Exception as e:
        logger.error(f"Ошибка при обработке heartbeat: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/update-screen-info/<client_id>', methods=['POST'])
def update_screen_info(client_id):
    try:
        if client_id not in clients:
            return jsonify({'success': False, 'error': 'Client not found'}), 404
        
        screen_info = request.json
        if screen_info:
            if 'screen_info' not in clients[client_id]:
                clients[client_id]['screen_info'] = {}
            
            clients[client_id]['screen_info'].update(screen_info)
        
        return jsonify({'success': True})
    except Exception as e:
        logger.error(f"Ошибка при обновлении информации об экране: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/get-processes/<client_id>', methods=['GET'])
@require_teacher_auth
def get_processes(client_id):
    if client_id not in clients:
        return jsonify({'success': False, 'error': 'Client not found'}), 404
    
    command_id = str(uuid.uuid4())
    
    if 'commands' not in clients[client_id]:
        clients[client_id]['commands'] = []
    
    command = {
        'id': command_id,
        'command': "tasklist /FO CSV",  
        'timestamp': datetime.now().isoformat(),
        'status': 'pending',
        'type': 'get_processes'  
    }
    
    clients[client_id]['commands'].append(command)
    
    return jsonify({
        'success': True, 
        'command_id': command_id,
        'message': 'Команда получения списка процессов отправлена'
    })

@app.route('/api/kill-process/<client_id>/<process_id>', methods=['POST'])
@require_teacher_auth
def kill_process(client_id, process_id):
    if client_id not in clients:
        return jsonify({'success': False, 'error': 'Client not found'}), 404
    
    try:
        pid = int(process_id)
    except ValueError:
        return jsonify({'success': False, 'error': 'Invalid process ID'}), 400
    
    command_id = str(uuid.uuid4())
    
    if 'commands' not in clients[client_id]:
        clients[client_id]['commands'] = []
    
    command = {
        'id': command_id,
        'command': f"taskkill /F /PID {pid}",  
        'timestamp': datetime.now().isoformat(),
        'status': 'pending',
        'type': 'kill_process'  
    }
    
    clients[client_id]['commands'].append(command)
    
    return jsonify({
        'success': True, 
        'command_id': command_id,
        'message': f'Команда завершения процесса {pid} отправлена'
    })

if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0') 