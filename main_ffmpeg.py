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

discussion_loop_event_loop = None  # <-- Добавить эту строку
discussion_thread = None
discussion_loop_task = None
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

PYTHON_AUDIO_AVAILABLE = False
try:
    import pyaudio
    PYTHON_AUDIO_AVAILABLE = True
    print("✅ PyAudio доступен для аудио захвата")
except ImportError:
    print("⚠️ PyAudio не установлен. Аудио захват будет ограничен.")
    print("📦 Для захвата аудио установите: pip install pyaudio")

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

class FFmpegStreamManager:
    """Управление FFmpeg стримом на YouTube"""

    def __init__(self):
        self.stream_process = None
        self.is_streaming = False
        self.stream_key = None
        self.rtmp_url = None
        self.ffmpeg_pid = None
        self.video_source = "black"
        self.ffmpeg_stdin = None
        self.start_time = None
        self.audio_queue = []  # Очередь аудио файлов для воспроизведения
        self.is_playing_audio = False

        # Настройки аудио
        self.audio_sample_rate = 44100
        self.audio_channels = 2

        # Флаг для определения, используем ли мы PyAudio
        self.use_pyaudio = PYTHON_AUDIO_AVAILABLE

    def set_stream_key(self, stream_key: str):
        """Установка ключа стрима"""
        self.stream_key = stream_key
        self.rtmp_url = f"rtmp://a.rtmp.youtube.com/live2/{stream_key}"
        logger.info(f"🔑 Stream Key установлен: {stream_key[:10]}...")
        return True

    def set_video_source(self, source_type: str, source_param: str = None):
        """Установка источника видео"""
        self.video_source = source_type
        self.video_param = source_param
        logger.info(f"📹 Источник видео: {source_type}")

    def start_stream(self, use_audio: bool = True):
        """Запуск FFmpeg стрима с объединением аудио и видео"""
        if not self.stream_key:
            logger.error("❌ Stream Key не установлен!")
            return {'success': False, 'error': 'Stream Key не установлен'}

        try:
            self.start_time = time.time()
            self.rtmp_url = f"rtmp://a.rtmp.youtube.com/live2/{self.stream_key}"

            logger.info(f"🎬 Подготовка стрима на: {self.rtmp_url}")

            # 1. Создаем основной видео источник
            video_filter = "color=black:size=1920x1080:rate=30"
            current_time = datetime.now().strftime("%H:%M")

            # Добавляем текст с текущим временем (опционально)
            video_filter += f",drawtext=text='AI Stream {current_time}':fontcolor=white:fontsize=24:x=(w-text_w)/2:y=20"

            # 2. Создаем сложный фильтр для смешивания аудио
            # Мы будем использовать amix фильтр для смешивания разных аудио источников
            complex_filter = f"""
                [0:v]fps=30,format=yuv420p[video];
                [1:a]aformat=sample_fmts=fltp:sample_rates=44100:channel_layouts=stereo[audio1];
                [2:a]aformat=sample_fmts=fltp:sample_rates=44100:channel_layouts=stereo[audio2];
                [audio1][audio2]amix=inputs=2:duration=longest:dropout_transition=0[audio_out]
            """

            # 3. Команда FFmpeg
            ffmpeg_cmd = [
                'ffmpeg',
                '-re',  # Реальное время для видео

                # Видео источник
                '-f', 'lavfi',
                '-i', video_filter,

                # Основной аудио источник (тишина)
                '-f', 'lavfi',
                '-i', 'anullsrc=r=44100:cl=stereo',

                # Второй аудио источник для TTS (будет подключаться позже)
                '-f', 'lavfi',
                '-i', 'anullsrc=r=44100:cl=stereo',

                # Комплексный фильтр для обработки
                '-filter_complex', complex_filter.strip().replace('\n', ' '),

                # Кодек видео
                '-map', '[video]',
                '-c:v', 'libx264',
                '-preset', 'veryfast',
                '-tune', 'zerolatency',
                '-pix_fmt', 'yuv420p',
                '-g', '60',
                '-b:v', '4500k',
                '-maxrate', '4500k',
                '-bufsize', '9000k',
                '-r', '30',

                # Кодек аудио
                '-map', '[audio_out]',
                '-c:a', 'aac',
                '-b:a', '128k',
                '-ar', '44100',
                '-ac', '2',

                # Формат вывода
                '-f', 'flv',
                '-flvflags', 'no_duration_filesize',
                self.rtmp_url
            ]

            logger.info(f"🚀 Запуск FFmpeg: {' '.join(ffmpeg_cmd[:10])}...")
            logger.debug(f"Полная команда: {' '.join(ffmpeg_cmd)}")

            # Запускаем FFmpeg
            self.stream_process = subprocess.Popen(
                ffmpeg_cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                stdin=subprocess.PIPE,  # Теперь используем stdin
                text=False,
                bufsize=0
            )

            self.is_streaming = True
            self.ffmpeg_pid = self.stream_process.pid
            self.ffmpeg_stdin = self.stream_process.stdin
            self.ffmpeg_stdout = self.stream_process.stdout
            self.ffmpeg_stderr = self.stream_process.stderr

            # Запуск мониторинга
            threading.Thread(target=self._monitor_ffmpeg, daemon=True).start()
            logger.info(f"🎬 FFmpeg стрим запущен (PID: {self.ffmpeg_pid})")

            return {'success': True, 'pid': self.ffmpeg_pid, 'message': 'Стрим запущен'}

        except Exception as e:
            logger.error(f"❌ Ошибка запуска FFmpeg: {e}", exc_info=True)
            return {'success': False, 'error': str(e)}
    def _monitor_ffmpeg(self):
        """Мониторинг процесса FFmpeg"""
        try:
            stream_connected = False

            for line in iter(self.stream_process.stderr.readline, b''):
                line = line.decode('utf-8', errors='ignore').strip()

                # Проверяем подключение
                if 'rtmp://' in line and ('connected' in line.lower() or 'connected to' in line.lower()):
                    if not stream_connected:
                        stream_connected = True
                        logger.info("✅ Успешное подключение к YouTube RTMP серверу")
                        socketio.emit('stream_connected', {'status': 'connected'})

                # Логируем статистику
                if 'frame=' in line and 'fps=' in line:
                    logger.debug(f"FFmpeg: {line}")
                elif 'error' in line.lower() or 'failed' in line.lower():
                    logger.error(f"FFmpeg error: {line}")
                    socketio.emit('stream_error', {'message': line})

            # Ждем завершения процесса
            self.stream_process.wait()

        except Exception as e:
            logger.error(f"Ошибка мониторинга FFmpeg: {e}")
        finally:
            self.is_streaming = False
            if self.ffmpeg_stdin:
                try:
                    self.ffmpeg_stdin.close()
                except:
                    pass

    def _audio_processor(self):
        """Обработчик аудио очереди"""
        import numpy as np

        while self.is_streaming:
            try:
                if self.audio_queue:
                    audio_file = self.audio_queue.pop(0)
                    self.is_playing_audio = True
                    self.stream_audio_realtime(audio_file)
                    self.is_playing_audio = False
                else:
                    # Отправляем тишину
                    silence_duration = 0.1  # 100 мс
                    samples = int(self.audio_sample_rate * silence_duration)
                    silence = np.zeros(samples * self.audio_channels, dtype=np.int16).tobytes()

                    if self.ffmpeg_stdin:
                        try:
                            self.ffmpeg_stdin.write(silence)
                            self.ffmpeg_stdin.flush()
                        except:
                            break

                    time.sleep(silence_duration)
            except Exception as e:
                logger.error(f"Ошибка обработчика аудио: {e}")
                time.sleep(0.1)

    def add_audio_to_queue(self, audio_file: str):
        """Добавление аудио файла в очередь на воспроизведение"""
        if not os.path.exists(audio_file):
            logger.error(f"❌ Аудио файл не найден: {audio_file}")
            return False

        self.audio_queue.append(audio_file)
        logger.info(f"🎵 Аудио добавлено в очередь: {os.path.basename(audio_file)}")
        logger.info(f"📊 Очередь аудио: {len(self.audio_queue)} файлов")
        return True

    def send_audio_to_stream(self, audio_data: bytes):
        """Отправка аудио данных в стрим"""
        if not self.is_streaming or not self.ffmpeg_stdin:
            logger.warning("⚠️ Не могу отправить аудио: стрим не активен")
            return False

        try:
            # Отправляем аудио в FFmpeg
            self.ffmpeg_stdin.write(audio_data)
            self.ffmpeg_stdin.flush()
            return True

        except Exception as e:
            logger.error(f"❌ Ошибка отправки аудио: {e}")
            return False

    def play_audio_file(self, audio_file: str) -> bool:
        """Воспроизведение аудио файла (MP3) и отправка в стрим"""
        if not os.path.exists(audio_file):
            logger.error(f"❌ Аудио файл не найден: {audio_file}")
            return False

        try:
            logger.info(f"▶️ Воспроизведение аудио: {os.path.basename(audio_file)}")

            # Вариант 1: Используем отдельный FFmpeg процесс для отправки аудио
            ffmpeg_audio_cmd = [
                'ffmpeg',
                '-re',  # Реальное время
                '-i', audio_file,
                '-c:a', 'aac',
                '-b:a', '128k',
                '-ar', '44100',
                '-ac', '2',
                '-f', 'flv',
                self.rtmp_url
            ]

            logger.debug(f"Запуск отдельного FFmpeg для аудио")

            # Запускаем отдельный процесс для аудио
            process = subprocess.Popen(
                ffmpeg_audio_cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE
            )

            # Ждем завершения (но не блокируем основной поток)
            def wait_for_process():
                process.wait()
                if process.returncode == 0:
                    logger.info(f"✅ Аудио успешно воспроизведено: {os.path.basename(audio_file)}")
                else:
                    error = process.stderr.read().decode('utf-8', errors='ignore')
                    logger.error(f"❌ Ошибка воспроизведения аудио: {error}")

            # Запускаем в отдельном потоке
            threading.Thread(target=wait_for_process, daemon=True).start()

            return True

        except Exception as e:
            logger.error(f"❌ Ошибка воспроизведения аудио файла: {e}", exc_info=True)
            return False

    def stream_audio_realtime(self, audio_file: str):
        """Стриминг аудио в реальном времени с синхронизацией"""
        if not self.is_streaming:
            logger.warning("Стрим не активен")
            return False

        try:
            # Получаем длительность аудио
            duration = self._get_audio_duration(audio_file)

            # Команда для конвертации и отправки аудио в реальном времени
            ffmpeg_cmd = [
                'ffmpeg',
                '-re',  # Реальное время (важно для синхронизации!)
                '-i', audio_file,  # Входной файл
                '-f', 's16le',  # Формат выхода
                '-ar', '44100',  # Частота дискретизации
                '-ac', '2',  # Стерео
                '-c:a', 'pcm_s16le',  # Кодек аудио
                '-'  # Вывод в stdout
            ]

            logger.info(f"🎵 Стриминг аудио: {os.path.basename(audio_file)} ({duration:.1f} сек)")

            process = subprocess.Popen(
                ffmpeg_cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=0
            )

            # Стримим аудио порциями
            chunk_size = 88200  # 0.5 секунды аудио (44100 Гц * 2 канала * 2 байта)

            while True:
                audio_data = process.stdout.read(chunk_size)
                if not audio_data:
                    break

                if self.ffmpeg_stdin:
                    try:
                        self.ffmpeg_stdin.write(audio_data)
                        self.ffmpeg_stdin.flush()
                    except BrokenPipeError:
                        logger.error("FFmpeg перестал принимать аудио")
                        break

            process.wait()
            logger.info(f"✅ Аудио завершено: {os.path.basename(audio_file)}")
            return True

        except Exception as e:
            logger.error(f"Ошибка стриминга аудио: {e}")
            return False

    def _get_audio_duration(self, audio_file: str) -> float:
        """Получение длительности аудио файла через ffprobe"""
        try:
            cmd = [
                'ffprobe',
                '-v', 'error',
                '-show_entries', 'format=duration',
                '-of', 'csv=p=0',
                audio_file
            ]

            result = subprocess.run(cmd, capture_output=True, text=True)
            if result.returncode == 0:
                return float(result.stdout.strip())
        except:
            pass

        # Если не получилось, оцениваем примерно
        try:
            # Примерная оценка: 0.1 секунды на слово
            import re
            with open(audio_file, 'rb') as f:
                # Читаем ID3 тег для MP3
                f.seek(-128, 2)
                tag = f.read(3)
                if tag == b'TAG':
                    # MP3 с тегом
                    return 5.0
        except:
            pass

        return 5.0  # Значение по умолчанию

    def stream_audio_sync(self, audio_file: str, wait_for_completion: bool = True):
        """Синхронное воспроизведение аудио файла"""
        if not self.is_streaming:
            logger.warning("Стрим не активен")
            return False

        try:
            # Создаем отдельный процесс для конвертации и отправки аудио
            ffmpeg_cmd = [
                'ffmpeg',
                '-i', audio_file,
                '-f', 's16le',
                '-ar', '44100',
                '-ac', '2',
                '-'
            ]

            process = subprocess.Popen(
                ffmpeg_cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                bufsize=0
            )

            # Создаем pipe для отправки данных
            def send_audio():
                while True:
                    data = process.stdout.read(4096)
                    if not data:
                        break

                    if self.ffmpeg_stdin:
                        try:
                            self.ffmpeg_stdin.write(data)
                            self.ffmpeg_stdin.flush()
                        except:
                            break

            # Запускаем в отдельном потоке
            audio_thread = threading.Thread(target=send_audio, daemon=True)
            audio_thread.start()

            if wait_for_completion:
                audio_thread.join(timeout=30)  # Максимум 30 секунд

            process.wait(timeout=5)
            return True

        except Exception as e:
            logger.error(f"Ошибка синхронного воспроизведения аудио: {e}")
            return False

    def play_audio_simple(self, audio_file: str):
        """Простое воспроизведение аудио файла (самый надежный метод)"""
        if not self.is_streaming or not self.ffmpeg_stdin:
            logger.warning("Стрим не активен или stdin недоступен")
            return False

        try:
            # Используем ffmpeg для прямой отправки в rtmp
            ffmpeg_audio_cmd = [
                'ffmpeg',
                '-re',  # Реальное время
                '-i', audio_file,
                '-c:a', 'aac',
                '-b:a', '128k',
                '-ar', '44100',
                '-ac', '2',
                '-f', 'flv',
                self.rtmp_url
            ]

            logger.info(f"▶️ Воспроизведение аудио: {os.path.basename(audio_file)}")

            # Запускаем отдельный процесс для аудио
            process = subprocess.Popen(
                ffmpeg_audio_cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE
            )

            # Ждем завершения
            process.wait()

            if process.returncode == 0:
                logger.info(f"✅ Аудио успешно воспроизведено")
                return True
            else:
                error = process.stderr.read().decode('utf-8', errors='ignore')
                logger.error(f"❌ Ошибка воспроизведения аудио: {error}")
                return False

        except Exception as e:
            logger.error(f"Ошибка простого воспроизведения аудио: {e}")
            return False

    def stop_stream(self):
        """Остановка стрима"""
        if self.stream_process:
            logger.info("🛑 Остановка FFmpeg стрима...")
            self.is_streaming = False

            try:
                # Очищаем очередь аудио
                self.audio_queue.clear()

                # Закрываем stdin
                if self.ffmpeg_stdin:
                    self.ffmpeg_stdin.close()

                # Отправляем SIGTERM
                self.stream_process.terminate()

                # Ждем завершения
                for _ in range(20):
                    if self.stream_process.poll() is not None:
                        break
                    time.sleep(0.5)

                # Если все еще жив - SIGKILL
                if self.stream_process.poll() is None:
                    self.stream_process.kill()
                    self.stream_process.wait()

                logger.info("✅ FFmpeg стрим остановлен")
                return True

            except Exception as e:
                logger.error(f"❌ Ошибка остановки FFmpeg: {e}")
                return False

        return True

    def get_status(self):
        """Получение статуса"""
        return {
            'is_streaming': self.is_streaming,
            'stream_key': self.stream_key[:10] + '...' if self.stream_key else None,
            'rtmp_url': self.rtmp_url,
            'pid': self.ffmpeg_pid,
            'video_source': self.video_source,
            'use_pyaudio': self.use_pyaudio,
            'audio_queue_size': len(self.audio_queue),
            'is_playing_audio': self.is_playing_audio
        }

    def get_stream_health(self):
        """Проверка здоровья стрима"""
        status = self.get_status()

        # Проверяем, жив ли процесс
        if self.stream_process:
            status['process_alive'] = (self.stream_process.poll() is None)
            if not status['process_alive']:
                status['exit_code'] = self.stream_process.poll()
        else:
            status['process_alive'] = False

        # Проверяем время работы
        if self.start_time:
            status['uptime'] = time.time() - self.start_time

        return status

    def check_stream_connection(self):
        """Проверка подключения к YouTube"""
        if not self.rtmp_url:
            return {'connected': False, 'error': 'No RTMP URL'}

        try:
            # Используем ffprobe для проверки подключения
            cmd = [
                'ffprobe',
                '-v', 'error',
                '-timeout', '5000000',  # 5 секунд таймаут
                self.rtmp_url
            ]

            result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)

            return {
                'connected': result.returncode == 0,
                'output': result.stdout if result.returncode == 0 else result.stderr
            }

        except subprocess.TimeoutExpired:
            return {'connected': False, 'error': 'Connection timeout'}
        except Exception as e:
            return {'connected': False, 'error': str(e)}

    def create_test_audio(self, text: str = "Тестовое сообщение", voice: str = "male_ru"):
        """Создание тестового аудио файла"""
        try:
            import tempfile
            import asyncio
            import edge_tts

            # Создаем временный файл
            with tempfile.NamedTemporaryFile(suffix='.mp3', delete=False) as tmp:
                temp_path = tmp.name

            # Генерируем аудио
            async def generate():
                tts = edge_tts.Communicate(
                    text=text,
                    voice='ru-RU-DmitryNeural' if voice == 'male_ru' else 'ru-RU-SvetlanaNeural'
                )
                await tts.save(temp_path)

            asyncio.run(generate())

            logger.info(f"✅ Тестовое аудио создано: {temp_path}")
            return temp_path

        except Exception as e:
            logger.error(f"❌ Ошибка создания тестового аудио: {e}")
            return None

