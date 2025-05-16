import os
import time
import json
import subprocess
import requests
import argparse
import socket
import threading
from datetime import datetime
import tempfile
from ctypes import windll, Structure, c_long, byref
import platform
import logging
import psutil
from win10toast import ToastNotifier


local_server = "http://192.168.0.101:5000"

#  логирование
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler('client.log')
    ]
)



class RECT(Structure):
    _fields_ = [
        ('left', c_long),
        ('top', c_long),
        ('right', c_long),
        ('bottom', c_long)
    ]

def get_screen_resolution():
    """Определяет разрешение основного экрана через Windows API."""
    try:
        hdesktop = windll.user32.GetDesktopWindow()
        rect = RECT()
        windll.user32.GetWindowRect(hdesktop, byref(rect))
        
        width = rect.right - rect.left
        height = rect.bottom - rect.top
        
        return f"{width}x{height}"
    except Exception as e:
        print(f"Ошибка при определении разрешения экрана: {e}")
        return "1920x1080"


API_URL = os.environ.get('API_URL', local_server)
POLLING_INTERVAL = 5
DEFAULT_CONFIG_NAME = "default"
CONFIG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'configs')

DEFAULT_STREAM_PORT = 8090

os.makedirs(CONFIG_DIR, exist_ok=True)

