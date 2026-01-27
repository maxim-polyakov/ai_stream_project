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

class FFmpegStreamManager:
    def __init__(self):
        self.stream_process = None
        self.is_streaming = False
        self.stream_key = None
        self.rtmp_url = None
        self.ffmpeg_pid = None
        self.last_error = None
        self.stream_start_time = None

        # Create temp directories
        os.makedirs('temp_videos', exist_ok=True)
        os.makedirs('audio_cache', exist_ok=True)

    def start_stream(self) -> Dict[str, Any]:
        """Start FFmpeg stream - SIMPLE WORKING VERSION"""
        if not self.stream_key:
            return {'success': False, 'error': 'Stream Key not set'}

        try:
            # SIMPLE FFMPEG COMMAND - FIXED SYNTAX
            # Use escape sequences for drawtext filter
            ffmpeg_cmd = [
                'ffmpeg',
                '-re',
                '-f', 'lavfi',
                '-i', "color=c=black:s=1280x720:r=30[bg];" +
                      "[bg]drawtext=text='AI\\ Live\\ Stream':" +
                      "fontcolor=white:fontsize=32:" +
                      "x=(w-text_w)/2:y=(h-text_h)/2:box=1:boxcolor=black@0.5",
                '-f', 'lavfi',
                '-i', 'anullsrc=r=44100:cl=stereo',
                '-c:v', 'libx264',
                '-preset', 'veryfast',
                '-tune', 'zerolatency',
                '-pix_fmt', 'yuv420p',
                '-g', '60',
                '-b:v', '2500k',
                '-maxrate', '3000k',
                '-bufsize', '5000k',
                '-c:a', 'aac',
                '-b:a', '128k',
                '-ar', '44100',
                '-ac', '2',
                '-f', 'flv',
                self.rtmp_url
            ]

            logger.info("🚀 Starting FFmpeg stream")

            self.stream_process = subprocess.Popen(
                ffmpeg_cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                bufsize=1,
                universal_newlines=False
            )

            self.is_streaming = True
            self.ffmpeg_pid = self.stream_process.pid
            self.stream_start_time = time.time()

            # Start monitoring in background
            threading.Thread(target=self._monitor_stream, daemon=True).start()

            time.sleep(2)  # Give FFmpeg time to start

            if self.stream_process.poll() is None:
                logger.info(f"✅ FFmpeg stream started (PID: {self.ffmpeg_pid})")
                return {
                    'success': True,
                    'pid': self.ffmpeg_pid,
                    'message': 'Stream started successfully'
                }
            else:
                return {'success': False, 'error': 'FFmpeg failed to start'}

        except Exception as e:
            logger.error(f"❌ FFmpeg error: {e}")
            return {'success': False, 'error': str(e)}

    def _monitor_stream(self):
        """Monitor FFmpeg process"""
        try:
            # Read stderr for diagnostics
            while self.is_streaming and self.stream_process:
                if self.stream_process.poll() is not None:
                    break

                try:
                    line = self.stream_process.stderr.readline()
                    if line:
                        line = line.decode('utf-8', errors='ignore').strip()
                        if line:
                            # Log errors
                            if any(keyword in line.lower() for keyword in ['error', 'fail']):
                                logger.error(f"FFmpeg: {line}")
                                self.last_error = line
                            # Log connection success
                            elif 'rtmp' in line.lower() and 'connected' in line.lower():
                                logger.info(f"✅ Connected to YouTube")
                except:
                    pass

                time.sleep(0.1)

        except Exception as e:
            logger.error(f"Monitor error: {e}")

    def play_audio(self, audio_file: str, agent_name: str = "AI") -> bool:
        """Play audio file in stream - SIMPLE METHOD"""
        if not os.path.exists(audio_file):
            return False

        if not self.is_streaming:
            return False

        try:
            # Get audio duration
            duration = self._get_audio_duration(audio_file)

            # Create a simple video with the agent name and audio
            temp_video = self._create_simple_video(agent_name, audio_file)
            if not temp_video:
                return False

            # Send video+audio to stream
            cmd = [
                'ffmpeg',
                '-re',
                '-i', temp_video,
                '-c:v', 'libx264',
                '-preset', 'veryfast',
                '-pix_fmt', 'yuv420p',
                '-c:a', 'aac',
                '-b:a', '128k',
                '-f', 'flv',
                self.rtmp_url
            ]

            logger.info(f"🎵 Playing audio: {agent_name} ({duration:.1f}s)")

            # Run in separate thread
            def send_audio():
                try:
                    process = subprocess.Popen(
                        cmd,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL
                    )

                    # Wait for duration + buffer
                    time.sleep(duration + 2)

                    # Cleanup
                    if process.poll() is None:
                        process.terminate()

                    # Remove temp file
                    try:
                        os.remove(temp_video)
                    except:
                        pass

                except Exception as e:
                    logger.error(f"Audio send error: {e}")

            threading.Thread(target=send_audio, daemon=True).start()
            return True

        except Exception as e:
            logger.error(f"Play audio error: {e}")
            return False

    def _create_simple_video(self, agent_name: str, audio_file: str) -> Optional[str]:
        """Create simple video with text and audio"""
        try:
            temp_video = f'temp_videos/{agent_name}_{int(time.time())}.mp4'

            # SIMPLE COMMAND - NO COMPLEX FILTERS
            cmd = [
                'ffmpeg',
                '-f', 'lavfi',
                '-i', f"color=c=black:s=1280x720:r=30",
                '-i', audio_file,
                '-vf',
                f"drawtext=text='{agent_name} Speaking':fontcolor=white:fontsize=36:x=(w-text_w)/2:y=(h-text_h)/2",
                '-c:v', 'libx264',
                '-preset', 'fast',
                '-pix_fmt', 'yuv420p',
                '-c:a', 'aac',
                '-shortest',
                '-y',
                temp_video
            ]

            result = subprocess.run(cmd, capture_output=True, timeout=20)

            if result.returncode == 0 and os.path.exists(temp_video):
                return temp_video
            else:
                logger.error(f"Video creation failed: {result.stderr[:100]}")
                return None

        except Exception as e:
            logger.error(f"Create video error: {e}")
            return None

    def _get_audio_duration(self, audio_file: str) -> float:
        """Get audio duration"""
        try:
            cmd = [
                'ffprobe',
                '-v', 'error',
                '-show_entries', 'format=duration',
                '-of', 'default=noprint_wrappers=1:nokey=1',
                audio_file
            ]
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
            return float(result.stdout.strip() or 5.0)
        except:
            return 5.0

    def stop_stream(self):
        """Stop stream"""
        if self.stream_process:
            self.is_streaming = False
            self.stream_process.terminate()
            time.sleep(1)
            if self.stream_process.poll() is None:
                self.stream_process.kill()
            logger.info("🛑 Stream stopped")