# ========== EDGE TTS MANAGER ==========

class EdgeTTSManager:
    """Менеджер TTS для генерации аудио и передачи в стрим"""

    def __init__(self, ffmpeg_manager: FFmpegStreamManager = None):
        self.cache_dir = 'audio_cache'
        os.makedirs(self.cache_dir, exist_ok=True)
        self.ffmpeg_manager = ffmpeg_manager

        self.voice_map = {
            'male_ru': 'ru-RU-DmitryNeural',
            'male_ru_deep': 'ru-RU-DmitryNeural',
            'female_ru': 'ru-RU-SvetlanaNeural',
            'female_ru_soft': 'ru-RU-DariyaNeural'
        }

        # Инициализация pygame для локального воспроизведения
        try:
            pygame.mixer.init(frequency=44100, size=-16, channels=2, buffer=2048)
            self.pygame_available = True
        except:
            self.pygame_available = False
            logger.warning("⚠️ Pygame не доступен для локального воспроизведения")

        logger.info("Edge TTS Manager инициализирован")

    async def _stream_audio_to_ffmpeg(self, audio_file: str) -> bool:
        """Отправка аудио файла в FFmpeg стрим"""
        try:
            if not self.ffmpeg_manager or not self.ffmpeg_manager.is_streaming:
                return False

            # Получаем длительность аудио
            duration = self._get_audio_duration(audio_file)
            logger.debug(f"⏱️  Длительность аудио: {duration:.2f} сек")

            # Используем ffmpeg для конвертации и отправки аудио
            ffmpeg_cmd = [
                'ffmpeg',
                '-re',  # Реальное время (важно для синхронизации!)
                '-i', audio_file,
                '-f', 's16le',  # Сырое аудио
                '-ar', '44100',
                '-ac', '2',
                '-'
            ]

            logger.debug(f"Запускаем ffmpeg для аудио: {' '.join(ffmpeg_cmd[:5])}...")

            # Запускаем процесс
            process = await asyncio.create_subprocess_exec(
                *ffmpeg_cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )

            # Читаем и отправляем аудио данные
            total_bytes = 0
            chunk_size = 88200  # 0.5 секунды аудио (44100 Гц * 2 канала * 2 байта)

            while True:
                try:
                    # Читаем порцию аудио
                    audio_data = await process.stdout.read(chunk_size)
                    if not audio_data:
                        break

                    total_bytes += len(audio_data)

                    # Отправляем в FFmpeg stdin
                    if self.ffmpeg_manager.ffmpeg_stdin:
                        try:
                            # Нужно выполнить в отдельном потоке, так как stdin не асинхронный
                            await asyncio.get_event_loop().run_in_executor(
                                None,
                                lambda: self.ffmpeg_manager.ffmpeg_stdin.write(audio_data)
                            )
                            # Не flush слишком часто, это может замедлить
                            if total_bytes % (chunk_size * 10) == 0:
                                await asyncio.get_event_loop().run_in_executor(
                                    None,
                                    self.ffmpeg_manager.ffmpeg_stdin.flush
                                )
                        except (BrokenPipeError, OSError) as e:
                            logger.error(f"❌ Ошибка записи в FFmpeg stdin: {e}")
                            break

                except Exception as e:
                    logger.error(f"❌ Ошибка чтения аудио: {e}")
                    break

            # Ждем завершения процесса
            await process.wait()

            # Финальный flush
            if self.ffmpeg_manager.ffmpeg_stdin:
                try:
                    await asyncio.get_event_loop().run_in_executor(
                        None,
                        self.ffmpeg_manager.ffmpeg_stdin.flush
                    )
                except:
                    pass

            logger.debug(f"📊 Отправлено {total_bytes} байт аудио")
            return True

        except Exception as e:
            logger.error(f"❌ Ошибка стриминга аудио в FFmpeg: {e}", exc_info=True)
            return False

    async def text_to_speech_and_stream(self, text: str, voice_id: str = 'male_ru', agent_name: str = "") -> Optional[
        str]:
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
                logger.debug(f"♻️ Используем кэшированное аудио: {os.path.basename(cache_file)}")
                await self._play_and_stream(cache_file)
                return cache_file

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
            logger.info(f"🔊 Генерация TTS: {agent_name} ({voice_name})")

            communicate = edge_tts.Communicate(
                text=text,
                voice=voice_name,
                rate=rate,
                pitch=pitch
            )

            await communicate.save(cache_file)
            logger.info(f"💾 Аудио сохранено: {os.path.basename(cache_file)}")

            # Воспроизводим и отправляем в стрим
            await self._play_and_stream(cache_file)

            return cache_file

        except Exception as e:
            logger.error(f"❌ Ошибка Edge TTS: {e}", exc_info=True)
            return None

    async def _play_and_stream(self, audio_file: str):
        """Воспроизведение аудио локально и отправка в стрим"""
        try:
            # 1. Локальное воспроизведение (если доступно)
            if self.pygame_available:
                try:
                    pygame.mixer.music.load(audio_file)
                    pygame.mixer.music.play()
                    logger.debug(f"🔊 Локальное воспроизведение: {os.path.basename(audio_file)}")
                except Exception as e:
                    logger.warning(f"Не удалось воспроизвести локально: {e}")

            # 2. Отправка в YouTube стрим
            if self.ffmpeg_manager and self.ffmpeg_manager.is_streaming:
                # Проверяем, доступен ли stdin FFmpeg
                if not self.ffmpeg_manager.ffmpeg_stdin:
                    logger.error("❌ FFmpeg stdin не доступен")
                    return

                logger.info(f"📤 Отправка аудио в стрим: {os.path.basename(audio_file)}")

                # Используем новый метод для отправки аудио
                success = await self._stream_audio_to_ffmpeg(audio_file)

                if success:
                    logger.info(f"✅ Аудио успешно отправлено в стрим: {os.path.basename(audio_file)}")
                else:
                    logger.error(f"❌ Не удалось отправить аудио в стрим: {os.path.basename(audio_file)}")

        except Exception as e:
            logger.error(f"Ошибка воспроизведения: {e}", exc_info=True)

    async def speak_direct(self, text: str, voice_id: str = 'male_ru') -> bool:
        """Прямое озвучивание текста и отправка в стрим"""
        try:
            logger.info(f"🎤 Озвучиваем напрямую: {text[:50]}...")

            audio_file = await self.text_to_speech_and_stream(text, voice_id)

            if audio_file:
                # Ждем окончания воспроизведения
                duration = self._get_audio_duration(audio_file)
                await asyncio.sleep(duration + 0.5)
                return True
            return False

        except Exception as e:
            logger.error(f"Ошибка прямого озвучивания: {e}")
            return False

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

    def __init__(self, ffmpeg_manager: FFmpegStreamManager = None):
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
        """Запуск раунда дискуссии с отправкой звука в стрим"""
        if self.is_discussion_active:
            return

        self.is_discussion_active = True
        self.discussion_round += 1

        try:
            if not self.current_topic:
                self.select_topic()

            logger.info(f"🚀 Начало раунда #{self.discussion_round}")

            # Определяем порядок выступлений
            speaking_order = random.sample(self.agents, len(self.agents))

            for agent in speaking_order:
                if not self.is_discussion_active:
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

                # Генерация аудио и отправка в стрим
                logger.info(f"🔊 Генерация TTS для {agent.name}...")

                audio_task = asyncio.create_task(
                    self.tts_manager.text_to_speech_and_stream(
                        text=message,
                        voice_id=agent.voice,
                        agent_name=agent.name
                    )
                )

                # Ждем завершения генерации аудио
                audio_file = await audio_task

                if audio_file:
                    logger.info(f"✅ Аудио сгенерировано: {os.path.basename(audio_file)}")

                    # Получаем длительность аудио
                    try:
                        audio_duration = self._get_audio_duration(audio_file)
                    except:
                        # Приблизительная длительность
                        word_count = len(message.split())
                        audio_duration = max(3, min(word_count * 0.4, 15))

                    logger.info(f"⏱️  Длительность аудио: {audio_duration:.1f} сек")

                    # Ждем пока аудио воспроизведется
                    await asyncio.sleep(audio_duration)
                else:
                    # Если аудио не сгенерировалось
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

            # Пауза перед следующим раундом
            await asyncio.sleep(Config.DISCUSSION_INTERVAL)

            # Случайная смена темы
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

        except Exception as e:
            logger.warning(f"Не удалось получить длительность аудио: {e}")
            return 5.0

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