class StudentClient:
    def __init__(self, config_name=DEFAULT_CONFIG_NAME):
        self.client_id = None
        self.token = None
        self.config_name = config_name
        self.config_file = self._get_config_path()
        self.load_credentials()
        self.ffmpeg_process = None
        self.stream_port = DEFAULT_STREAM_PORT
        
    def _get_config_path(self):
        """Получает путь к файлу конфигурации."""
        if os.path.exists('client_credentials.json') and self.config_name == DEFAULT_CONFIG_NAME:
            return 'client_credentials.json'
        
        return os.path.join(CONFIG_DIR, f"{self.config_name}.json")
        
    def load_credentials(self):
        """Загрузить учетные данные из файла, если они существуют."""
        try:
            if os.path.exists(self.config_file):
                with open(self.config_file, 'r') as f:
                    creds = json.load(f)
                    self.client_id = creds.get('client_id')
                    self.token = creds.get('token')
                    print(f"Загружены существующие учетные данные для client_id: {self.client_id}")
                    print(f"Из файла конфигурации: {self.config_file}")
            else:
                print(f"Файл конфигурации {self.config_file} не найден. Будет создан новый.")
        except (FileNotFoundError, json.JSONDecodeError) as e:
            print(f"Ошибка чтения конфигурации: {e}")
            print("Будет создан новый конфигурационный файл.")
    
    def register(self):
        """Register with the server to get client_id and token."""
        if self.client_id and self.token:
            print("Уже зарегистрирован.")
            return
            
        try:
            response = requests.post(f"{API_URL}/api/register")
            if response.status_code == 200:
                data = response.json()
                self.client_id = data['client_id']
                self.token = data['token']
                self._save_config()
                
                print(f"Успешно зарегистрирован с client_id: {self.client_id}")
                print(f"Конфигурация сохранена в: {self.config_file}")
            else:
                print(f"Ошибка регистрации: {response.status_code} {response.text}")
        except Exception as e:
            print(f"Ошибка при регистрации: {e}")
    
    def _save_config(self):
        """Сохраняет конфигурацию в файл."""
        try:
            config_data = {
                'client_id': self.client_id,
                'token': self.token,
                'config_name': self.config_name,
                'created_at': datetime.now().isoformat(),
                'api_url': API_URL
            }
            
            os.makedirs(os.path.dirname(self.config_file), exist_ok=True)
            
            with open(self.config_file, 'w') as f:
                json.dump(config_data, f, indent=4)
            
            print(f"Конфигурация сохранена в: {self.config_file}")
            return True
        except Exception as e:
            print(f"Ошибка при сохранении конфигурации: {e}")
            return False
    
    def poll_commands(self):
        """Опрашиваем команды от сервера."""
        if not self.client_id or not self.token:
            print("Не зарегистрирован. Невозможно получить команды.")
            return []
            
        try:
            url = f"{API_URL}/api/commands/{self.client_id}?token={self.token}"
            response = requests.get(url)
            
            if response.status_code == 200:
                data = response.json()
                return data.get('commands', [])
            else:
                print(f"Ошибка при получении команд: {response.status_code} {response.text}")
                return []
        except Exception as e:
            print(f"Ошибка при опросе команд: {e}")
            return []
    
    def acknowledge_commands(self):
        """Подтверждение что все команды обработаны."""
        if not self.client_id or not self.token:
            print("Не зарегистрирован. Невозможно подтвердить команды.")
            return
            
        try:
            url = f"{API_URL}/api/commands/{self.client_id}/ack?token={self.token}"
            response = requests.post(
                url, 
                json={'command_ids': []},
                headers={'Content-Type': 'application/json'}
            )
            
            if response.status_code == 200:
                print("Все команды успешно подтверждены")
            else:
                print(f"Ошибка подтверждения команд: {response.status_code} {response.text}")
        except Exception as e:
            print(f"Ошибка при подтверждении команд: {e}")
    
    def execute_command(self, command):
        """Выполнение команды, полученной с сервера."""
        print(f"Выполнение команды: {command}")
        
        cmd_text = command.get('command', '')
        command_id = command.get('id', '')
        command_type = command.get('type', '')
        
        if command_type == 'get_processes':
            stdout = self._get_process_list_custom()
            stderr = ""
            exit_code = 0
            print(f"Получен список процессов (своя реализация)")
            self.send_command_result(command_id, stdout, stderr, exit_code)
            return
        
        try:
            timeout_value = 30 if 'tasklist' in cmd_text else 10
            
            result = subprocess.run(
                cmd_text, 
                shell=True, 
                capture_output=True, 
                encoding='cp866',
                errors='replace',
                timeout=timeout_value
            )
            stdout = result.stdout
            stderr = result.stderr
            exit_code = result.returncode
            
            print(f"Вывод команды: {stdout}")
            if stderr:
                print(f"Ошибка команды: {stderr}")
            
            self.send_command_result(command_id, stdout, stderr, exit_code)
            
        except subprocess.TimeoutExpired:
            error_msg = "Время выполнения команды истекло"
            print(error_msg)
            self.send_command_result(command_id, "", error_msg, -1)
        except Exception as e:
            error_msg = f"Ошибка выполнения команды: {e}"
            print(error_msg)
            self.send_command_result(command_id, "", error_msg, -2)
    
    def _format_process_list(self, csv_output):
        """Форматирует вывод tasklist в CSV с правильными заголовками для веб-интерфейса."""
        if not csv_output:
            return "Нет запущенных процессов"
            
        try:
            header = '"Имя образа","PID","Имя сеанса","№ сеанса","Память","Состояние","Имя пользователя","Время ЦП","Заголовок окна"\n'
            
            if '/NH' in csv_output:
                csv_with_header = header + csv_output
            else:
                csv_with_header = csv_output
            
            lines = csv_with_header.strip().split('\n')
            processed_lines = [lines[0]]
            
            for i in range(1, len(lines)):
                line = lines[i].strip()
                if not line:
                    continue
                    
                comma_count = 0
                in_quotes = False
                for char in line:
                    if char == '"':
                        in_quotes = not in_quotes
                    elif char == ',' and not in_quotes:
                        comma_count += 1
                
                # Должно быть 8 запятых (9 полей)
                expected_commas = 8
                if comma_count < expected_commas:
                    # Добавляем недостающие поля
                    missing_fields = expected_commas - comma_count
                    line = line.rstrip('",') + '","","","' + '"' * (missing_fields - 2)
                
                processed_lines.append(line)
            
            return '\n'.join(processed_lines)
            
        except Exception as e:
            print(f"Ошибка при форматировании списка процессов: {e}")
            return header + csv_output
    
    def send_command_result(self, command_id, stdout, stderr, exit_code):
        """Отправляет результат выполнения команды на сервер."""
        if not self.client_id or not self.token:
            print("Не зарегистрирован. Невозможно отправить результат команды.")
            return False
        
        try:
            url = f"{API_URL}/api/command-result/{self.client_id}?token={self.token}"
            
            max_size = 1024 * 1024  # 1MB
            if len(stdout) > max_size:
                print(f"Результат команды слишком большой ({len(stdout)} байт), обрезаем до {max_size} байт")
                stdout = stdout[:max_size] + "\n... (результат обрезан, слишком большой вывод)"
            
            response = requests.post(
                url,
                json={
                    'command_id': command_id,
                    'stdout': stdout,
                    'stderr': stderr,
                    'exit_code': exit_code
                },
                headers={'Content-Type': 'application/json'},
                timeout=30
            )
            
            if response.status_code == 200:
                print(f"Результат команды успешно отправлен")
                return True
            else:
                print(f"Ошибка отправки результата команды: {response.status_code} {response.text}")
                return False
        except Exception as e:
            print(f"Ошибка при отправке результата команды: {e}")
            return False
    
    def register_stream_with_server(self):
        """Регистрирует информацию о стриме на сервере."""
        if not self.client_id or not self.token:
            print("Не зарегистрирован. Невозможно зарегистрировать стрим.")
            return False
            
        try:
            hostname = socket.gethostname()
            local_ip = socket.gethostbyname(hostname)
            
            stream_data = {
                "stream_type": "ffmpeg_tcp",
                "stream_url": f"tcp://{local_ip}:{self.stream_port}",
                "client_id": self.client_id
            }
            
            print(f"Регистрация стрима: {stream_data}")
            url = f"{API_URL}/api/register-stream/{self.client_id}?token={self.token}"
            
            response = requests.post(url, json=stream_data, timeout=30)
            
            if response.status_code == 200:
                print(f"Стрим успешно зарегистрирован на сервере: tcp://{local_ip}:{self.stream_port}")
                return True
            else:
                print(f"Ошибка регистрации стрима: {response.status_code} {response.text}")
                return False
        except Exception as e:
            print(f"Ошибка при регистрации стрима: {e}")
            return False
    
    def start_ffmpeg_stream(self):
        """Запуск FFmpeg для стриминга экрана по TCP."""
        if self.ffmpeg_process and self.ffmpeg_process.poll() is None:
            print("FFmpeg уже запущен")
            return True
            
        try:
            ffmpeg_version = subprocess.run(['ffmpeg', '-version'], capture_output=True, text=True)
            print(f"FFmpeg версия: {ffmpeg_version.stdout.split(os.linesep)[0]}")
        except FileNotFoundError:
            print("FFmpeg не найден в системе! Установите FFmpeg для стриминга.")
            return False
            
        try:
            resolution = get_screen_resolution()
            print(f"Определено разрешение экрана: {resolution}")
            
            temp_dir = os.path.join(tempfile.gettempdir(), "ffmpeg_stream")
            os.makedirs(temp_dir, exist_ok=True)
            
            log_file = os.path.join(temp_dir, f"ffmpeg_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log")
            
            print(f"Логи FFmpeg будут записаны в: {log_file}")
            
            # Формируем команду FFmpeg для стриминга экрана с оптимизацией для снижения задержки
            cmd = [
                'ffmpeg',
                '-f', 'gdigrab',                   # Используем gdigrab для Windows
                '-framerate', '15',                # Повышаем частоту кадров для более плавного видео
                '-video_size', resolution,         # Используем определенное разрешение экрана
                '-i', 'desktop',                   # Захватываем весь рабочий стол
                '-vcodec', 'libx264',              # Кодек H.264
                '-preset', 'ultrafast',            # Самый быстрый пресет для минимальной задержки
                '-tune', 'zerolatency',            # Минимальная задержка
                '-pix_fmt', 'yuv420p',             # Формат пикселей, совместимый с большинством плееров
                '-r', '15',                        # Частота кадров выходного потока
                '-g', '15',                        # Группа кадров (I-frame каждые 15 кадров)
                '-keyint_min', '15',               # Минимальное расстояние между ключевыми кадрами
                '-sc_threshold', '0',              # Отключаем обнаружение смены сцены для более стабильного потока
                '-b:v', '1500k',                   # Повышаем битрейт для лучшего качества
                '-maxrate', '1500k',               # Максимальный битрейт
                '-bufsize', '500k',                # Меньший буфер для снижения задержки
                '-f', 'mpegts',                    # Формат выходного потока - mpegts
                '-flush_packets', '1',             # Сразу же отправлять пакеты
                '-loglevel', 'info',               # Повышаем уровень логирования
                f'tcp://0.0.0.0:{self.stream_port}?listen'  # Слушаем на всех интерфейсах
            ]
            
            print(f"Запуск FFmpeg стрима на порту {self.stream_port}...")
            print(f"Команда: {' '.join(cmd)}")
            
            log_fd = open(log_file, 'w')
            
            self.ffmpeg_process = subprocess.Popen(
                cmd, 
                stdout=subprocess.PIPE, 
                stderr=subprocess.PIPE,
                bufsize=10**8
            )
            
            if os.name == 'nt':
                try:
                    windll.kernel32.SetPriorityClass(int(self.ffmpeg_process._handle), 0x00008000)
                    print("Установлен приоритет процесса FFmpeg: ВЫСОКИЙ")
                except Exception as e:
                    print(f"Не удалось установить приоритет процесса: {e}")
            
            # Запускаем потоки для чтения вывода FFmpeg (чтобы не блокировать основной поток)
            threading.Thread(target=self._read_ffmpeg_output, args=(self.ffmpeg_process.stdout, log_fd, "stdout"), daemon=True).start()
            threading.Thread(target=self._read_ffmpeg_output, args=(self.ffmpeg_process.stderr, log_fd, "stderr"), daemon=True).start()
            
            # Небольшая задержка, чтобы FFmpeg успел запуститься
            time.sleep(3)
            
            # Проверяем, что процесс не завершился с ошибкой
            if self.ffmpeg_process.poll() is not None:
                print(f"FFmpeg завершился с кодом {self.ffmpeg_process.returncode}")
                print(f"Проверьте лог файл: {log_file}")
                return False
                
            print("FFmpeg запущен успешно и ожидает подключение")
            
            # Регистрируем стрим на сервере
            register_success = self.register_stream_with_server()
            
            if not register_success:
                print("Ошибка при регистрации стрима, пробуем еще раз через 3 секунды...")
                time.sleep(3)
                register_success = self.register_stream_with_server()
                
            return register_success
            
        except Exception as e:
            print(f"Ошибка при запуске FFmpeg: {e}")
            return False
    
    def _read_ffmpeg_output(self, pipe, log_file=None, source="unknown"):
        """Чтение вывода FFmpeg в отдельном потоке."""
        for line in iter(pipe.readline, b''):
            try:
                line_text = line.decode('utf-8').strip()
                if line_text:
                    if log_file:
                        # Записываем в лог с отметкой времени
                        timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S.%f')
                        log_file.write(f"[{timestamp}] [{source}] {line_text}\n")
                        log_file.flush()
                    
                    if "error" in line_text.lower():
                        print(f"FFmpeg ERROR: {line_text}")
                    elif "warning" in line_text.lower():
                        print(f"FFmpeg WARNING: {line_text}")
                    else:
                        print(f"FFmpeg: {line_text}")
            except Exception as e:
                print(f"Ошибка при обработке вывода FFmpeg: {e}")
    
    def stop_ffmpeg_stream(self):
        """Остановка FFmpeg стрима."""
        if self.ffmpeg_process and self.ffmpeg_process.poll() is None:
            print("Остановка FFmpeg стрима...")
            try:
                self.ffmpeg_process.terminate()
                time.sleep(0.5)
                if self.ffmpeg_process.poll() is None:
                    self.ffmpeg_process.kill()
                    time.sleep(0.5)
                
                if self.ffmpeg_process.poll() is None:
                    print("Не удалось остановить процесс FFmpeg, он может остаться в памяти")
                else:
                    print(f"FFmpeg стрим остановлен, код возврата: {self.ffmpeg_process.returncode}")
            except Exception as e:
                print(f"Ошибка при остановке FFmpeg: {e}")
    
    def run(self):
        """Main client loop."""
        if not self.client_id:
            print("Регистрация не удалась. Выход.")
            return
        
        print(f"Клиент запущен с конфигурацией: {self.config_name}")
        print(f"ID клиента: {self.client_id}")
        
        ffmpeg_success = self.start_ffmpeg_stream()
        if not ffmpeg_success:
            print("Не удалось запустить FFmpeg стрим. Пробуем еще раз через 5 секунд...")
            time.sleep(5)
            ffmpeg_success = self.start_ffmpeg_stream()
            if not ffmpeg_success:
                print("Повторная попытка запуска стрима не удалась. Проверьте настройки FFmpeg.")
        
        print(f"Опрос команд каждые {POLLING_INTERVAL} секунд...")
        
        last_command_poll_time = 0
        last_ffmpeg_check_time = 0
        ffmpeg_restart_attempts = 0
        
        try:
            while True:
                current_time = time.time()
                
                if current_time - last_command_poll_time >= POLLING_INTERVAL:
                    try:
                        commands = self.poll_commands()
                        if commands:
                            print(f"Получено {len(commands)} команд(ы)")
                            for command in commands:
                                self.execute_command(command)
                            self.acknowledge_commands()
                    except Exception as e:
                        print(f"Ошибка при опросе или выполнении команд: {e}")
                    last_command_poll_time = current_time
                
                if current_time - last_ffmpeg_check_time >= 10:
                    if self.ffmpeg_process and self.ffmpeg_process.poll() is not None:
                        print(f"FFmpeg завершился с кодом {self.ffmpeg_process.returncode}. Перезапуск...")
                        
                        if ffmpeg_restart_attempts < 5:
                            ffmpeg_restart_attempts += 1
                            restart_delay = 5 * ffmpeg_restart_attempts
                            print(f"Попытка перезапуска {ffmpeg_restart_attempts}/5 через {restart_delay} секунд")
                            time.sleep(restart_delay)
                            
                            ffmpeg_success = self.start_ffmpeg_stream()
                            if ffmpeg_success:
                                print("FFmpeg успешно перезапущен")
                                ffmpeg_restart_attempts = 0
                        else:
                            print("Достигнуто максимальное количество попыток перезапуска FFmpeg.")
                            print("Проверьте настройки и перезапустите клиент вручную.")
                            if ffmpeg_restart_attempts > 0 and current_time - last_ffmpeg_check_time >= 300:
                                ffmpeg_restart_attempts = 0
                    else:
                        ffmpeg_restart_attempts = 0
                    
                    last_ffmpeg_check_time = current_time
                
                time.sleep(0.5)
                    
        except KeyboardInterrupt:
            print("\nЗавершение работы клиента...")
            self.stop_ffmpeg_stream()

    def _get_process_list_custom(self):
        """Получает список процессов через psutil и форматирует в CSV для веб-интерфейса."""
        try:
            process_list = []
            
            header = '"Имя образа","PID","Имя сеанса","№ сеанса","Память","Состояние","Имя пользователя","Время ЦП","Заголовок окна"'
            process_list.append(header)
            
            for proc in psutil.process_iter(['pid', 'name', 'username', 'memory_info', 'cpu_percent']):
                try:
                    # Базовая информация
                    pid = proc.info['pid']
                    name = proc.info['name']
                    
                    try:
                        memory_mb = round(proc.info['memory_info'].rss / (1024 * 1024))
                        memory = f"{memory_mb:,} КБ".replace(',', ' ')
                    except:
                        memory = "Н/Д"
                        
                    username = proc.info.get('username', 'Н/Д')
                    cpu_percent = f"{proc.info.get('cpu_percent', 0):.1f}%"
                    
                    window_title = ""
                    
                    process_line = f'"{name}","{pid}","Console","8","{memory}","Running","{username}","{cpu_percent}","{window_title}"'
                    process_list.append(process_line)
                except Exception as e:
                    print(f"Ошибка получения информации о процессе {pid}: {e}")
            
            return "\n".join(process_list)
            
        except Exception as e:
            print(f"Ошибка при получении списка процессов: {e}")
            return '"Имя образа","PID","Имя сеанса","№ сеанса","Память","Состояние","Имя пользователя","Время ЦП","Заголовок окна"\n"Error","0","None","0","0 КБ","Error","None","0%","Ошибка получения списка процессов"'