# ========== EDGE TTS MANAGER ==========

class EdgeTTSManager:
    def __init__(self, ffmpeg_manager: FFmpegStreamManager = None):
        self.cache_dir = 'audio_cache'
        os.makedirs(self.cache_dir, exist_ok=True)
        self.ffmpeg_manager = ffmpeg_manager

        # Voice mapping
        self.voice_map = {
            'male_ru': 'ru-RU-DmitryNeural',
            'female_ru': 'ru-RU-SvetlanaNeural'
        }

        # Initialize pygame for local playback
        try:
            pygame.mixer.init()
            self.pygame_available = True
        except:
            self.pygame_available = False

    async def text_to_speech(self, text: str, voice_id: str = 'male_ru', agent_name: str = "") -> Optional[str]:
        """Generate TTS audio"""
        try:
            voice_name = self.voice_map.get(voice_id, 'ru-RU-DmitryNeural')

            # Create cache filename
            text_hash = hashlib.md5(f"{text}_{voice_id}".encode()).hexdigest()
            cache_file = os.path.join(self.cache_dir, f"{agent_name}_{text_hash}.mp3")

            # Check cache
            if os.path.exists(cache_file):
                logger.info(f"♻️ Using cached audio")
                return cache_file

            # Generate new audio
            logger.info(f"🔊 Generating TTS for {agent_name}")

            communicate = edge_tts.Communicate(
                text=text,
                voice=voice_name,
                rate='+0%',
                pitch='+0Hz'
            )

            await communicate.save(cache_file)

            if os.path.exists(cache_file):
                logger.info(f"💾 Audio saved: {cache_file}")
                return cache_file
            else:
                return None

        except Exception as e:
            logger.error(f"TTS error: {e}")
            return None

    async def speak_in_stream(self, text: str, voice_id: str = 'male_ru', agent_name: str = "") -> bool:
        """Generate and play TTS in stream"""
        try:
            # Generate audio
            audio_file = await self.text_to_speech(text, voice_id, agent_name)
            if not audio_file:
                return False

            # Play locally for testing
            if self.pygame_available:
                try:
                    pygame.mixer.music.load(audio_file)
                    pygame.mixer.music.play()

                    # Wait for playback
                    duration = self._get_duration(audio_file)
                    await asyncio.sleep(duration)
                except:
                    pass

            # Send to stream
            if self.ffmpeg_manager and self.ffmpeg_manager.is_streaming:
                success = self.ffmpeg_manager.play_audio(audio_file, agent_name)
                return success
            else:
                return False

        except Exception as e:
            logger.error(f"Speak error: {e}")
            return False

    def _get_duration(self, audio_file: str) -> float:
        """Get audio duration"""
        try:
            cmd = [
                'ffprobe',
                '-v', 'error',
                '-show_entries', 'format=duration',
                '-of', 'default=noprint_wrappers=1:nokey=1',
                audio_file
            ]
            result = subprocess.run(cmd, capture_output=True, text=True)
            return float(result.stdout.strip() or 3.0)
        except:
            return 3.0

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
    def __init__(self, ffmpeg_manager: FFmpegStreamManager = None):
        self.agents = self._init_agents()
        self.tts_manager = EdgeTTSManager(ffmpeg_manager)
        self.ffmpeg_manager = ffmpeg_manager
        self.current_topic = ""
        self.is_discussion_active = False
        self.active_agent = None

    def _init_agents(self):
        """Initialize agents from config"""
        agents = []
        for agent_config in Config.AGENTS:
            agents.append({
                'id': agent_config["id"],
                'name': agent_config["name"],
                'expertise': agent_config["expertise"],
                'voice': agent_config["voice"]
            })
        return agents

    async def run_discussion_round(self):
        """Run one discussion round - SIMPLIFIED"""
        if self.is_discussion_active:
            return

        self.is_discussion_active = True

        try:
            # Select topic if needed
            if not self.current_topic:
                self.current_topic = random.choice(Config.TOPICS)

            # Shuffle agents
            speaking_order = random.sample(self.agents, len(self.agents))

            for agent in speaking_order:
                if not self.is_discussion_active:
                    break

                # Agent starts speaking
                self.active_agent = agent['id']

                # Generate response
                message = await self._generate_response(agent)

                # Send to UI
                socketio.emit('agent_speaking', {
                    'agent_id': agent['id'],
                    'agent_name': agent['name'],
                    'message': message
                })

                # Generate and play TTS
                success = await self.tts_manager.speak_in_stream(
                    text=message,
                    voice_id=agent['voice'],
                    agent_name=agent['name']
                )

                # Wait based on success
                if success:
                    await asyncio.sleep(3)  # Wait for audio
                else:
                    await asyncio.sleep(2)  # Wait shorter

                # Agent stops speaking
                self.active_agent = None

                # Pause between agents
                if agent != speaking_order[-1]:
                    await asyncio.sleep(1)

        except Exception as e:
            logger.error(f"Discussion error: {e}")
        finally:
            self.is_discussion_active = False

    async def _generate_response(self, agent: Dict) -> str:
        """Generate AI response"""
        try:
            if not openai_client:
                # Demo response
                return f"Как {agent['name']}, эксперт в {agent['expertise']}, я считаю что {self.current_topic} важно изучать."

            # OpenAI API call
            response = await asyncio.to_thread(
                openai_client.chat.completions.create,
                model=Config.OPENAI_MODEL,
                messages=[
                    {"role": "system", "content": f"Ты {agent['name']}, эксперт в {agent['expertise']}."},
                    {"role": "user", "content": f"Что ты думаешь о {self.current_topic}? Ответь кратко."}
                ],
                temperature=0.7,
                max_tokens=150
            )

            return response.choices[0].message.content

        except Exception as e:
            logger.error(f"OpenAI error: {e}")
            return f"{agent['name']}: Мне нужно подумать об этом."

# ========== ГЛОБАЛЬНЫЕ ОБЪЕКТЫ ==========

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