ffmpeg_manager = FFmpegStreamManager()
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
    await asyncio.sleep(2)
    logger.info("🔄 Запуск цикла дискуссии")

    # Выбираем первую тему
    stream_manager.select_topic()

    while True:
        try:
            if not stream_manager.is_discussion_active:
                await stream_manager.run_discussion_round()
            await asyncio.sleep(0.5)
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"❌ Ошибка в основном цикле: {e}", exc_info=True)
            await asyncio.sleep(5)


def start_discussion_loop():
    """Запуск цикла в отдельном потоке"""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(discussion_loop())


# ========== FLASK РОУТЫ ==========

@app.route('/health')
def health():
    """Проверка здоровья"""
    return jsonify({
        'status': 'ok',
        'time': datetime.now().isoformat(),
        'agents': len(stream_manager.agents),
        'streaming': ffmpeg_manager.is_streaming,
        'discussion_active': stream_manager.is_discussion_active
    })

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
    """Ручной запуск дискуссии с триггером в event loop"""
    try:
        if not stream_manager.is_discussion_active:
            stream_manager.is_discussion_active = True

            # Если есть event loop, можно триггернуть
            if discussion_loop_event_loop and discussion_loop_event_loop.is_running():
                # Триггерим запуск в event loop дискуссии
                def trigger_discussion():
                    if discussion_loop_event_loop.is_running():
                        # Запланировать задачу в event loop
                        asyncio.run_coroutine_threadsafe(
                            stream_manager.run_discussion_round(),
                            discussion_loop_event_loop
                        )

                # Запускаем в отдельном потоке для безопасности
                threading.Thread(target=trigger_discussion, daemon=True).start()

            topic = stream_manager.select_topic()

            return jsonify({
                'success': True,
                'topic': topic,
                'message': 'Дискуссия начата'
            })
        else:
            return jsonify({
                'success': False,
                'message': 'Дискуссия уже активна'
            })

    except Exception as e:
        logger.error(f"Ошибка запуска дискуссии: {e}")
        return jsonify({'success': False, 'error': str(e)})