def list_configs():
    """Выводит список доступных конфигураций."""
    print("Доступные конфигурации:")
    
    if os.path.exists('client_credentials.json'):
        print("- default (старый формат)")
    
    if os.path.exists(CONFIG_DIR):
        configs = [f.replace('.json', '') for f in os.listdir(CONFIG_DIR) if f.endswith('.json')]
        if configs:
            for config in configs:
                print(f"- {config}")
        else:
            print("Конфигураций не найдено в директории configs/")
    else:
        print("Директория configs/ не существует. Будет создана при первом запуске.")

def main(args):
    if args.list:
        list_configs()
        exit(0)
    
    if args.new:
        config_path = os.path.join(CONFIG_DIR, f"{args.config}.json")
        if os.path.exists(config_path):
            os.remove(config_path)
            print(f"Удален существующий конфиг: {config_path}")
        elif args.config == DEFAULT_CONFIG_NAME and os.path.exists('client_credentials.json'):
            os.remove('client_credentials.json')
            print("Удален старый формат конфигурации.")
    
    client = StudentClient(args.config)
    client.stream_port = args.port
    
    if not client.client_id or not client.token:
        client.register()
    
    if not client.client_id or not client.token:
        logging.error("Не удалось получить ID и токен клиента. Проверьте подключение к серверу.")
        return
    
    logging.info(f"Клиент запущен с ID: {client.client_id}")
    
    toast = ToastNotifier()
    
    threading.Thread(target=screenshot_thread, args=(API_URL, client.client_id, client.ffmpeg_process, args.quality, args.fps), daemon=True).start()
    threading.Thread(target=heartbeat_thread, args=(API_URL, client.client_id), daemon=True).start()
    threading.Thread(target=command_thread, args=(API_URL, client.client_id), daemon=True).start()
    threading.Thread(target=notification_thread, args=(API_URL, client.client_id, toast), daemon=True).start()
    
    client.run()

