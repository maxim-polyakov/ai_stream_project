#!/usr/bin/env python3
"""
🤖 AI Stream с FFmpeg стримингом на YouTube
Версия с OAuth 2.0 для YouTube API
ЕДИНЫЙ ПРОЦЕСС С ПАЙПАМИ ДЛЯ АУДИО
"""

import os
import sys
import json
import random
import asyncio
import threading
import logging
import time
import subprocess
import hashlib
from datetime import datetime, timedelta
from typing import List, Dict, Any, Optional
from flask import Flask, render_template, request, jsonify, redirect, url_for
from flask_socketio import SocketIO, emit
import signal
import shutil
from urllib.parse import urlencode
import queue
import tempfile

# Проверяем импорты
try:
    import openai
    import edge_tts
    import pygame

    from config import Config

    print("✅ Все основные зависимости установлены")
except ImportError as e:
    print(f"❌ Ошибка импорта: {e}")
    print("\n📦 Установите зависимости:")
    print("pip install flask==2.3.0 flask-socketio==5.3.0 eventlet==0.33.0 openai>=1.3.0")
    print("pip install edge-tts>=6.1.9 pygame>=2.5.0 python-dotenv>=1.0.0")
    sys.exit(1)

# Импорты для YouTube OAuth
YOUTUBE_OAUTH_AVAILABLE = False
try:
    from google_auth_oauthlib.flow import Flow
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request
    from googleapiclient.discovery import build
    from googleapiclient.errors import HttpError

    YOUTUBE_OAUTH_AVAILABLE = True
    print("✅ Google OAuth модули установлены")