@app.route('/api/stop_discussion', methods=['POST'])
def api_stop_discussion():
    """Остановка дискуссии"""
    stream_manager.is_discussion_active = False
    stream_manager.active_agent = None
    logger.info("⏸️  Дискуссия остановлена вручную")
    return jsonify({'success': True, 'message': 'Дискуссия остановлена'})


@app.route('/api/youtube_control', methods=['POST'])
def api_youtube_control():
    """Управление YouTube трансляцией"""
    try:
        data = request.get_json() if request.is_json else request.form
        action = data.get('action', 'get_info')

        if not youtube_oauth:
            return jsonify({
                'status': 'error',
                'message': 'YouTube OAuth не настроен'
            })

        if action == 'get_info':
            metrics = youtube_oauth.get_metrics()
            return jsonify({
                'status': 'success',
                'authenticated': youtube_oauth.youtube is not None,
                'has_broadcast': youtube_oauth.broadcast_id is not None,
                'is_live': youtube_oauth.is_live,
                'broadcast_id': youtube_oauth.broadcast_id,
                'stream_id': youtube_oauth.stream_id,
                'stream_info': {
                    'stream_key': youtube_oauth.stream_key,
                    'rtmp_url': youtube_oauth.rtmp_url
                },
                'metrics': metrics
            })

        elif action == 'start_stream':
            title = data.get('title', "🤖 AI Agents Live Stream")
            description = data.get('description', Config.STREAM_DESCRIPTION)

            result = youtube_oauth.start_full_stream(
                title=title,
                description=description
            )

            if result and result.get('success'):
                return jsonify({
                    'status': 'started',
                    **result
                })
            else:
                return jsonify({
                    'status': 'error',
                    'message': 'Не удалось создать трансляцию'
                }), 500

        else:
            return jsonify({
                'status': 'error',
                'message': f'Неизвестное действие: {action}'
            }), 400

    except Exception as e:
        logger.error(f"Ошибка YouTube контроля: {e}")
        return jsonify({
            'status': 'error',
            'message': str(e)
        }), 500