def screenshot_thread(server_url, client_id, ffmpeg_process, quality, fps):
    """Поток для отправки скриншотов экрана"""
    while True:
        try:
            if ffmpeg_process and ffmpeg_process.poll() is not None:
                logging.warning(f"FFmpeg процесс завершился с кодом {ffmpeg_process.returncode}, перезапуск...")
            

            screen_info = {
                'resolution': get_screen_resolution(),
                'quality': quality,
                'fps': fps
            }
            
            requests.post(f"{server_url}/api/update-screen-info/{client_id}",
                          json=screen_info)
        except Exception as e:
            logging.error(f"Ошибка в потоке скриншотов: {e}")
        
        time.sleep(5)

def heartbeat_thread(server_url, client_id):
    """Поток для отправки heartbeat сигналов на сервер"""
    while True:
        try:
            system_info = {
                'os': platform.system() + ' ' + platform.release(),
                'hostname': platform.node(),
                'cpu_percent': psutil.cpu_percent(),
                'memory_percent': psutil.virtual_memory().percent,
                'timestamp': datetime.now().isoformat()
            }
            

            response = requests.post(f"{server_url}/api/heartbeat/{client_id}", 
                                    json=system_info)
            
            if response.status_code != 200:
                logging.warning(f"Heartbeat вернул код {response.status_code}")
                
        except Exception as e:
            logging.error(f"Ошибка в потоке heartbeat: {e}")
            
        time.sleep(5)