except ImportError as e:
    print(f"⚠️ Google OAuth модуль не найден: {e}")
    print("Для YouTube трансляций установите:")
    print("pip install google-api-python-client google-auth-httplib2 google-auth-oauthlib")
    YOUTUBE_OAUTH_AVAILABLE = False

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('stream.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# Инициализация Flask и SocketIO
app = Flask(__name__, static_folder='stream_ui', template_folder='stream_ui')
app.config['SECRET_KEY'] = 'ai_stream_secret_key_2024'
socketio = SocketIO(app,
                    cors_allowed_origins="*",
                    async_mode='threading',
                    logger=True,
                    engineio_logger=False,
                    ping_timeout=300,
                    ping_interval=60,
                    max_http_buffer_size=1e8)

# Инициализация OpenAI
if Config.OPENAI_API_KEY:
    from openai import OpenAI

    openai_client = OpenAI(api_key=Config.OPENAI_API_KEY)
else:
    logger.warning("⚠️ OpenAI API ключ не найден. Будут использоваться демо-сообщения.")
    openai_client = None


# ========== YOUTUBE OAUTH API ==========

class YouTubeOAuthStream:
    """Управление YouTube трансляциями через OAuth 2.0"""

    def __init__(self):
        self.youtube = None
        self.broadcast_id = None
        self.stream_id = None
        self.is_live = False
        self.credentials = None
        self.stream_key = None
        self.rtmp_url = None
        self.token_file = 'youtube_token.json'

        # Скоупы для YouTube API
        self.SCOPES = [
            'https://www.googleapis.com/auth/youtube',
            'https://www.googleapis.com/auth/youtube.force-ssl',
            'https://www.googleapis.com/auth/youtube.readonly'
        ]

        # OAuth настройки
        self.CLIENT_SECRETS_FILE = 'client_secrets.json'
        self.REDIRECT_URI = 'http://localhost:5500/oauth2callback'

        # Статистика
        self.metrics = {
            'streams_created': 0,
            'broadcasts_created': 0,
            'errors': []
        }

        logger.info("Инициализация YouTube API с OAuth 2.0")

    def get_auth_url(self) -> Optional[str]:
        """Получение URL для аутентификации через OAuth"""
        try:
            if not os.path.exists(self.CLIENT_SECRETS_FILE):
                logger.error(f"❌ Файл клиента OAuth не найден: {self.CLIENT_SECRETS_FILE}")
                print(f"❌ Файл клиента OAuth не найден: {self.CLIENT_SECRETS_FILE}")
                print("📋 Создайте файл client_secrets.json:")
                print("1. Перейдите в Google Cloud Console")
                print("2. Создайте OAuth 2.0 Client ID")
                print("3. Скачайте JSON файл")
                print("4. Сохраните как 'client_secrets.json'")
                return None

            flow = Flow.from_client_secrets_file(
                self.CLIENT_SECRETS_FILE,
                scopes=self.SCOPES,
                redirect_uri=self.REDIRECT_URI
            )

            # Генерируем URL для аутентификации
            auth_url, _ = flow.authorization_url(
                access_type='offline',
                include_granted_scopes='true',
                prompt='consent'  # Всегда запрашиваем согласие
            )

            return auth_url

        except Exception as e:
            logger.error(f"❌ Ошибка получения URL авторизации: {e}")
            return None

    def authenticate_with_code(self, authorization_code: str) -> bool:
        """Аутентификация с помощью authorization code"""
        try:
            flow = Flow.from_client_secrets_file(
                self.CLIENT_SECRETS_FILE,
                scopes=self.SCOPES,
                redirect_uri=self.REDIRECT_URI
            )

            # Получаем токен по коду
            flow.fetch_token(code=authorization_code)
            credentials = flow.credentials

            # Сохраняем токен
            self._save_credentials(credentials)

            # Инициализируем API клиент
            return self._initialize_youtube(credentials)

        except Exception as e:
            logger.error(f"❌ Ошибка аутентификации с кодом: {e}")
            return False

    def load_credentials(self) -> bool:
        """Загрузка сохраненных токенов"""
        try:
            if not os.path.exists(self.token_file):
                return False

            credentials = Credentials.from_authorized_user_file(
                self.token_file,
                self.SCOPES
            )

            # Проверяем, не истек ли токен
            if credentials and credentials.expired and credentials.refresh_token:
                credentials.refresh(Request())

            return self._initialize_youtube(credentials)

        except Exception as e:
            logger.error(f"❌ Ошибка загрузки токенов: {e}")
            return False

    def _initialize_youtube(self, credentials: Credentials) -> bool:
        """Инициализация YouTube API клиента"""
        try:
            self.credentials = credentials
            self.youtube = build(
                'youtube',
                'v3',
                credentials=credentials,
                cache_discovery=False
            )

            # Сохраняем обновленные токены
            self._save_credentials(credentials)

            # Проверяем доступ
            return self.test_api_access()

        except Exception as e:
            logger.error(f"❌ Ошибка инициализации YouTube API: {e}")
            return False

    def _save_credentials(self, credentials: Credentials):
        """Сохранение токенов в файл"""
        try:
            token_data = {
                'token': credentials.token,
                'refresh_token': credentials.refresh_token,
                'token_uri': credentials.token_uri,
                'client_id': credentials.client_id,
                'client_secret': credentials.client_secret,
                'scopes': credentials.scopes
            }

            with open(self.token_file, 'w') as f:
                json.dump(token_data, f)

            logger.info("✅ Токены сохранены")

        except Exception as e:
            logger.error(f"❌ Ошибка сохранения токенов: {e}")

    def test_api_access(self) -> bool:
        """Проверка доступа к YouTube API"""
        try:
            # Простой запрос для проверки доступа
            request = self.youtube.channels().list(
                part="snippet",
                mine=True
            )
            response = request.execute()

            if 'items' in response and len(response['items']) > 0:
                channel_info = response['items'][0]['snippet']
                logger.info(f"✅ Доступ к YouTube API подтвержден")
                logger.info(f"📺 Канал: {channel_info['title']}")
                return True

            return False

        except HttpError as e:
            error_details = json.loads(e.content.decode('utf-8'))
            logger.error(f"❌ Ошибка YouTube API: {error_details}")

            if e.resp.status == 403:
                error_message = error_details.get('error', {}).get('message', '')
                if 'liveStreamingNotEnabled' in error_message:
                    logger.error("❌ YouTube Live Streaming не включен для этого аккаунта!")
                    logger.error("📋 Решение:")
                    logger.error("1. Войдите в YouTube Studio")
                    logger.error("2. Перейдите в 'Контент' → 'Трансляции'")
                    logger.error("3. Активируйте функцию live streaming")
                    logger.error("4. Подтвердите номер телефона")
                    logger.error("5. Подождите 24 часа")

            return False
        except Exception as e:
            logger.error(f"❌ Ошибка проверки доступа: {e}")
            return False

    def create_live_broadcast(
            self,
            title: str,
            description: str = "",
            privacy_status: str = "unlisted",
            scheduled_time: Optional[datetime] = None
    ) -> Optional[Dict[str, Any]]:
        """Создание трансляции"""
        try:
            if not self.youtube:
                logger.error("❌ YouTube API не инициализирован")
                return None

            if not scheduled_time:
                scheduled_time = datetime.now() + timedelta(minutes=2)

            broadcast_body = {
                'snippet': {
                    'title': title,
                    'description': description,
                    'scheduledStartTime': scheduled_time.isoformat()
                },
                'status': {
                    'privacyStatus': privacy_status,
                    'selfDeclaredMadeForKids': False
                },
                'contentDetails': {
                    'enableAutoStart': True,
                    'enableAutoStop': True,
                    'enableEmbed': True,
                    'recordFromStart': True,
                    'enableDvr': True,
                    'enableContentEncryption': False,
                    'enableLowLatency': True,
                    'projection': 'rectangular',
                    'stereoLayout': 'mono'
                }
            }

            request = self.youtube.liveBroadcasts().insert(
                part='snippet,status,contentDetails',
                body=broadcast_body
            )

            response = request.execute()
            self.broadcast_id = response['id']

            logger.info(f"📡 Трансляция создана: {self.broadcast_id}")
            logger.info(f"📺 Заголовок: {title}")
            logger.info(f"🔒 Статус: {privacy_status}")

            self.metrics['broadcasts_created'] += 1

            return response

        except HttpError as e:
            error_details = json.loads(e.content.decode('utf-8'))
            logger.error(f"❌ Ошибка создания трансляции: {error_details}")
            self.metrics['errors'].append(str(error_details))
            return None
        except Exception as e:
            logger.error(f"❌ Ошибка создания трансляции: {e}")
            self.metrics['errors'].append(str(e))
            return None

    def create_stream(
            self,
            title: str = "AI Live Stream",
            resolution: str = "1080p",
            frame_rate: str = "30fps"
    ) -> Optional[Dict[str, Any]]:
        """Создание потока для трансляции"""
        try:
            if not self.youtube:
                logger.error("❌ YouTube API не инициализирован")
                return None

            stream_body = {
                'snippet': {
                    'title': title
                },
                'cdn': {
                    'frameRate': frame_rate,
                    'ingestionType': 'rtmp',
                    'resolution': resolution,
                    'format': ''
                }
            }

            request = self.youtube.liveStreams().insert(
                part='snippet,cdn',
                body=stream_body
            )

            response = request.execute()
            self.stream_id = response['id']

            # Получаем данные для стрима
            stream_key = response['cdn']['ingestionInfo']['streamName']
            ingestion_address = response['cdn']['ingestionInfo']['ingestionAddress']
            self.stream_key = stream_key
            self.rtmp_url = f"rtmp://a.rtmp.youtube.com/live2/{stream_key}"

            logger.info(f"🌊 Поток создан: {self.stream_id}")
            logger.info(f"🔑 Stream Key: {stream_key}")
            logger.info(f"📍 RTMP URL: {self.rtmp_url}")

            self.metrics['streams_created'] += 1

            return {
                'stream_id': self.stream_id,
                'stream_key': stream_key,
                'ingestion_address': ingestion_address,
                'rtmp_url': self.rtmp_url,
                'full_response': response
            }

        except HttpError as e:
            error_details = json.loads(e.content.decode('utf-8'))
            logger.error(f"❌ Ошибка создания потока: {error_details}")
            self.metrics['errors'].append(str(error_details))
            return None
        except Exception as e:
            logger.error(f"❌ Ошибка создания потока: {e}")
            self.metrics['errors'].append(str(e))
            return None

    def bind_broadcast_to_stream(self) -> bool:
        """Привязка трансляции к потоку"""
        try:
            if not self.broadcast_id or not self.stream_id:
                logger.error("❌ Нет broadcast_id или stream_id")
                return False

            request = self.youtube.liveBroadcasts().bind(
                part='id,contentDetails',
                id=self.broadcast_id,
                streamId=self.stream_id
            )

            response = request.execute()
            logger.info("🔗 Трансляция привязана к потоку")
            return True

        except Exception as e:
            logger.error(f"❌ Ошибка привязки: {e}")
            self.metrics['errors'].append(str(e))
            return False

    def start_broadcast(self) -> bool:
        """Начало трансляции"""
        try:
            if not self.broadcast_id:
                logger.error("❌ Нет активной трансляции")
                return False

            # Сначала проверяем текущий статус трансляции
            request = self.youtube.liveBroadcasts().list(
                part='status',
                id=self.broadcast_id
            )
            response = request.execute()

            if 'items' not in response or len(response['items']) == 0:
                logger.error("❌ Трансляция не найдена")
                return False

            current_status = response['items'][0]['status']['lifeCycleStatus']
            logger.info(f"📊 Текущий статус трансляции: {current_status}")

            # Можно переводить в live только из статусов 'ready' или 'testing'
            if current_status not in ['ready', 'testing']:
                logger.error(f"❌ Невозможно начать трансляцию из статуса {current_status}")
                logger.info("ℹ️ Ожидайте 1-2 минуты после создания трансляции")
                return False

            # Теперь переводим в live
            request = self.youtube.liveBroadcasts().transition(
                broadcastStatus='live',
                id=self.broadcast_id,
                part='id,snippet,status'
            )

            response = request.execute()
            self.is_live = True

            logger.info("🎬 ТРАНСЛЯЦИЯ НАЧАЛАСЬ!")
            logger.info(f"📺 Ссылка: https://youtube.com/watch?v={self.broadcast_id}")

            return True

        except HttpError as e:
            error_details = json.loads(e.content.decode('utf-8'))
            logger.error(f"❌ Ошибка начала трансляции: {error_details}")

            if 'invalidTransition' in str(error_details):
                logger.info("📋 Возможные причины:")
                logger.info("1. Трансляция уже идет (status: live)")
                logger.info("2. Трансляция еще не готова (status: created)")
                logger.info("3. Не прошло достаточно времени после создания")
                logger.info("4. Не привязан stream или stream не активен")

            return False
        except Exception as e:
            logger.error(f"❌ Ошибка начала трансляции: {e}")
            return False

    def complete_broadcast(self) -> bool:
        """Завершение трансляции"""
        try:
            if not self.broadcast_id:
                return True

            request = self.youtube.liveBroadcasts().transition(
                broadcastStatus='complete',
                id=self.broadcast_id,
                part='status'
            )

            response = request.execute()
            self.is_live = False

            logger.info("🛑 Трансляция завершена")

            # Очищаем ID
            self.broadcast_id = None
            self.stream_id = None
            self.stream_key = None
            self.rtmp_url = None

            return True

        except Exception as e:
            logger.error(f"❌ Ошибка завершения: {e}")
            self.metrics['errors'].append(str(e))
            return False

    def start_full_stream(
            self,
            title: str,
            description: str = "",
            privacy_status: str = "unlisted",
            resolution: str = "1080p"
    ) -> Optional[Dict[str, Any]]:
        """
        Полный процесс запуска трансляции через OAuth
        """
        try:
            print("\n" + "=" * 70)
            print("🎬 ЗАПУСК YOUTUBE ТРАНСЛЯЦИИ ЧЕРЕЗ OAUTH")
            print("=" * 70)

            # 1. Проверяем аутентификацию
            print("🔧 Шаг 1: Проверка аутентификации...")
            if not self.youtube:
                if not self.load_credentials():
                    print("❌ Не аутентифицирован. Требуется OAuth авторизация")
                    print("ℹ️ Используйте ручной Stream Key или пройдите OAuth авторизацию")
                    return None
            print("✅ Аутентификация успешна")

            # 2. Создание трансляции
            print("🔧 Шаг 2: Создание трансляции...")
            broadcast = self.create_live_broadcast(
                title=title,
                description=description,
                privacy_status=privacy_status
            )

            if not broadcast:
                print("❌ Не удалось создать трансляцию")
                return None
            print(f"✅ Трансляция создана: {self.broadcast_id}")

            # 3. Создание потока
            print("🔧 Шаг 3: Создание потока...")
            stream_info = self.create_stream(
                title=f"Stream for: {title[:50]}",
                resolution=resolution
            )

            if not stream_info:
                print("❌ Не удалось создать поток")
                return None
            print(f"✅ Поток создан: {self.stream_id}")

            # 4. Привязка
            print("🔧 Шаг 4: Привязка трансляции к потоку...")
            if not self.bind_broadcast_to_stream():
                print("❌ Не удалось привязать")
                return None
            print("✅ Трансляция привязана к потоку")

            # 5. ПЕРЕДАЕМ УПРАВЛЕНИЕ FFMPEG
            print("🔧 Шаг 5: Запуск FFmpeg стрима...")
            # Здесь мы НЕ запускаем трансляцию через API
            # Ждем, пока FFmpeg подключится к YouTube

            result = {
                'success': True,
                'broadcast_id': self.broadcast_id,
                'stream_id': self.stream_id,
                'watch_url': f"https://youtube.com/watch?v={self.broadcast_id}",
                'stream_key': stream_info['stream_key'],
                'rtmp_url': stream_info['rtmp_url'],
                'message': "Трансляция создана, запустите FFmpeg для начала стрима. Трансляция автоматически начнется когда YouTube получит поток."
            }

            print("\n" + "=" * 70)
            print("🎬 YOUTUBE ТРАНСЛЯЦИЯ ГОТОВА К ЗАПУСКУ!")
            print("=" * 70)
            print(f"📺 Ссылка: {result['watch_url']}")
            print(f"🔑 Stream Key: {result['stream_key']}")
            print(f"📍 RTMP URL: {result['rtmp_url']}")
            print("\n⚠️  Важно: Трансляция автоматически начнется")
            print("когда YouTube получит видеопоток от FFmpeg.")
            print("Обычно это занимает 30-60 секунд.")
            print("=" * 70)

            return result

        except Exception as e:
            import traceback
            print(f"❌ Ошибка запуска трансляции: {e}")
            traceback.print_exc()
            logger.error(f"❌ Ошибка запуска трансляции: {e}")
            self.metrics['errors'].append(str(e))
            return None

    def get_metrics(self) -> Dict[str, Any]:
        """Получение метрик работы"""
        return {
            **self.metrics,
            'timestamp': datetime.now().isoformat(),
            'is_live': self.is_live,
            'current_broadcast': self.broadcast_id,
            'current_stream': self.stream_id,
            'stream_key': self.stream_key,
            'rtmp_url': self.rtmp_url,
            'authenticated': self.youtube is not None
        }


# ========== FFMPEG STREAM MANAGER с ПАЙПАМИ ==========

class FFmpegPipeStreamManager:
    """Управление FFmpeg стримом на YouTube с пайпами для аудио"""

    def __init__(self):
        self.stream_process = None
        self.is_streaming = False
        self.stream_key = None
        self.rtmp_url = None
        self.ffmpeg_pid = None
        self.audio_pipe = None
        self.audio_queue = queue.Queue()
        self.audio_thread = None
        self.video_thread = None
        self.current_agent = None
        self.last_error = None
        self.stream_start_time = None
        self.audio_counter = 0

        # Создаем папки для временных файлов
        os.makedirs('temp_audio', exist_ok=True)
        os.makedirs('temp_video', exist_ok=True)

    def set_stream_key(self, stream_key: str):
        """Установка ключа стрима"""
        self.stream_key = stream_key
        self.rtmp_url = f"rtmp://a.rtmp.youtube.com/live2/{stream_key}"
        logger.info(f"🔑 Stream Key установлен: {stream_key[:10]}...")
        return True

    def start_stream(self) -> Dict[str, Any]:
        """Запуск FFmpeg стрима с пайпами для аудио"""
        if not self.stream_key:
            logger.error("❌ Stream Key не установлен!")
            return {'success': False, 'error': 'Stream Key не установлен'}

        try:
            self.stream_start_time = time.time()

            # Создаем именованный пайп для аудио
            audio_pipe_path = os.path.join(tempfile.gettempdir(), f'audio_pipe_{int(time.time())}')

            # Удаляем если существует
            if os.path.exists(audio_pipe_path):
                os.remove(audio_pipe_path)

            # Создаем пайп
            os.mkfifo(audio_pipe_path)
            self.audio_pipe = audio_pipe_path
            logger.info(f"🎵 Создан аудио пайп: {audio_pipe_path}")

            # РАБОЧАЯ КОМАНДА FFMPEG ДЛЯ YOUTUBE
            # Используем фильтр lavfi для видео и amovie для чтения из пайпа
            ffmpeg_cmd = [
                'ffmpeg',
                '-re',  # Реальное время для видео
                '-f', 'lavfi',
                '-i', "color=c=0x2d2d2d:s=1920x1080:r=30[bg];"
                      "[bg]drawtext=text='🤖 AI Agents Live Stream':"
                      "fontcolor=white:fontsize=48:x=(w-text_w)/2:y=(h-text_h-200)/2:"
                      "box=1:boxcolor=black@0.5,"
                      "drawtext=textfile=dynamic_text.txt:"
                      "fontcolor=0x4a69ff:fontsize=36:x=(w-text_w)/2:y=(h-text_h+100)/2:"
                      "reload=1:box=1:boxcolor=black@0.3[v]",

                # Аудио из пайпа
                '-f', 's16le',  # Формат сырого PCM аудио
                '-acodec', 'pcm_s16le',
                '-ar', '44100',  # Частота дискретизации
                '-ac', '2',  # Стерео
                '-i', audio_pipe_path,  # Читаем из пайпа

                # Кодеки и настройки
                '-map', '0:v',  # Видео из первого источника
                '-map', '1:a',  # Аудио из второго источника

                # Видео кодирование
                '-c:v', 'libx264',
                '-preset', 'veryfast',
                '-tune', 'zerolatency',
                '-pix_fmt', 'yuv420p',
                '-g', '60',  # Ключевой кадр каждые 60 кадров (2 секунды при 30fps)
                '-b:v', '3000k',  # Битрейт видео
                '-maxrate', '3500k',
                '-bufsize', '6000k',

                # Аудио кодирование
                '-c:a', 'aac',
                '-b:a', '128k',  # Битрейт аудио
                '-ar', '44100',
                '-ac', '2',
                '-strict', 'experimental',

                # Формат вывода
                '-f', 'flv',
                '-flvflags', 'no_duration_filesize',

                # RTMP выход
                self.rtmp_url
            ]

            logger.info(f"🚀 Запуск FFmpeg с пайпом для аудио")

            # Создаем файл для динамического текста
            with open('dynamic_text.txt', 'w', encoding='utf-8') as f:
                f.write('Загрузка...')

            # Логируем команду
            logger.debug(f"FFmpeg команда: {' '.join(ffmpeg_cmd[:10])}...")

            # Запускаем FFmpeg
            self.stream_process = subprocess.Popen(
                ffmpeg_cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                bufsize=10 ** 6,  # Большой буфер для избежания блокировки
                universal_newlines=False
            )

            self.is_streaming = True
            self.ffmpeg_pid = self.stream_process.pid

            # Запускаем тестовое аудио для проверки
            threading.Thread(target=self._send_test_audio, daemon=True).start()

            # Запускаем потоки для обработки аудио
            self._start_audio_handler()
            self._start_monitor_thread()
            self._start_text_updater()

            logger.info(f"🎬 FFmpeg стрим запущен (PID: {self.ffmpeg_pid})")

            # Даем время на запуск
            time.sleep(3)

            return {
                'success': True,
                'pid': self.ffmpeg_pid,
                'stream_key': self.stream_key,
                'rtmp_url': self.rtmp_url,
                'message': 'FFmpeg стрим запущен с пайпом для аудио. Ждите 30-60 секунд для начала трансляции на YouTube.'
            }

        except Exception as e:
            logger.error(f"❌ Ошибка запуска FFmpeg: {e}", exc_info=True)
            return {'success': False, 'error': str(e)}

    def _send_test_audio(self):
        """Отправка тестового аудио для проверки"""
        try:
            # Ждем запуска FFmpeg
            time.sleep(5)

            # Генерируем простое тестовое аудио
            test_text = "Тестовое аудио для проверки стрима. Звук должен быть слышен на трансляции."

            # Создаем временный файл с тестовым аудио
            temp_audio = os.path.join('temp_audio', f'test_{int(time.time())}.mp3')

            # Создаем тестовое аудио с помощью edge-tts
            import edge_tts
            communicate = edge_tts.Communicate(
                text=test_text,
                voice='ru-RU-DmitryNeural'
            )

            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            loop.run_until_complete(communicate.save(temp_audio))

            # Отправляем в очередь
            if os.path.exists(temp_audio):
                self.play_audio(temp_audio)
                logger.info("🔊 Тестовое аудио отправлено")

        except Exception as e:
            logger.warning(f"⚠️ Не удалось отправить тестовое аудио: {e}")

    def _start_text_updater(self):
        """Обновление текста на видео"""

        def updater():
            while self.is_streaming:
                try:
                    current_time = datetime.now().strftime("%H:%M:%S")
                    status = "Трансляция активна" if self.is_streaming else "Пауза"

                    # Обновляем файл с текстом
                    with open('dynamic_text.txt', 'w', encoding='utf-8') as f:
                        f.write(f"Время: {current_time} | {status}")

                    time.sleep(1)
                except:
                    time.sleep(5)

        thread = threading.Thread(target=updater, daemon=True)
        thread.start()

    def _start_audio_handler(self):
        """Запуск обработчика аудио очереди"""

        def audio_handler():
            logger.info("🎵 Запуск обработчика аудио")

            # Открываем пайп для записи
            try:
                with open(self.audio_pipe, 'wb') as pipe_fd:
                    logger.info("✅ Аудио пайп открыт для записи")

                    # Сначала отправляем тишину для инициализации
                    silence_samples = int(44100 * 1.0 * 2 * 2)  # 1 сек тишины
                    silence_data = bytes(silence_samples)
                    pipe_fd.write(silence_data)
                    pipe_fd.flush()

                    while self.is_streaming:
                        try:
                            # Ждем аудио файл в очереди
                            audio_file = self.audio_queue.get(timeout=1)
                            if audio_file and os.path.exists(audio_file):
                                logger.info(f"🎵 Отправка аудио в пайп: {os.path.basename(audio_file)}")
                                self._send_audio_to_pipe(pipe_fd, audio_file)
                        except queue.Empty:
                            # Если очередь пуста, отправляем тишину
                            silence_samples = int(44100 * 0.1 * 2 * 2)  # 100 мс тишины
                            silence_data = bytes(silence_samples)
                            pipe_fd.write(silence_data)
                            pipe_fd.flush()
                            continue
                        except Exception as e:
                            logger.error(f"❌ Ошибка обработки аудио: {e}")
            except Exception as e:
                logger.error(f"❌ Ошибка в обработчике аудио: {e}")

        self.audio_thread = threading.Thread(target=audio_handler, daemon=True)
        self.audio_thread.start()

    def _send_audio_to_pipe(self, pipe_fd, audio_file: str):
        """Отправка аудио файла в пайп FFmpeg"""
        try:
            if not os.path.exists(audio_file):
                logger.error(f"❌ Аудио файл не найден: {audio_file}")
                return False

            # Получаем длительность аудио
            duration = self._get_audio_duration(audio_file)
            logger.info(f"⏱️  Длительность аудио: {duration:.1f} сек")

            # Конвертируем аудио в сырой формат для пайпа
            raw_audio_file = audio_file.replace('.mp3', '.raw')

            cmd = [
                'ffmpeg',
                '-i', audio_file,
                '-f', 's16le',
                '-ar', '44100',
                '-ac', '2',
                '-acodec', 'pcm_s16le',
                raw_audio_file
            ]

            # Конвертируем в сырой формат
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
            if result.returncode != 0:
                logger.error(f"❌ Ошибка конвертации аудио: {result.stderr[:200]}")
                return False

            # Отправляем сырое аудио в пайп
            with open(raw_audio_file, 'rb') as audio_fd:
                # Читаем и отправляем данные
                chunk_size = 4096
                bytes_sent = 0
                while True:
                    chunk = audio_fd.read(chunk_size)
                    if not chunk:
                        break
                    pipe_fd.write(chunk)
                    pipe_fd.flush()
                    bytes_sent += len(chunk)

            logger.info(f"✅ Аудио отправлено в пайп ({bytes_sent} байт)")

            # Удаляем временный файл
            try:
                os.remove(raw_audio_file)
            except:
                pass

            return True

        except Exception as e:
            logger.error(f"❌ Ошибка отправки аудио в пайп: {e}", exc_info=True)
            return False

    def _start_monitor_thread(self):
        """Мониторинг процесса FFmpeg"""

        def monitor():
            logger.info(f"👀 Начало мониторинга FFmpeg процесса (PID: {self.ffmpeg_pid})")

            while self.is_streaming and self.stream_process:
                # Проверяем, жив ли процесс
                if self.stream_process.poll() is not None:
                    return_code = self.stream_process.returncode
                    logger.warning(f"⚠️ FFmpeg процесс завершился с кодом: {return_code}")

                    # Пытаемся получить ошибку
                    try:
                        error_output = self.stream_process.stderr.read()
                        if error_output:
                            error_str = error_output.decode('utf-8', errors='ignore')[:500]
                            logger.error(f"FFmpeg ошибка: {error_str}")
                            self.last_error = error_str
                    except:
                        pass

                    self.is_streaming = False
                    break

                time.sleep(0.1)

            logger.info("👀 Мониторинг FFmpeg завершен")

        self.video_thread = threading.Thread(target=monitor, daemon=True)
        self.video_thread.start()

    def play_audio(self, audio_file: str) -> bool:
        """Добавление аудио файла в очередь для воспроизведения"""
        if not os.path.exists(audio_file):
            logger.error(f"❌ Аудио файл не найден: {audio_file}")
            return False

        if not self.is_streaming:
            logger.error("❌ Стрим не запущен")
            return False

        # Добавляем в очередь
        self.audio_queue.put(audio_file)
        logger.info(f"➕ Аудио добавлено в очередь: {os.path.basename(audio_file)}")

        # Обновляем текст на видео
        try:
            agent_name = os.path.basename(audio_file).split('_')[0]
            with open('dynamic_text.txt', 'w', encoding='utf-8') as f:
                f.write(f"Говорит: {agent_name}")
        except:
            pass

        return True

    def _get_audio_duration(self, audio_file: str) -> float:
        """Получение длительности аудио"""
        try:
            result = subprocess.run([
                'ffprobe',
                '-v', 'error',
                '-show_entries', 'format=duration',
                '-of', 'default=noprint_wrappers=1:nokey=1',
                audio_file
            ], capture_output=True, text=True, timeout=5)

            duration_str = result.stdout.strip()
            if duration_str:
                return float(duration_str)
            else:
                return 5.0
        except:
            return 5.0

    def stop_stream(self):
        """Остановка стрима"""
        try:
            self.is_streaming = False

            # Останавливаем процесс FFmpeg
            if self.stream_process:
                logger.info("🛑 Остановка FFmpeg стрима...")
                self.stream_process.terminate()

                # Ждем завершения
                for _ in range(10):
                    if self.stream_process.poll() is not None:
                        break
                    time.sleep(0.5)

                if self.stream_process.poll() is None:
                    self.stream_process.kill()
                    self.stream_process.wait()

                logger.info("✅ FFmpeg стрим остановлен")

            # Удаляем пайп
            if self.audio_pipe and os.path.exists(self.audio_pipe):
                try:
                    os.remove(self.audio_pipe)
                    logger.info(f"🗑️ Удален аудио пайп: {self.audio_pipe}")
                except:
                    pass

            # Удаляем текстовый файл
            if os.path.exists('dynamic_text.txt'):
                try:
                    os.remove('dynamic_text.txt')
                except:
                    pass

            return True

        except Exception as e:
            logger.error(f"❌ Ошибка остановки стрима: {e}")
            return False

    def get_status(self):
        """Получение статуса"""
        return {
            'is_streaming': self.is_streaming,
            'stream_key': self.stream_key[:10] + '...' if self.stream_key else None,
            'rtmp_url': self.rtmp_url,
            'pid': self.ffmpeg_pid,
            'audio_queue_size': self.audio_queue.qsize(),
            'last_error': self.last_error,
            'uptime': time.time() - self.stream_start_time if self.stream_start_time else 0
        }

# ========== EDGE TTS MANAGER ==========

class EdgeTTSManager:
    """Менеджер TTS для генерации аудио и передачи в стрим"""

    def __init__(self, ffmpeg_manager: FFmpegPipeStreamManager = None):
        self.cache_dir = 'audio_cache'
        os.makedirs(self.cache_dir, exist_ok=True)
        self.ffmpeg_manager = ffmpeg_manager
        self.voice_map = {
            'male_ru': 'ru-RU-DmitryNeural',
            'male_ru_deep': 'ru-RU-DmitryNeural',
            'female_ru': 'ru-RU-SvetlanaNeural',
            'female_ru_soft': 'ru-RU-DariyaNeural'
        }

        try:
            pygame.mixer.init(frequency=44100, size=-16, channels=2, buffer=2048)
            self.pygame_available = True
        except:
            self.pygame_available = False
            logger.warning("⚠️ Pygame не доступен для локального воспроизведения")

        logger.info("Edge TTS Manager инициализирован")

    async def text_to_speech_and_stream(self, text: str, voice_id: str = 'male_ru', agent_name: str = "") -> Optional[str]:
        """Генерация аудио и отправка в стрим"""
        try:
            if voice_id not in self.voice_map:
                voice_id = 'male_ru'

            voice_name = self.voice_map[voice_id]

            # Хэш для имени файла
            text_hash = hashlib.md5(f"{text}_{voice_id}".encode()).hexdigest()
            cache_file = os.path.join(self.cache_dir, f"{agent_name}_{text_hash}.mp3")

            # Проверяем кэш
            if os.path.exists(cache_file):
                logger.info(f"♻️ Используем кэшированное аудио: {os.path.basename(cache_file)}")
            else:
                # Настройки голоса
                rate = '+0%'
                pitch = '+0Hz'

                if voice_id == 'male_ru_deep':
                    rate = '-10%'
                    pitch = '-20Hz'
                elif voice_id == 'female_ru_soft':
                    rate = '-5%'
                    pitch = '+10Hz'

                # Генерация аудио
                logger.info(f"🔊 Генерация TTS для {agent_name}: {text[:50]}...")

                communicate = edge_tts.Communicate(
                    text=text,
                    voice=voice_name,
                    rate=rate,
                    pitch=pitch
                )

                # Сохраняем аудио
                await communicate.save(cache_file)
                logger.info(f"💾 Аудио сохранено: {cache_file}")

            # Проверяем, что файл создан
            if not os.path.exists(cache_file) or os.path.getsize(cache_file) == 0:
                logger.error(f"❌ Аудио файл не создан или пустой: {cache_file}")
                return None

            # Воспроизводим локально для тестирования
            if self.pygame_available:
                try:
                    pygame.mixer.music.load(cache_file)
                    pygame.mixer.music.play()

                    # Ждем окончания
                    duration = self._get_audio_duration(cache_file)
                    await asyncio.sleep(duration)

                    logger.info(f"🔊 Локальное воспроизведение завершено")
                except Exception as e:
                    logger.warning(f"Не удалось воспроизвести локально: {e}")

            # Отправляем в стрим через FFmpeg пайп
            if self.ffmpeg_manager and self.ffmpeg_manager.is_streaming:
                logger.info(f"📤 Отправка аудио в стрим: {os.path.basename(cache_file)}")
                success = self.ffmpeg_manager.play_audio(cache_file)

                if success:
                    logger.info(f"✅ Аудио отправлено в очередь стрима")
                    return cache_file
                else:
                    logger.error(f"❌ Не удалось отправить аудио в стрим")
                    return None
            else:
                logger.warning("⚠️ FFmpeg стрим не активен, только локальное воспроизведение")
                return cache_file

        except Exception as e:
            logger.error(f"❌ Ошибка в text_to_speech_and_stream: {e}", exc_info=True)
            return None

    def _get_audio_duration(self, audio_file: str) -> float:
        """Получение длительности аудио файла"""
        try:
            result = subprocess.run([
                'ffprobe',
                '-v', 'error',
                '-show_entries', 'format=duration',
                '-of', 'default=noprint_wrappers=1:nokey=1',
                audio_file
            ], capture_output=True, text=True)

            duration = float(result.stdout.strip())
            return duration

        except:
            # Приблизительная оценка: 10 слов в секунду
            with open(audio_file, 'rb') as f:
                size = len(f.read())
            return size / (44100 * 2 * 2)


# ========== AI AGENT ==========

class AIAgent:
    """AI агент"""

    def __init__(self, config: Dict[str, Any]):
        self.id = config["id"]
        self.name = config["name"]
        self.expertise = config["expertise"]
        self.personality = config["personality"]
        self.avatar = config["avatar"]
        self.color = config["color"]
        self.voice = config["voice"]
        self.message_history = []

    async def generate_response(self, topic: str, conversation_history: List[str] = None) -> str:
        """Генерация ответа через OpenAI"""
        if not openai_client:
            # Демо-режим
            demo_responses = [
                f"Как эксперт в {self.expertise.lower()}, я считаю, что {topic.lower()} - важная тема.",
                f"С точки зрения {self.expertise.lower()}, можно выделить несколько ключевых аспектов.",
                f"Мои исследования в {self.expertise.lower()} показывают интересные перспективы.",
            ]
            return random.choice(demo_responses)

        try:
            system_prompt = f"""Ты {self.name}, эксперт в области {self.expertise}.
Твоя личность: {self.personality}

Ты участвуешь в научной дискуссии на YouTube стриме. Будь:
- Профессиональным и уважительным
- Конкретным и содержательным
- Естественным в общении
- Используй примеры из своей области

Отвечай 2-3 предложениями."""

            user_prompt = f"Тема дискуссии: {topic}\n\n"

            if conversation_history:
                user_prompt += "Последние реплики:\n"
                for msg in conversation_history[-3:]:
                    user_prompt += f"- {msg}\n"
                user_prompt += "\n"

            user_prompt += f"{self.name}, что ты думаешь по этой теме? (кратко, 2-3 предложения)"

            # Вызов OpenAI API
            response = await asyncio.to_thread(
                openai_client.chat.completions.create,
                model=Config.OPENAI_MODEL,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt}
                ],
                temperature=0.8,
                max_tokens=250
            )

            message = response.choices[0].message.content.strip()

            # Очищаем артефакты
            if message.startswith(f"{self.name}:"):
                message = message[len(f"{self.name}:"):].strip()
            if message.startswith('"') and message.endswith('"'):
                message = message[1:-1]

            self.message_history.append(message[:100] + "...")

            return message

        except Exception as e:
            logger.error(f"❌ Ошибка генерации ответа для {self.name}: {e}")
            return f"Как эксперт в {self.expertise.lower()}, я считаю, что {topic.lower()} требует внимательного изучения."