@app.route('/api/test_audio', methods=['POST'])
def api_test_audio():
    """Тестирование аудио"""
    try:
        data = request.get_json() if request.is_json else request.form
        text = data.get('text', 'Тестовое сообщение для проверки звука')
        voice = data.get('voice', 'male_ru')

        # Запускаем в отдельном потоке
        def run_test():
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)

            audio_file = loop.run_until_complete(
                stream_manager.tts_manager.text_to_speech_and_stream(
                    text=text,
                    voice_id=voice,
                    agent_name="Тест"
                )
            )
            return audio_file

        thread = threading.Thread(target=run_test)
        thread.start()

        return jsonify({
            'success': True,
            'message': 'Тестовое аудио запущено'
        })

    except Exception as e:
        logger.error(f"Ошибка теста аудио: {e}")
        return jsonify({
            'success': False,
            'error': str(e)
        })


@app.route('/api/control', methods=['POST'])
def api_control():
    """Общий endpoint для управления"""
    try:
        data = request.get_json() if request.is_json else request.form
        action = data.get('action')

        if action == 'start_discussion':
            stream_manager.is_discussion_active = True
            return jsonify({
                'status': 'started',
                'message': 'Дискуссия начата'
            })

        elif action == 'stop_discussion':
            stream_manager.is_discussion_active = False
            stream_manager.active_agent = None
            return jsonify({
                'status': 'stopped',
                'message': 'Дискуссия остановлена'
            })

        elif action == 'change_topic':
            topic = stream_manager.select_topic()
            return jsonify({
                'status': 'changed',
                'topic': topic,
                'message': 'Тема изменена'
            })

        else:
            return jsonify({
                'status': 'error',
                'message': f'Неизвестное действие: {action}'
            }), 400

    except Exception as e:
        logger.error(f"Ошибка управления: {e}")
        return jsonify({
            'status': 'error',
            'message': str(e)
        }), 500


@app.route('/api/stop_stream', methods=['POST'])
def api_stop_stream():
    """Остановка стрима"""
    try:
        ffmpeg_manager.stop_stream()

        socketio.emit('stream_stopped', {
            'time': datetime.now().isoformat()
        })

        return jsonify({
            'status': 'stopped',
            'message': 'Стрим остановлен'
        })

    except Exception as e:
        logger.error(f"Ошибка остановки стрима: {e}")
        return jsonify({
            'status': 'error',
            'message': str(e)
        }), 500


@app.route('/api/start_stream', methods=['POST'])
def api_start_stream():
    """Ручной запуск стрима с Stream Key"""
    try:
        data = request.get_json() if request.is_json else request.form
        stream_key = data.get('stream_key')

        if not stream_key:
            return jsonify({
                'success': False,
                'error': 'Stream Key не указан'
            }), 400

        # Устанавливаем ключ
        ffmpeg_manager.set_stream_key(stream_key)

        # Запускаем стрим
        result = ffmpeg_manager.start_stream()

        if result.get('success'):
            # Уведомляем всех клиентов
            socketio.emit('stream_started', {
                'pid': result['pid'],
                'rtmp_url': ffmpeg_manager.rtmp_url
            })

            return jsonify({
                'status': 'started',
                'pid': result['pid'],
                'rtmp_url': ffmpeg_manager.rtmp_url,
                'message': 'Стрим запущен'
            })
        else:
            return jsonify({
                'status': 'error',
                'message': result.get('error', 'Неизвестная ошибка')
            }), 500

    except Exception as e:
        logger.error(f"Ошибка запуска стрима: {e}")
        return jsonify({
            'status': 'error',
            'message': str(e)
        }), 500