def command_thread(server_url, client_id):
    """Поток для получения и выполнения команд от сервера"""
    while True:
        try:
            # Запрашиваем новые команды
            response = requests.get(f"{server_url}/api/get-commands/{client_id}")
            
            if response.status_code == 200:
                commands = response.json().get('commands', [])
                
                for cmd in commands:
                    cmd_id = cmd.get('id')
                    cmd_text = cmd.get('command')
                    
                    if cmd_text:
                        logging.info(f"Выполнение команды: {cmd_text}")
                        
                        try:
                            result = subprocess.run(
                                cmd_text, 
                                shell=True, 
                                capture_output=True,
                                text=True,
                                timeout=30
                            )
                            
                            response_data = {
                                'stdout': result.stdout,
                                'stderr': result.stderr,
                                'exit_code': result.returncode
                            }
                            
                            requests.post(
                                f"{server_url}/api/command-result/{client_id}/{cmd_id}",
                                json=response_data
                            )
                            
                        except subprocess.TimeoutExpired:
                            requests.post(
                                f"{server_url}/api/command-result/{client_id}/{cmd_id}",
                                json={
                                    'stdout': '',
                                    'stderr': 'Команда прервалась через 30 секунд',
                                    'exit_code': -1
                                }
                            )
                        except Exception as e:
                            requests.post(
                                f"{server_url}/api/command-result/{client_id}/{cmd_id}",
                                json={
                                    'stdout': '',
                                    'stderr': f'Ошибка при выполнении команды: {str(e)}',
                                    'exit_code': -1
                                }
                            )
                
        except Exception as e:
            logging.error(f"Ошибка в потоке команд: {e}")
        
        time.sleep(1)