# ========== AI STREAM MANAGER ==========

class AIStreamManager:
    """Менеджер стрима"""

    def __init__(self, ffmpeg_manager: FFmpegPipeStreamManager = None):
        self.agents: List[AIAgent] = []
        self.tts_manager = EdgeTTSManager(ffmpeg_manager)
        self.ffmpeg_manager = ffmpeg_manager
        self.current_topic = ""
        self.is_discussion_active = False
        self.message_count = 0
        self.discussion_round = 0
        self.active_agent = None
        self.conversation_history = []

        self._init_agents()
        logger.info(f"AI Stream Manager инициализирован с {len(self.agents)} агентами")

    def _init_agents(self):
        """Инициализация агентов"""
        for agent_config in Config.AGENTS:
            agent = AIAgent(agent_config)
            self.agents.append(agent)

    def select_topic(self) -> str:
        """Выбор темы"""
        self.current_topic = random.choice(Config.TOPICS)
        logger.info(f"📝 Выбрана тема: {self.current_topic}")
        socketio.emit('topic_update', {'topic': self.current_topic})
        return self.current_topic

    async def run_discussion_round(self):
        """Запуск раунда дискуссии"""
        if self.is_discussion_active:
            logger.warning("⚠️ Дискуссия уже активна")
            return

        self.is_discussion_active = True
        self.discussion_round += 1

        try:
            if not self.current_topic:
                self.select_topic()

            logger.info(f"🚀 Начало раунда #{self.discussion_round}: {self.current_topic}")

            # Определяем порядок выступлений
            speaking_order = random.sample(self.agents, len(self.agents))

            # Уведомляем о начале раунда
            socketio.emit('round_started', {
                'round': self.discussion_round,
                'topic': self.current_topic,
                'agents': [{'id': a.id, 'name': a.name} for a in speaking_order]
            })

            for agent in speaking_order:
                if not self.is_discussion_active:
                    logger.info("⏸️ Дискуссия остановлена")
                    break

                # Агент начинает говорить
                self.active_agent = agent.id
                socketio.emit('agent_start_speaking', {
                    'agent_id': agent.id,
                    'agent_name': agent.name,
                    'expertise': agent.expertise
                })

                # Генерация ответа через OpenAI
                logger.info(f"🤖 {agent.name} генерирует ответ...")
                message = await agent.generate_response(
                    self.current_topic,
                    self.conversation_history
                )

                # Сохраняем в историю
                self.conversation_history.append(f"{agent.name}: {message}")
                self.message_count += 1

                # Отправляем сообщение в WebSocket
                socketio.emit('new_message', {
                    'agent_id': agent.id,
                    'agent_name': agent.name,
                    'message': message,
                    'expertise': agent.expertise,
                    'avatar': agent.avatar,
                    'color': agent.color,
                    'timestamp': datetime.now().isoformat()
                })

                logger.info(f"💬 {agent.name}: {message[:80]}...")

                # Генерация и отправка аудио
                logger.info(f"🔊 Генерация TTS для {agent.name}...")

                audio_file = await self.tts_manager.text_to_speech_and_stream(
                    text=message,
                    voice_id=agent.voice,
                    agent_name=agent.name
                )

                if audio_file:
                    logger.info(f"✅ Аудио сгенерировано и отправлено: {os.path.basename(audio_file)}")

                    # Ждем пока аудио должно воспроизводиться
                    audio_duration = self.tts_manager._get_audio_duration(audio_file)
                    logger.info(f"⏱️  Длительность аудио: {audio_duration:.1f} сек")

                    # Добавляем небольшую задержку для надежности
                    await asyncio.sleep(audio_duration + 1)
                else:
                    # Если аудио не сгенерировалось, ждем по количеству слов
                    word_count = len(message.split())
                    pause_duration = max(3, min(word_count * 0.3, 10))
                    logger.warning(f"⚠️ Аудио не сгенерировано, ждем {pause_duration} сек")
                    await asyncio.sleep(pause_duration)

                # Агент заканчивает говорить
                socketio.emit('agent_stop_speaking', {'agent_id': agent.id})
                self.active_agent = None

                # Пауза между агентами
                if agent != speaking_order[-1]:
                    pause = random.uniform(1.5, 3.0)
                    logger.debug(f"⏸️  Пауза между агентами: {pause:.1f} сек")
                    await asyncio.sleep(pause)

            logger.info(f"✅ Раунд #{self.discussion_round} завершен")

            socketio.emit('round_complete', {
                'round': self.discussion_round,
                'total_messages': self.message_count,
                'next_round_in': Config.DISCUSSION_INTERVAL
            })

            # Случайная смена темы (30% вероятность)
            if random.random() > 0.7:
                self.select_topic()

        except Exception as e:
            logger.error(f"❌ Ошибка в раунде дискуссии: {e}", exc_info=True)
            socketio.emit('error', {
                'message': f'Ошибка в дискуссии: {str(e)}',
                'round': self.discussion_round
            })

        finally:
            self.is_discussion_active = False
            self.active_agent = None

    def get_agents_state(self) -> List[Dict[str, Any]]:
        """Состояние агентов"""
        return [
            {
                'id': agent.id,
                'name': agent.name,
                'expertise': agent.expertise,
                'avatar': agent.avatar,
                'color': agent.color,
                'is_speaking': agent.id == self.active_agent,
                'message_count': len(agent.message_history)
            }
            for agent in self.agents
        ]

    def get_stats(self) -> Dict[str, Any]:
        """Статистика"""
        return {
            'message_count': self.message_count,
            'discussion_round': self.discussion_round,
            'current_topic': self.current_topic,
            'is_active': self.is_discussion_active,
            'active_agent': self.active_agent,
            'agents_count': len(self.agents),
            'conversation_history': len(self.conversation_history),
            'ffmpeg_streaming': self.ffmpeg_manager.is_streaming if self.ffmpeg_manager else False
        }