@app.route('/api/stream_status')
def get_stream_status():
    """Получение статуса стрима"""
    return jsonify(ffmpeg_manager.get_status())


@app.route('/api/stream_health')
def get_stream_health():
    """Получение здоровья стрима"""
    return jsonify(ffmpeg_manager.get_stream_health())

@app.route('/api/change_topic', methods=['POST'])
def api_change_topic():
    """Смена темы"""
    topic = stream_manager.select_topic()
    return jsonify({'success': True, 'topic': topic})

@app.route('/youtube-control')
def youtube_control():
    """Страница управления YouTube API"""
    return render_template('youtube_control.html')


@app.route('/api/youtube_auth_status')
def youtube_auth_status():
    """Проверка статуса аутентификации YouTube"""
    if youtube_oauth and youtube_oauth.youtube:
        return jsonify({'authenticated': True})
    else:
        return jsonify({'authenticated': False})


# ========== SOCKET.IO HANDLERS ==========

@socketio.on('connect')
def handle_connect():
    """Обработчик подключения клиента"""
    logger.info(f"📡 Клиент подключен: {request.sid}")

    emit('connected', {
        'agents': stream_manager.get_agents_state(),
        'topic': stream_manager.current_topic or "Не выбрана",
        'stats': stream_manager.get_stats(),
        'stream_status': ffmpeg_manager.get_status(),
        'time': datetime.now().isoformat()
    })


@socketio.on('request_update')
def handle_request_update():
    """Запрос обновления состояния"""
    emit('update', {
        'agents': stream_manager.get_agents_state(),
        'topic': stream_manager.current_topic or "Не выбрана",
        'stats': stream_manager.get_stats(),
        'stream_status': ffmpeg_manager.get_status()
    })


@socketio.on('disconnect')
def handle_disconnect():
    logger.info(f"📡 Клиент отключен: {request.sid}")

def signal_handler(signum, frame):
    """Обработчик сигналов"""
    print(f"\n🛑 Получен сигнал {signum}. Завершение...")

    # Останавливаем стрим
    if ffmpeg_manager.is_streaming:
        ffmpeg_manager.stop_stream()

    sys.exit(0)