def notification_thread(server_url, client_id, toast):
    """Поток для получения и отображения уведомлений"""
    last_check_time = datetime.now()
    
    # Получаем токен клиента
    config_file = os.path.join(CONFIG_DIR, f"{DEFAULT_CONFIG_NAME}.json")
    if os.path.exists(config_file):
        try:
            with open(config_file, 'r') as f:
                config = json.load(f)
                token = config.get('token')
        except:
            logging.error("Не удалось прочитать токен из файла конфигурации")
            token = None
    else:
        logging.error("Файл конфигурации не найден")
        token = None
    
    if not token:
        logging.error("Токен клиента не найден, уведомления не будут работать")
        return
        
    logging.info("Запущен поток получения уведомлений")
    
    while True:
        try:
            response = requests.get(
                f"{server_url}/api/check-notifications/{client_id}",
                params={
                    "since": last_check_time.isoformat(),
                    "token": token
                },
                timeout=5
            )
            
            if response.status_code == 200:
                data = response.json()
                if data.get('success'):
                    notifications = data.get('notifications', [])
                    
                    for notification in notifications:
                        # Отображаем уведомление в Windows
                        try:
                            message = notification.get('message', '')
                            if message:
                                logging.info(f"Показываю уведомление: {message}")
                                
                                toast.show_toast(
                                    title="Сообщение от преподавателя",
                                    msg=message,
                                    duration=5,
                                    icon_path=None,
                                    threaded=True
                                )
                                
                                print(f"\n[УВЕДОМЛЕНИЕ]: {message}\n")
                        except Exception as e:
                            logging.error(f"Ошибка показа уведомления: {e}")
                    
                    if notifications:
                        last_check_time = datetime.now()
                        logging.info(f"Получено {len(notifications)} новых уведомлений")
                else:
                    error = data.get('error', 'Неизвестная ошибка')
                    logging.warning(f"Ошибка при получении уведомлений: {error}")
            elif response.status_code == 401:
                logging.error("Ошибка аутентификации при получении уведомлений")
            else:
                logging.warning(f"Ошибка при получении уведомлений. Код ответа: {response.status_code}")
                
        except requests.RequestException as e:
            logging.error(f"Ошибка соединения в потоке уведомлений: {e}")
        except Exception as e:
            logging.error(f"Неизвестная ошибка в потоке уведомлений: {e}")
        
        time.sleep(2)

if __name__ == "__main__":
    # Парсинг аргументов командной строки
    parser = argparse.ArgumentParser(description='Клиент для системы удаленного управления Учитель-Студент')
    parser.add_argument('--config', '-c', type=str, default=DEFAULT_CONFIG_NAME, 
                        help='Имя конфигурационного файла (без расширения .json)')
    parser.add_argument('--list', '-l', action='store_true', 
                        help='Показать список доступных конфигураций')
    parser.add_argument('--new', '-n', action='store_true',
                        help='Принудительно создать новую конфигурацию, игнорируя существующую')
    parser.add_argument('--port', '-p', type=int, default=DEFAULT_STREAM_PORT,
                        help=f'Порт для FFmpeg TCP стрима (по умолчанию {DEFAULT_STREAM_PORT})')
    parser.add_argument('--quality', '-q', type=int, default=75,
                        help='Качество изображения (по умолчанию 75)')
    parser.add_argument('--fps', '-f', type=int, default=15,
                        help='Частота кадров (по умолчанию 15)')
    
    args = parser.parse_args()
    
    
    main(args) 