# ========== ГЛОБАЛЬНЫЕ ОБЪЕКТЫ ==========

ffmpeg_manager = FFmpegPipeStreamManager()
stream_manager = AIStreamManager(ffmpeg_manager)

# Инициализация YouTube OAuth
youtube_oauth = None

if YOUTUBE_OAUTH_AVAILABLE:
    try:
        print(f"\n🔧 Инициализация YouTube OAuth...")
        youtube_oauth = YouTubeOAuthStream()

        # Пробуем загрузить сохраненные токены
        if youtube_oauth.load_credentials():
            print("✅ YouTube OAuth: Токены загружены, аутентифицирован")
        else:
            print("ℹ️ YouTube OAuth: Требуется авторизация")

    except Exception as e:
        print(f"❌ Ошибка инициализации YouTube OAuth: {e}")
        import traceback
        traceback.print_exc()
        youtube_oauth = None
else:
    print("ℹ️ YouTube OAuth не будет использоваться")


# ========== АСИНХРОННЫЙ ЦИКЛ ==========

async def discussion_loop():
    """Основной цикл дискуссии"""
    await asyncio.sleep(2)  # Даем время на запуск сервера
    logger.info("🔄 Запуск цикла дискуссии AI агентов")

    # Автоматически выбираем первую тему
    if not stream_manager.current_topic:
        stream_manager.select_topic()

    print(f"📝 Начальная тема: {stream_manager.current_topic}")
    print("🤖 Агенты готовы к дискуссии")

    while True:
        try:
            # Проверяем, активна ли дискуссия
            if not stream_manager.is_discussion_active:
                # Ждем команду или продолжаем автоматически
                await asyncio.sleep(5)
                continue

            # Запускаем раунд дискуссии
            await stream_manager.run_discussion_round()

            # Короткая пауза между раундами
            await asyncio.sleep(Config.DISCUSSION_INTERVAL)

        except asyncio.CancelledError:
            logger.info("🔚 Цикл дискуссии остановлен")
            break
        except Exception as e:
            logger.error(f"❌ Ошибка в цикле дискуссии: {e}", exc_info=True)
            await asyncio.sleep(10)