if __name__ == '__main__':
    # Инициализируем event loop для дискуссий
    discussion_loop_event_loop = asyncio.new_event_loop()

    # Регистрируем обработчики сигналов
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    print("=" * 70)
    print("🤖 AI AGENTS STREAM WITH FFMPEG")
    print("=" * 70)

    # Информация о зависимостях
    print(f"📦 Версии зависимостей:")
    print(f"   Flask: 2.3.0")
    print(f"   Flask-SocketIO: 5.3.0")
    print(f"   OpenAI: >=1.3.0")
    print(f"   Edge TTS: >=6.1.9")
    print(f"   FFmpeg: системный")

    if YOUTUBE_OAUTH_AVAILABLE:
        print(f"   YouTube API: Доступен ✅")
    else:
        print(f"   YouTube API: Не доступен (используйте ручной Stream Key)")

    # Создаем директории
    os.makedirs("stream_ui", exist_ok=True)
    os.makedirs("audio_cache", exist_ok=True)

    # Очищаем старые аудио файлы
    if os.path.exists('audio_cache'):
        try:
            for filename in os.listdir('audio_cache'):
                file_path = os.path.join('audio_cache', filename)
                try:
                    if os.path.isfile(file_path) or os.path.islink(file_path):
                        os.unlink(file_path)
                    elif os.path.isdir(file_path):
                        shutil.rmtree(file_path)
                except Exception as e:
                    logger.warning(f"Не удалось удалить {file_path}: {e}")
            print("✅ Очищена директория audio_cache")
        except Exception as e:
            logger.error(f"Ошибка очистки audio_cache: {e}")
    else:
        os.makedirs('audio_cache', exist_ok=True)

    # Создаем UI если его нет
    ui_dir = "stream_ui"
    if not os.path.exists(ui_dir):
        os.makedirs(ui_dir, exist_ok=True)

    # Создаем основной index.html
    index_path = os.path.join(ui_dir, "index.html")
    if not os.path.exists(index_path):
        print("📁 Создаю основной UI...")
        with open(index_path, 'w', encoding='utf-8') as f:
            f.write('''<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <title>AI Stream Control</title>
    <style>
        body { font-family: Arial; padding: 20px; max-width: 1200px; margin: 0 auto; }
        .panel { background: #f5f5f5; padding: 20px; border-radius: 10px; margin: 20px 0; }
        .status { padding: 10px; margin: 10px 0; border-radius: 5px; }
        .online { background: #d4edda; }
        .offline { background: #f8d7da; }
        .info { background: #d1ecf1; }
        button { margin: 5px; padding: 10px 20px; border: none; cursor: pointer; border-radius: 5px; }
        .btn-primary { background: #007bff; color: white; }
        .btn-success { background: #28a745; color: white; }
        .btn-danger { background: #dc3545; color: white; }
        .agent-card { display: inline-block; padding: 15px; margin: 10px; border-radius: 8px; }
        .speaking { border: 3px solid #28a745; }
        .message { background: white; padding: 10px; margin: 5px 0; border-radius: 5px; border-left: 4px solid #007bff; }
    </style>
</head>
<body>
    <h1>🤖 AI Stream Control Panel</h1>

    <div class="panel">
        <h2>📊 Статус системы</h2>
        <div id="system-status" class="status offline">Загрузка...</div>
        <div id="agents-container"></div>
    </div>

    <div class="panel">
        <h2>🎬 Управление стримом</h2>
        <div>
            <button class="btn-primary" onclick="manualStream()">🔑 Ручной запуск стрима</button>
            <button class="btn-success" onclick="youtubeApiStream()">🚀 Автоматический YouTube стрим</button>
            <button class="btn-danger" onclick="stopStream()">🛑 Остановить стрим</button>
            <a href="/youtube-control" target="_blank">
                <button class="btn-primary">⚙️ YouTube API Control</button>
            </a>
        </div>
    </div>

    <div class="panel">
        <h2>💬 Управление дискуссией</h2>
        <div id="topic-display">Тема: <span id="current-topic">Загрузка...</span></div>
        <button class="btn-primary" onclick="startDiscussion()">▶️ Начать дискуссию</button>
        <button class="btn-danger" onclick="stopDiscussion()">⏸️ Остановить дискуссию</button>
        <button class="btn-primary" onclick="changeTopic()">🔄 Сменить тему</button>
        <button class="btn-primary" onclick="testAudio()">🔊 Тест звука</button>
    </div>

    <div class="panel">
        <h2>📨 Сообщения</h2>
        <div id="messages-container"></div>
    </div>

    <script src="https://cdn.socket.io/4.5.4/socket.io.min.js"></script>
    <script>
        const socket = io();

        socket.on('connected', function(data) {
            updateSystemStatus(data);
            updateAgents(data.agents);
            document.getElementById('current-topic').textContent = data.topic;
        });

        socket.on('update', function(data) {
            updateSystemStatus(data);
            updateAgents(data.agents);
            document.getElementById('current-topic').textContent = data.topic;
        });

        socket.on('stream_connected', function(data) {
            alert('✅ Стрим успешно подключен к YouTube!');
            document.getElementById('system-status').className = 'status online';
            document.getElementById('system-status').innerHTML = 'Стрим активен и подключен к YouTube';
        });

        socket.on('new_message', function(data) {
            addMessage(data);
        });

        socket.on('agent_start_speaking', function(data) {
            highlightAgent(data.agent_id, true);
        });

        socket.on('agent_stop_speaking', function(data) {
            highlightAgent(data.agent_id, false);
        });

        function updateSystemStatus(data) {
            const statusDiv = document.getElementById('system-status');
            let html = `<strong>Статус:</strong> `;

            if(data.stream_status.is_streaming) {
                statusDiv.className = 'status online';
                html += `Стрим активен (PID: ${data.stream_status.pid})<br>`;
                html += `<strong>RTMP URL:</strong> ${data.stream_status.rtmp_url || 'Не указан'}<br>`;
            } else {
                statusDiv.className = 'status offline';
                html += `Стрим не активен<br>`;
            }

            html += `<strong>Агентов:</strong> ${data.agents.length}<br>`;
            html += `<strong>Сообщений:</strong> ${data.stats.message_count}<br>`;
            html += `<strong>Раунд:</strong> ${data.stats.discussion_round}`;

            statusDiv.innerHTML = html;
        }

        function updateAgents(agents) {
            const container = document.getElementById('agents-container');
            let html = '';

            agents.forEach(agent => {
                html += `<div class="agent-card ${agent.is_speaking ? 'speaking' : ''}" 
                         style="background: ${agent.color}; color: white; min-width: 200px;">
                    <strong>${agent.name}</strong><br>
                    <small>${agent.expertise}</small><br>
                    <span>Сообщений: ${agent.message_count}</span>
                    ${agent.is_speaking ? '<br><span>🎤 Говорит</span>' : ''}
                </div>`;
            });

            container.innerHTML = html;
        }

        function addMessage(data) {
            const container = document.getElementById('messages-container');
            const messageDiv = document.createElement('div');
            messageDiv.className = 'message';
            messageDiv.innerHTML = `
                <strong>${data.agent_name}</strong> (${data.expertise})<br>
                ${data.message}<br>
                <small>${new Date(data.timestamp).toLocaleTimeString()}</small>
            `;
            container.insertBefore(messageDiv, container.firstChild);
        }

        function highlightAgent(agentId, isSpeaking) {
            const agents = document.querySelectorAll('.agent-card');
            agents.forEach(card => {
                if(card.textContent.includes(agentId)) {
                    if(isSpeaking) {
                        card.classList.add('speaking');
                    } else {
                        card.classList.remove('speaking');
                    }
                }
            });
        }

        function manualStream() {
            const key = prompt('Введите YouTube Stream Key:');
            if(key) {
                fetch('/api/start_stream', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({stream_key: key})
                })
                .then(res => res.json())
                .then(data => {
                    if(data.status === 'started') {
                        alert('✅ Стрим запущен!');
                    } else {
                        alert('❌ Ошибка: ' + data.message);
                    }
                });
            }
        }

        function youtubeApiStream() {
            if(!confirm('Запустить автоматический YouTube стрим через API?\n(Требуется client_secrets.json)')) {
                return;
            }

            const title = prompt('Название трансляции:', '🤖 AI Agents Live: Научные дебаты ИИ');
            if(title) {
                fetch('/api/start_youtube_stream', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({title: title})
                })
                .then(res => res.json())
                .then(data => {
                    if(data.status === 'started') {
                        alert(`✅ YouTube трансляция создана!\nСмотреть: ${data.watch_url}`);
                    } else {
                        alert('❌ Ошибка: ' + data.message);
                    }
                });
            }
        }

        function stopStream() {
            if(confirm('Остановить стрим?')) {
                fetch('/api/stop_stream', {
                    method: 'POST'
                })
                .then(res => res.json())
                .then(data => {
                    if(data.status === 'stopped') {
                        alert('✅ Стрим остановлен');
                    } else {
                        alert('❌ Ошибка: ' + data.message);
                    }
                });
            }
        }

        function startDiscussion() {
            fetch('/api/control', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({action: 'start_discussion'})
            })
            .then(res => res.json())
            .then(data => {
                if(data.status === 'started') {
                    alert('✅ Дискуссия начата');
                }
            });
        }

        function stopDiscussion() {
            fetch('/api/control', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({action: 'stop_discussion'})
            })
            .then(res => res.json())
            .then(data => {
                if(data.status === 'stopped') {
                    alert('✅ Дискуссия остановлена');
                }
            });
        }

        function changeTopic() {
            fetch('/api/control', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({action: 'change_topic'})
            })
            .then(res => res.json())
            .then(data => {
                if(data.status === 'changed') {
                    alert('✅ Тема изменена: ' + data.topic);
                }
            });
        }

        function testAudio() {
            const text = prompt('Текст для теста звука:', 'Привет! Это тест звука на YouTube стриме.');
            if(text) {
                fetch('/api/test_audio', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({text: text, voice: 'male_ru'})
                })
                .then(res => res.json())
                .then(data => {
                    alert(data.message);
                });
            }
        }

        // Автоматическое обновление статуса
        setInterval(() => {
            fetch('/api/stream_status')
            .then(res => res.json())
            .then(data => {
                socket.emit('request_update');
            });
        }, 5000);
    </script>
</body>
</html>''')

    # Создаем страницу управления YouTube API
    youtube_control_path = os.path.join(ui_dir, "youtube_control.html")
    if not os.path.exists(youtube_control_path):
        print("📁 Создаю YouTube API UI...")
        with open(youtube_control_path, 'w', encoding='utf-8') as f:
            f.write('''<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <title>YouTube API Control</title>
    <style>
        body { font-family: Arial; padding: 20px; max-width: 800px; margin: 0 auto; }
        .panel { background: #f5f5f5; padding: 20px; border-radius: 10px; margin: 20px 0; }
        .btn { background: #4285f4; color: white; border: none; padding: 10px 20px; margin: 5px; cursor: pointer; border-radius: 5px; }
        .btn:hover { background: #3367d6; }
        .btn-danger { background: #ea4335; }
        .btn-success { background: #34a853; }
        .status { padding: 10px; border-radius: 5px; margin: 10px 0; }
        .online { background: #d4edda; }
        .offline { background: #f8d7da; }
        .info { background: #d1ecf1; }
        input, textarea { width: 100%; padding: 8px; margin: 5px 0; }
    </style>
</head>
<body>
    <h1>🎬 YouTube API Control Panel</h1>

    <div id="youtube-status" class="status offline">
        YouTube API: Проверка доступности...
    </div>

    <div class="panel">
        <h3>Автоматический запуск YouTube трансляции</h3>
        <div>
            <label>Название трансляции:</label><br>
            <input type="text" id="stream-title" value="🤖 AI Agents Live: Научные дебаты ИИ">
        </div>
        <div>
            <label>Описание:</label><br>
            <textarea id="stream-description" rows="8"></textarea>
        </div>
        <button class="btn btn-success" onclick="startYoutubeStream()">🎬 Создать YouTube трансляцию</button>
        <button class="btn" onclick="checkYouTubeStatus()">🔄 Проверить статус</button>
    </div>

    <div class="panel" id="stream-controls" style="display: none;">
        <h3>Управление трансляцией</h3>
        <div id="stream-info" class="status info">Информация не доступна</div>
        <button class="btn" onclick="updateStreamInfo()">✏️ Обновить информацию</button>
        <button class="btn" onclick="getChatId()">💬 Получить ID чата</button>
        <button class="btn btn-danger" onclick="endYoutubeStream()">🛑 Завершить трансляцию</button>
    </div>

    <div class="panel">
        <h3>Статус FFmpeg</h3>
        <div id="ffmpeg-status" class="status">Загрузка...</div>
        <button class="btn" onclick="checkFFmpegStatus()">🔄 Обновить статус FFmpeg</button>
    </div>

    <script>
        // Автоматически заполняем описание
        document.getElementById('stream-description').value = `Автономные ИИ-агенты обсуждают науку в реальном времени.

Участники:
• Доктор Алексей Волков - Квантовая физика
• Профессор Мария Соколова - Нейробиология
• Доктор Иван Петров - Климатология
• Исследователь София Ковалева - ИИ и робототехника

Темы: Искусственный интеллект, квантовые вычисления, изменение климата, нейроинтерфейсы.

Стрим создан автоматически с помощью Python и OpenAI GPT-4.`;

        // Проверяем доступность YouTube API при загрузке
        window.addEventListener('load', function() {
            checkYouTubeStatus();
            checkFFmpegStatus();
        });

        function checkYouTubeStatus() {
            fetch('/api/youtube_control', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({action: 'get_info'})
            })
            .then(res => res.json())
            .then(data => {
                const statusDiv = document.getElementById('youtube-status');
                if(data.status === 'success') {
                    statusDiv.className = 'status online';
                    statusDiv.innerHTML = 'YouTube API: Доступен';
                    document.getElementById('stream-controls').style.display = 'block';
                    updateStreamInfoDisplay(data);
                } else {
                    statusDiv.className = 'status offline';
                    statusDiv.innerHTML = 'YouTube API: Не доступен. Установите client_secrets.json';
                }
            })
            .catch(err => {
                document.getElementById('youtube-status').className = 'status offline';
                document.getElementById('youtube-status').innerHTML = 'YouTube API: Ошибка подключения';
            });
        }

        function startYoutubeStream() {
            const title = document.getElementById('stream-title').value;
            const description = document.getElementById('stream-description').value;

            if(!title.trim()) {
                alert('Введите название трансляции');
                return;
            }

            fetch('/api/start_youtube_stream', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({title, description})
            })
            .then(res => res.json())
            .then(data => {
                if(data.status === 'started') {
                    alert('✅ YouTube трансляция создана!\\nСсылка: ' + data.watch_url);
                    document.getElementById('stream-controls').style.display = 'block';
                    updateStreamInfoDisplay({
                        status: 'success',
                        broadcast_id: data.broadcast_id,
                        stream_id: data.stream_id,
                        is_live: true,
                        stream_info: {
                            stream_key: data.stream_key,
                            rtmp_url: data.rtmp_url
                        }
                    });
                } else {
                    alert('❌ Ошибка: ' + data.message);
                }
            })
            .catch(err => {
                alert('❌ Ошибка сети: ' + err);
            });
        }

        function updateStreamInfo() {
            const title = document.getElementById('stream-title').value;
            const description = document.getElementById('stream-description').value;

            fetch('/api/youtube_control', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({
                    action: 'update_info',
                    title: title,
                    description: description
                })
            })
            .then(res => res.json())
            .then(data => {
                if(data.status === 'updated') {
                    alert('✅ Информация обновлена');
                } else {
                    alert('❌ Ошибка обновления');
                }
            });
        }

        function getChatId() {
            fetch('/api/youtube_control', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({action: 'get_chat_id'})
            })
            .then(res => res.json())
            .then(data => {
                if(data.chat_id) {
                    alert('💬 ID чата: ' + data.chat_id);
                } else {
                    alert('❌ Чат не найден');
                }
            });
        }

        function endYoutubeStream() {
            if(confirm('Завершить YouTube трансляцию?')) {
                fetch('/api/youtube_control', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({action: 'end_stream'})
                })
                .then(res => res.json())
                .then(data => {
                    if(data.status === 'ended') {
                        alert('✅ Трансляция завершена');
                        document.getElementById('stream-controls').style.display = 'none';
                        document.getElementById('stream-info').innerHTML = 'Информация не доступна';
                    } else {
                        alert('❌ Ошибка завершения');
                    }
                });
            }
        }

        function updateStreamInfoDisplay(data) {
            const infoDiv = document.getElementById('stream-info');
            let html = '';

            if(data.broadcast_id) {
                html += `<strong>ID трансляции:</strong> ${data.broadcast_id}<br>`;
                html += `<strong>Статус:</strong> ${data.is_live ? 'В эфире 🟢' : 'Не в эфире 🔴'}<br>`;
                html += `<strong>Stream Key:</strong> ${data.stream_info?.stream_key || 'Не указан'}<br>`;
                html += `<strong>RTMP URL:</strong> ${data.stream_info?.rtmp_url || 'Не указан'}`;
            }

            infoDiv.innerHTML = html || 'Информация не доступна';
        }

        function checkFFmpegStatus() {
            fetch('/api/stream_status')
            .then(res => res.json())
            .then(data => {
                const statusDiv = document.getElementById('ffmpeg-status');
                if(data.is_streaming) {
                    statusDiv.className = 'status online';
                    statusDiv.innerHTML = `FFmpeg: Работает (PID: ${data.pid})<br>
                                           RTMP: ${data.rtmp_url || 'Не указан'}`;
                } else {
                    statusDiv.className = 'status offline';
                    statusDiv.innerHTML = 'FFmpeg: Не запущен';
                }
            })
            .catch(err => {
                document.getElementById('ffmpeg-status').innerHTML = 'FFmpeg: Ошибка проверки';
            });
        }
    </script>
</body>
</html>''')

    # Запускаем поток дискуссии
    print("🔄 Запуск цикла дискуссии...")
    discussion_thread = threading.Thread(
        target=lambda: discussion_loop_event_loop.run_until_complete(discussion_loop()),
        daemon=True
    )
    discussion_thread.start()

    print("🚀 Запуск веб-сервера...")
    print("🌐 Основной интерфейс: http://localhost:5000")
    print("🎬 YouTube API интерфейс: http://localhost:5000/youtube-control")
    print("=" * 70)

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