def start_discussion_loop():
    """Запуск цикла в отдельном потоке"""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(discussion_loop())


# ========== FLASK РОУТЫ ==========

@app.route('/oauth')
def oauth_start():
    """Начало OAuth процесса"""
    try:
        if not youtube_oauth:
            return jsonify({
                'status': 'error',
                'message': 'YouTube OAuth не инициализирован'
            })

        auth_url = youtube_oauth.get_auth_url()
        if not auth_url:
            return jsonify({
                'status': 'error',
                'message': 'Не удалось получить URL авторизации. Проверьте client_secrets.json'
            })

        return redirect(auth_url)

    except Exception as e:
        logger.error(f"Ошибка начала OAuth: {e}")
        return jsonify({'status': 'error', 'message': str(e)})


@app.route('/oauth2callback')
def oauth_callback():
    """Callback URL для OAuth"""
    try:
        authorization_code = request.args.get('code')
        if not authorization_code:
            return jsonify({
                'status': 'error',
                'message': 'Код авторизации не получен'
            })

        if youtube_oauth.authenticate_with_code(authorization_code):
            return """
            <html>
            <body>
                <h1>✅ Авторизация успешна!</h1>
                <p>Теперь вы можете создавать YouTube трансляции.</p>
                <p><a href="/">Вернуться к управлению стримом</a></p>
            </body>
            </html>
            """
        else:
            return """
            <html>
            <body>
                <h1>❌ Ошибка авторизации</h1>
                <p>Не удалось авторизоваться в YouTube.</p>
                <p><a href="/">Вернуться к управлению стримом</a></p>
            </body>
            </html>
            """

    except Exception as e:
        logger.error(f"Ошибка OAuth callback: {e}")
        return f"""
        <html>
        <body>
            <h1>❌ Ошибка</h1>
            <p>{str(e)}</p>
            <p><a href="/">Вернуться к управлению стримом</a></p>
        </body>
        </html>
        """


@app.route('/api/start_youtube_oauth_stream', methods=['POST'])
def start_youtube_oauth_stream():
    """Запуск стрима через YouTube OAuth API"""
    try:
        if not youtube_oauth:
            return jsonify({
                'status': 'error',
                'message': 'YouTube OAuth не настроен'
            }), 501

        # Проверяем аутентификацию
        if not youtube_oauth.youtube:
            # Если не аутентифицирован, возвращаем URL для авторизации
            auth_url = youtube_oauth.get_auth_url()
            if auth_url:
                return jsonify({
                    'status': 'auth_required',
                    'auth_url': auth_url,
                    'message': 'Требуется авторизация в YouTube'
                })
            else:
                return jsonify({
                    'status': 'error',
                    'message': 'Не удалось получить URL авторизации'
                })

        # Получаем параметры
        if request.is_json:
            data = request.get_json()
        else:
            data = request.form

        title = data.get('title', "🤖 AI Agents Live: Научные дебаты ИИ")
        description = data.get('description', Config.STREAM_DESCRIPTION)
        privacy_status = data.get('privacy_status', 'unlisted')
        resolution = data.get('resolution', '1080p')

        logger.info(f"🎬 Запуск YouTube стрима через OAuth: {title}")

        # Создаем трансляцию через YouTube API
        result = youtube_oauth.start_full_stream(
            title=title,
            description=description,
            privacy_status=privacy_status,
            resolution=resolution
        )

        if result and result.get('success'):
            # Устанавливаем stream key в FFmpeg менеджер
            ffmpeg_manager.set_stream_key(result['stream_key'])

            # Запускаем FFmpeg стрим
            ffmpeg_result = ffmpeg_manager.start_stream()

            if ffmpeg_result.get('success'):
                return jsonify({
                    'status': 'started',
                    'broadcast_id': youtube_oauth.broadcast_id,
                    'stream_id': youtube_oauth.stream_id,
                    'watch_url': f"https://youtube.com/watch?v={youtube_oauth.broadcast_id}",
                    'stream_key': youtube_oauth.stream_key,
                    'rtmp_url': youtube_oauth.rtmp_url,
                    'pid': ffmpeg_manager.ffmpeg_pid,
                    'message': 'YouTube трансляция создана и FFmpeg стрим запущен. Трансляция начнется автоматически через 30-60 секунд.'
                })
            else:
                return jsonify({
                    'status': 'error',
                    'message': f'Трансляция создана, но не удалось запустить FFmpeg стрим: {ffmpeg_result.get("error", "Unknown error")}'
                }), 500
        else:
            error_msg = result.get('message', 'Неизвестная ошибка') if result else 'Не удалось создать трансляцию'
            return jsonify({
                'status': 'error',
                'message': error_msg
            }), 500

    except Exception as e:
        logger.error(f"Ошибка запуска YouTube стрима: {e}", exc_info=True)
        return jsonify({
            'status': 'error',
            'message': f'Внутренняя ошибка сервера: {str(e)}'
        }), 500


@app.route('/api/test_audio/<int:agent_id>')
def test_audio(agent_id):
    """Тестирование аудио для агента"""
    try:
        # Найти агента
        agent = None
        for a in stream_manager.agents:
            if a.id == agent_id:
                agent = a
                break

        if not agent:
            return jsonify({'success': False, 'error': 'Агент не найден'})

        # Тестовый текст
        test_text = f"Привет! Это тестовое сообщение от {agent.name}. Проверка звука на стриме."

        # Запустить в отдельном потоке
        def run_test():
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)

            audio_file = loop.run_until_complete(
                stream_manager.tts_manager.text_to_speech_and_stream(
                    text=test_text,
                    voice_id=agent.voice,
                    agent_name=agent.name
                )
            )

            return audio_file

        thread = threading.Thread(target=run_test)
        thread.start()
        thread.join(timeout=30)

        return jsonify({
            'success': True,
            'message': f'Тестовое аудио для {agent.name} отправлено'
        })

    except Exception as e:
        logger.error(f"Ошибка тестирования аудио: {e}")
        return jsonify({'success': False, 'error': str(e)})


@app.route('/')
def index():
    """Главная страница"""
    youtube_status = {
        'available': youtube_oauth is not None,
        'authenticated': youtube_oauth is not None and youtube_oauth.youtube is not None,
        'has_broadcast': youtube_oauth is not None and youtube_oauth.broadcast_id is not None,
        'is_live': youtube_oauth is not None and youtube_oauth.is_live,
        'broadcast_id': youtube_oauth.broadcast_id if youtube_oauth else None,
        'stream_key': youtube_oauth.stream_key if youtube_oauth else None,
        'rtmp_url': youtube_oauth.rtmp_url if youtube_oauth else None
    }

    return render_template('index.html',
                           agents=stream_manager.get_agents_state(),
                           topic=stream_manager.current_topic or "Загрузка темы...",
                           stats=stream_manager.get_stats(),
                           youtube_status=youtube_status)


@app.route('/api/agents')
def get_agents():
    """Получение списка агентов"""
    return jsonify(stream_manager.get_agents_state())


@app.route('/api/stats')
def get_stats():
    """Получение статистики"""
    return jsonify(stream_manager.get_stats())


@app.route('/api/start_discussion', methods=['POST'])
def api_start_discussion():
    """Ручной запуск дискуссии"""
    if not stream_manager.is_discussion_active:
        stream_manager.is_discussion_active = True
        topic = stream_manager.select_topic()
        return jsonify({'success': True, 'topic': topic, 'message': 'Дискуссия начата'})
    else:
        return jsonify({'success': False, 'message': 'Дискуссия уже активна'})


@app.route('/api/stop_discussion', methods=['POST'])
def api_stop_discussion():
    """Остановка дискуссии"""
    stream_manager.is_discussion_active = False
    return jsonify({'success': True, 'message': 'Дискуссия остановлена'})


@app.route('/api/change_topic', methods=['POST'])
def api_change_topic():
    """Смена темы"""
    topic = stream_manager.select_topic()
    return jsonify({'success': True, 'topic': topic})


# ========== ЗАПУСК СЕРВЕРА ==========

def signal_handler(signum, frame):
    """Обработчик сигналов"""
    print(f"\n🛑 Получен сигнал {signum}. Завершение...")

    # Останавливаем стрим
    if ffmpeg_manager.is_streaming:
        ffmpeg_manager.stop_stream()

    # Останавливаем YouTube трансляцию если активна
    if youtube_oauth and youtube_oauth.is_live:
        try:
            youtube_oauth.complete_broadcast()
        except:
            pass

    sys.exit(0)


if __name__ == '__main__':
    # Регистрируем обработчики сигналов
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    print("=" * 70)
    print("🤖 AI AGENTS STREAM WITH FFMPEG (PIPE Version)")
    print("=" * 70)

    # Информация о OAuth
    youtube_status_msg = "❌ Не настроен"
    if youtube_oauth:
        if youtube_oauth.youtube:
            youtube_status_msg = "✅ Аутентифицирован через OAuth"
            metrics = youtube_oauth.get_metrics()
            print(f"   YouTube OAuth: {youtube_status_msg}")
            print(f"   Метрики: {metrics['broadcasts_created']} трансляций, {metrics['streams_created']} потоков")
        else:
            youtube_status_msg = "⚠️ Требуется авторизация"
            print(f"   YouTube OAuth: {youtube_status_msg}")
            print(f"   🔗 Авторизация: http://localhost:5500/oauth")
    else:
        print(f"   YouTube OAuth: {youtube_status_msg}")
        print(f"   Используйте ручной Stream Key или настройте OAuth")

    # Проверяем зависимости
    print("\n🔧 Проверка зависимостей...")

    # Проверяем FFmpeg
    try:
        result = subprocess.run(['ffmpeg', '-version'], capture_output=True, text=True)
        if result.returncode == 0:
            print("✅ FFmpeg установлен")
        else:
            print("❌ FFmpeg не найден. Установите: sudo apt install ffmpeg")
    except:
        print("❌ Не удалось запустить FFmpeg")

    # Проверяем Edge TTS (исправлено - без await в этом контексте)
    try:
        import edge_tts
        print("✅ Edge TTS установлен")
        print("   Доступные голоса (с русским):")
        print("   • ru-RU-DmitryNeural - мужской голос")
        print("   • ru-RU-SvetlanaNeural - женский голос")
        print("   • ru-RU-DariyaNeural - женский мягкий")
    except ImportError:
        print("❌ Edge TTS не установлен: pip install edge-tts")

    # Проверяем Pygame
    try:
        import pygame
        pygame.mixer.init()
        pygame.mixer.quit()
        print("✅ Pygame установлен")
    except:
        print("⚠️ Pygame не установлен, локальное воспроизведение недоступно")

    # Проверяем OpenAI
    if Config.OPENAI_API_KEY:
        print("✅ OpenAI API ключ настроен")
    else:
        print("⚠️ OpenAI API ключ не найден, будет использоваться демо-режим")

    # Запускаем цикл дискуссии в отдельном потоке
    print("\n🔄 Запуск цикла дискуссии AI агентов...")
    discussion_thread = threading.Thread(target=start_discussion_loop, daemon=True)
    discussion_thread.start()
    print("✅ Цикл дискуссии запущен")

    # Статистика агентов
    print(f"👥 Загружено {len(stream_manager.agents)} AI агентов:")
    for agent in stream_manager.agents:
        print(f"   • {agent.name} - {agent.expertise} ({agent.voice})")

    print("\n" + "=" * 70)
    print("🌐 Веб-интерфейс доступен по адресу: http://localhost:5000")
    print("🔗 Тестирование аудио агентов:")
    for agent in stream_manager.agents:
        print(f"   • {agent.name}: http://localhost:5000/api/test_audio/{agent.id}")
    print("=" * 70)

    # Создаем UI если его нет
    ui_dir = "stream_ui"
    if not os.path.exists(ui_dir):
        os.makedirs(ui_dir, exist_ok=True)

        # Создаем простой HTML интерфейс
        index_html = '''<!DOCTYPE html>
<html>
<head>
    <title>🤖 AI Stream Control</title>
    <meta charset="utf-8">
    <style>
        body { font-family: Arial, sans-serif; margin: 20px; background: #1a1a1a; color: white; }
        .container { max-width: 1200px; margin: 0 auto; }
        .header { text-align: center; margin-bottom: 30px; }
        .agents-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(300px, 1fr)); gap: 20px; }
        .agent-card { background: #2d2d2d; padding: 20px; border-radius: 10px; border-left: 5px solid; }
        .speaking { box-shadow: 0 0 20px rgba(0, 255, 0, 0.5); }
        .topic-box { background: #2d2d2d; padding: 20px; border-radius: 10px; margin: 20px 0; }
        .controls { display: flex; flex-wrap: wrap; gap: 10px; margin: 20px 0; }
        button { padding: 10px 20px; background: #4a69ff; color: white; border: none; border-radius: 5px; cursor: pointer; }
        button:hover { background: #3a59ef; }
        button.test { background: #ff6b4a; }
        button.test:hover { background: #ff5b3a; }
        .status { padding: 10px; border-radius: 5px; margin: 10px 0; }
        .status-streaming { background: #1a5a1a; }
        .status-stopped { background: #5a1a1a; }
        .audio-test { margin-top: 10px; }
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h1>🤖 AI Agents Live Stream Control</h1>
            <p>Управление автономными ИИ агентами и YouTube трансляцией</p>
        </div>

        <div id="status" class="status status-stopped">
            Статус: Загрузка...
        </div>

        <div id="topic-box" class="topic-box">
            <h3>Текущая тема дискуссии:</h3>
            <p id="current-topic">Загрузка...</p>
        </div>

        <div class="controls">
            <button onclick="startDiscussion()">▶️ Начать дискуссию</button>
            <button onclick="stopDiscussion()">⏹️ Остановить дискуссию</button>
            <button onclick="changeTopic()">🔄 Сменить тему</button>
            <button onclick="startYouTubeStream()">📺 Запустить YouTube стрим</button>
        </div>

        <div class="agents-grid" id="agents-container">
            <!-- AI агенты будут здесь -->
        </div>

        <div id="messages" style="margin-top: 30px;">
            <h3>Последние сообщения:</h3>
            <div id="messages-list"></div>
        </div>
    </div>

    <script src="https://cdnjs.cloudflare.com/ajax/libs/socket.io/4.0.0/socket.io.min.js"></script>
    <script>
        const socket = io();

        socket.on('connect', () => {
            console.log('Connected to server');
            updateStatus('connected');
        });

        socket.on('topic_update', (data) => {
            document.getElementById('current-topic').textContent = data.topic;
        });

        socket.on('agent_start_speaking', (data) => {
            const agentCard = document.getElementById(`agent-${data.agent_id}`);
            if (agentCard) {
                agentCard.classList.add('speaking');
                agentCard.innerHTML += `<div style="color: #4a69ff; margin-top: 10px;">🎤 Говорит сейчас...</div>`;
            }
        });

        socket.on('agent_stop_speaking', (data) => {
            const agentCard = document.getElementById(`agent-${data.agent_id}`);
            if (agentCard) {
                agentCard.classList.remove('speaking');
                // Убираем сообщение "Говорит"
                const speakingMsg = agentCard.querySelector('div[style*="color: #4a69ff"]');
                if (speakingMsg) speakingMsg.remove();
            }
        });

        socket.on('new_message', (data) => {
            const messagesList = document.getElementById('messages-list');
            const messageDiv = document.createElement('div');
            messageDiv.style.background = '#2d2d2d';
            messageDiv.style.padding = '10px';
            messageDiv.style.margin = '10px 0';
            messageDiv.style.borderRadius = '5px';
            messageDiv.style.borderLeft = `5px solid ${data.color}`;

            messageDiv.innerHTML = `
                <strong>${data.agent_name}</strong> (${data.expertise}):<br>
                ${data.message}
                <div style="font-size: 12px; color: #888; margin-top: 5px;">
                    ${new Date(data.timestamp).toLocaleTimeString()}
                </div>
            `;

            messagesList.prepend(messageDiv);

            // Ограничиваем количество сообщений
            if (messagesList.children.length > 10) {
                messagesList.removeChild(messagesList.lastChild);
            }
        });

        function updateStatus(status) {
            const statusDiv = document.getElementById('status');
            statusDiv.textContent = `Статус: ${status}`;
            if (status.includes('подключен') || status.includes('запущен')) {
                statusDiv.className = 'status status-streaming';
            } else {
                statusDiv.className = 'status status-stopped';
            }
        }

        function startDiscussion() {
            fetch('/api/start_discussion', { method: 'POST' })
                .then(response => response.json())
                .then(data => {
                    if (data.success) {
                        alert('Дискуссия начата: ' + data.topic);
                    } else {
                        alert('Ошибка: ' + data.message);
                    }
                })
                .catch(err => console.error('Error:', err));
        }

        function stopDiscussion() {
            fetch('/api/stop_discussion', { method: 'POST' })
                .then(response => response.json())
                .then(data => alert(data.message || 'Дискуссия остановлена'))
                .catch(err => console.error('Error:', err));
        }

        function changeTopic() {
            fetch('/api/change_topic', { method: 'POST' })
                .then(response => response.json())
                .then(data => {
                    if (data.topic) {
                        document.getElementById('current-topic').textContent = data.topic;
                        alert('Тема изменена: ' + data.topic);
                    }
                })
                .catch(err => console.error('Error:', err));
        }

        function startYouTubeStream() {
            const title = prompt('Введите заголовок трансляции:', '🤖 AI Agents Live: Научные дебаты ИИ');
            if (!title) return;

            fetch('/api/start_youtube_oauth_stream', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ title: title })
            })
            .then(response => response.json())
            .then(data => {
                if (data.status === 'started') {
                    alert(`YouTube трансляция запущена!\\nСсылка: ${data.watch_url}`);
                } else if (data.status === 'auth_required') {
                    window.open(data.auth_url, '_blank');
                    alert('Требуется авторизация в YouTube. Откройте ссылку для авторизации.');
                } else {
                    alert('Ошибка: ' + (data.message || 'Неизвестная ошибка'));
                }
            })
            .catch(err => console.error('Error:', err));
        }

        function testAudio(agentId, agentName) {
            if (confirm(`Тестировать аудио для ${agentName}?`)) {
                fetch(`/api/test_audio/${agentId}`)
                    .then(response => response.json())
                    .then(data => {
                        if (data.success) {
                            alert(data.message || 'Тестовое аудио отправлено');
                        } else {
                            alert('Ошибка: ' + (data.error || 'Неизвестная ошибка'));
                        }
                    })
                    .catch(err => console.error('Error:', err));
            }
        }

        // Загружаем начальное состояние
        fetch('/api/agents')
            .then(response => response.json())
            .then(agents => {
                const container = document.getElementById('agents-container');
                agents.forEach(agent => {
                    const card = document.createElement('div');
                    card.className = 'agent-card';
                    card.id = `agent-${agent.id}`;
                    card.style.borderLeftColor = agent.color;

                    card.innerHTML = `
                        <h3>${agent.avatar} ${agent.name}</h3>
                        <p><em>${agent.expertise}</em></p>
                        <p>Сообщений: ${agent.message_count}</p>
                        <div class="audio-test">
                            <button class="test" onclick="testAudio(${agent.id}, '${agent.name}')">🔊 Тест аудио</button>
                        </div>
                        ${agent.is_speaking ? '<div style="color: #4a69ff; margin-top: 10px;">🎤 Говорит сейчас...</div>' : ''}
                    `;

                    container.appendChild(card);
                });
            })
            .catch(err => console.error('Error loading agents:', err));

        fetch('/api/stats')
            .then(response => response.json())
            .then(stats => {
                if (stats.current_topic) {
                    document.getElementById('current-topic').textContent = stats.current_topic;
                }
                updateStatus(`Активность: ${stats.is_active ? 'Дискуссия идет' : 'Пауза'} | Сообщений: ${stats.message_count}`);
            })
            .catch(err => console.error('Error loading stats:', err));
    </script>
</body>
</html>'''

        with open(os.path.join(ui_dir, 'index.html'), 'w', encoding='utf-8') as f:
            f.write(index_html)
        print("📁 Создан веб-интерфейс в папке stream_ui")

    try:
        socketio.run(app,
                     host='0.0.0.0',
                     port=5000,
                     debug=False,
                     use_reloader=False,
                     allow_unsafe_werkzeug=True)
    except Exception as e:
        logger.error(f"❌ Ошибка запуска сервера: {e}")
        print(f"\n❌ Ошибка: {e}")