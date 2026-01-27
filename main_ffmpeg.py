#!/usr/bin/env python3
"""
🤖 AI Stream с FFmpeg стримингом на YouTube
Версия с OAuth 2.0 для YouTube API
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

# Проверяем импорты
try:
    import openai
    import edge_tts
    import pygame

    # Попробуем импортировать pyaudio
    try:
        import pyaudio

        PYTHON_AUDIO_AVAILABLE = True
    except ImportError:
        PYTHON_AUDIO_AVAILABLE = False
        print("⚠️ PyAudio не установлен. Используем тихий звук.")

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


# ========== FFMPEG STREAM MANAGER ==========

class FFmpegStreamManager:
    """Управление FFmpeg стримом на YouTube"""

    def __init__(self):
        self.stream_process = None
        self.is_streaming = False
        self.stream_key = None
        self.rtmp_url = None
        self.ffmpeg_pid = None
        self.video_source = "black"
        self.audio_mixer = None
        self.current_audio_process = None

    def set_stream_key(self, stream_key: str):
        """Установка ключа стрима"""
        self.stream_key = stream_key
        self.rtmp_url = f"rtmp://a.rtmp.youtube.com/live2/{stream_key}"
        logger.info(f"🔑 Stream Key установлен: {stream_key[:10]}...")
        return True

    def start_stream(self, use_audio: bool = True) -> Dict[str, Any]:
        """Запуск FFmpeg стрима - ПРОСТОЙ РАБОЧИЙ ВАРИАНТ"""
        if not self.stream_key:
            logger.error("❌ Stream Key не установлен!")
            return {'success': False, 'error': 'Stream Key не установлен'}

        try:
            # ПРОСТАЯ РАБОЧАЯ КОМАНДА FFMPEG
            ffmpeg_cmd = [
                'ffmpeg',
                '-re',  # Реальное время
                '-f', 'lavfi',
                '-i',
                'color=c=black:s=1920x1080:r=30,drawtext=text=AI\ Live\ Stream:fontcolor=white:fontsize=48:x=(w-text_w)/2:y=(h-text_h)/2',
                '-f', 'lavfi',
                '-i', 'anullsrc=channel_layout=stereo:sample_rate=44100',
                '-c:v', 'libx264',
                '-preset', 'veryfast',
                '-tune', 'zerolatency',
                '-pix_fmt', 'yuv420p',
                '-g', '60',
                '-b:v', '3000k',
                '-maxrate', '3500k',
                '-bufsize', '6000k',
                '-c:a', 'aac',
                '-b:a', '128k',
                '-ar', '44100',
                '-ac', '2',
                '-f', 'flv',
                self.rtmp_url
            ]

            logger.info(f"🚀 Запуск FFmpeg: {' '.join(ffmpeg_cmd)}")

            # Запускаем FFmpeg
            self.stream_process = subprocess.Popen(
                ffmpeg_cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=0
            )

            self.is_streaming = True
            self.ffmpeg_pid = self.stream_process.pid

            # Запускаем мониторинг
            threading.Thread(target=self._monitor_ffmpeg, daemon=True).start()

            logger.info(f"🎬 FFmpeg стрим запущен (PID: {self.ffmpeg_pid})")

            return {
                'success': True,
                'pid': self.ffmpeg_pid,
                'stream_key': self.stream_key,
                'rtmp_url': self.rtmp_url,
                'message': 'FFmpeg стрим запущен. Ждите 30-60 секунд для начала трансляции на YouTube.'
            }

        except Exception as e:
            logger.error(f"❌ Ошибка запуска FFmpeg: {e}", exc_info=True)
            return {'success': False, 'error': str(e)}

    def play_audio_file(self, audio_file: str) -> bool:
        """Воспроизведение аудио файла в стриме - ОСНОВНОЙ РАБОЧИЙ МЕТОД"""
        if not os.path.exists(audio_file):
            logger.error(f"❌ Аудио файл не найден: {audio_file}")
            return False

        if not self.rtmp_url:
            logger.error("❌ RTMP URL не установлен")
            return False

        try:
            # ПРОСТОЙ и РАБОЧИЙ способ: временная замена основного потока

            # 1. Сначала получаем длительность аудио
            duration = self._get_audio_duration(audio_file)

            # 2. Создаем временный видеофайл с тем же фоном и аудио
            temp_video = self._create_video_with_audio(audio_file)

            if not temp_video:
                return False

            logger.info(f"▶️ Воспроизведение: {os.path.basename(audio_file)} ({duration:.1f} сек)")

            # 3. Отправляем временный видео+аудио файл
            cmd = [
                'ffmpeg',
                '-re',
                '-i', temp_video,
                '-c:v', 'libx264',
                '-preset', 'veryfast',
                '-tune', 'zerolatency',
                '-pix_fmt', 'yuv420p',
                '-c:a', 'aac',
                '-b:a', '128k',
                '-f', 'flv',
                self.rtmp_url
            ]

            logger.info(f"📤 Отправка аудио+видео в стрим")

            # Останавливаем текущий процесс
            if self.stream_process:
                self.stream_process.terminate()
                time.sleep(0.5)

            # Запускаем новый процесс
            self.current_audio_process = subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE
            )

            # Ждем завершения аудио
            time.sleep(duration + 1)

            # Останавливаем аудио процесс
            if self.current_audio_process:
                self.current_audio_process.terminate()

            # Восстанавливаем основной поток
            self.start_stream()

            # Удаляем временный файл
            try:
                os.remove(temp_video)
            except:
                pass

            logger.info(f"✅ Аудио воспроизведено")
            return True

        except Exception as e:
            logger.error(f"❌ Ошибка воспроизведения аудио: {e}", exc_info=True)
            return False

    def _get_audio_duration(self, audio_file: str) -> float:
        """Получение длительности аудио"""
        try:
            result = subprocess.run([
                'ffprobe',
                '-v', 'error',
                '-show_entries', 'format=duration',
                '-of', 'default=noprint_wrappers=1:nokey=1',
                audio_file
            ], capture_output=True, text=True)

            return float(result.stdout.strip())
        except:
            return 5.0  # По умолчанию 5 секунд

    def _create_video_with_audio(self, audio_file: str) -> Optional[str]:
        """Создание временного видео файла с аудио"""
        import tempfile

        try:
            # Создаем временный файл
            temp_dir = 'temp_videos'
            os.makedirs(temp_dir, exist_ok=True)

            temp_video = os.path.join(temp_dir, f'video_audio_{int(time.time())}.mp4')

            # Получаем текст из имени файла
            filename = os.path.basename(audio_file)
            agent_name = filename.split('_')[0] if '_' in filename else "AI Agent"

            # Команда для создания видео с текстом и аудио
            cmd = [
                'ffmpeg',
                '-f', 'lavfi',
                '-i',
                f'color=c=black:s=1920x1080:r=30,drawtext=text={agent_name}\\ говорит:fontcolor=white:fontsize=60:x=(w-text_w)/2:y=(h-text_h)/2',
                '-i', audio_file,
                '-c:v', 'libx264',
                '-preset', 'veryfast',
                '-tune', 'zerolatency',
                '-pix_fmt', 'yuv420p',
                '-c:a', 'aac',
                '-shortest',
                '-y',  # Перезаписать без подтверждения
                temp_video
            ]

            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True
            )

            if result.returncode == 0 and os.path.exists(temp_video):
                return temp_video

            return None

        except Exception as e:
            logger.error(f"❌ Ошибка создания видео: {e}")
            return None

    def stop_stream(self):
        """Остановка стрима"""
        if self.stream_process:
            logger.info("🛑 Остановка FFmpeg стрима...")

            if self.current_audio_process:
                self.current_audio_process.terminate()

            self.stream_process.terminate()
            self.stream_process.wait()
            self.is_streaming = False

            logger.info("✅ FFmpeg стрим остановлен")
            return True

        return True

# ========== EDGE TTS MANAGER ==========
class AudioVideoMixer:
    """Микширование аудио и видео в один поток"""

    def __init__(self, rtmp_url: str):
        self.rtmp_url = rtmp_url
        self.process = None
        self.audio_queue = []
        self.is_processing = False
        self.audio_fifo = None
        self.temp_dir = 'temp_audio'
        os.makedirs(self.temp_dir, exist_ok=True)

    def start(self):
        """Запуск микшера"""
        try:
            # Создаем FIFO для аудио
            import tempfile
            self.audio_fifo = os.path.join(tempfile.gettempdir(), f'audio_fifo_{int(time.time())}')
            os.mkfifo(self.audio_fifo)

            logger.info(f"🎵 Создан аудио FIFO: {self.audio_fifo}")

            # Команда FFmpeg для микширования
            # Используем amix фильтр для смешивания аудио потоков
            cmd = [
                'ffmpeg',
                '-re',
                '-f', 'lavfi',
                '-i',
                'color=c=black:s=1920x1080:r=30,drawtext=text=AI\\ Stream:fontcolor=white:fontsize=72:x=(w-text_w)/2:y=(h-text_h)/2',
                '-f', 'lavfi',
                '-i', f'aevalsrc=0:d=0.1[base]',
                '-filter_complex',
                '[1:a]aresample=async=1[a1];'  # Ресэмплинг базового аудио
                '[a1]amix=inputs=1:duration=longest[aout]',  # Готовим микшер
                '-map', '0:v',
                '-map', '[aout]',
                '-c:v', 'libx264',
                '-preset', 'veryfast',
                '-tune', 'zerolatency',
                '-pix_fmt', 'yuv420p',
                '-g', '60',
                '-b:v', '4500k',
                '-maxrate', '5000k',
                '-bufsize', '9000k',
                '-c:a', 'aac',
                '-b:a', '128k',
                '-ar', '44100',
                '-ac', '2',
                '-f', 'flv',
                self.rtmp_url
            ]

            logger.info(f"🔧 Запуск микшера: {' '.join(cmd[:10])}...")

            self.process = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE
            )

            # Запускаем обработчик очереди аудио
            threading.Thread(target=self._process_audio_queue, daemon=True).start()

            return True

        except Exception as e:
            logger.error(f"❌ Ошибка запуска микшера: {e}")
            return False

    def add_audio(self, audio_file: str):
        """Добавление аудио файла в очередь"""
        if os.path.exists(audio_file):
            self.audio_queue.append(audio_file)
            logger.info(f"➕ Аудио добавлено в очередь: {os.path.basename(audio_file)}")

    def _process_audio_queue(self):
        """Обработка очереди аудио"""
        while True:
            if self.audio_queue and self.process:
                audio_file = self.audio_queue.pop(0)
                self._inject_audio(audio_file)
            time.sleep(0.1)

    def _inject_audio(self, audio_file: str):
        """Инжекция аудио в работающий FFmpeg"""
        try:
            # Используем фильтр concat для добавления аудио
            # Это временное решение
            logger.info(f"🎵 Инжекция аудио: {os.path.basename(audio_file)}")

            # Временный файл для конкатенации
            concat_file = os.path.join(self.temp_dir, f'concat_{int(time.time())}.txt')
            with open(concat_file, 'w') as f:
                f.write(f"file '{os.path.abspath(audio_file)}'\n")

            # Используем ffmpeg для отправки аудио отдельно
            # ВРЕМЕННОЕ РЕШЕНИЕ: запускаем отдельный процесс
            cmd = [
                'ffmpeg',
                '-re',
                '-i', audio_file,
                '-c:a', 'aac',
                '-b:a', '128k',
                '-f', 'flv',
                self.rtmp_url
            ]

            process = subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL
            )

            # Ждем немного
            time.sleep(2)

            # Завершаем процесс
            process.terminate()

        except Exception as e:
            logger.error(f"❌ Ошибка инжекции аудио: {e}")

    def stop(self):
        """Остановка микшера"""
        if self.process:
            self.process.terminate()
            self.process.wait()

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

        try:
            pygame.mixer.init(frequency=44100, size=-16, channels=2, buffer=2048)
            self.pygame_available = True
        except:
            self.pygame_available = False
            logger.warning("⚠️ Pygame не доступен для локального воспроизведения")

        logger.info("Edge TTS Manager инициализирован")

    async def text_to_speech_and_stream(self, text: str, voice_id: str = 'male_ru', agent_name: str = "") -> Optional[
        str]:
        """Генерация аудио и отправка в стрим - УПРОЩЕННАЯ ВЕРСИЯ"""
        try:
            if voice_id not in self.voice_map:
                voice_id = 'male_ru'

            voice_name = self.voice_map[voice_id]

            # Хэш для имени файла
            text_hash = hashlib.md5(f"{text}_{voice_id}".encode()).hexdigest()
            cache_file = os.path.join(self.cache_dir, f"{agent_name}_{text_hash}.mp3")

            # Проверяем кэш
            if os.path.exists(cache_file):
                logger.info(f"♻️ Используем кэшированное аудио")
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
                logger.info(f"🔊 Генерация TTS: {agent_name}")

                communicate = edge_tts.Communicate(
                    text=text,
                    voice=voice_name,
                    rate=rate,
                    pitch=pitch
                )

                await communicate.save(cache_file)
                logger.info(f"💾 Аудио сохранено: {os.path.basename(cache_file)}")

            # ВАЖНО: Воспроизводим локально для тестирования
            if self.pygame_available:
                try:
                    pygame.mixer.music.load(cache_file)
                    pygame.mixer.music.play()
                    logger.info(f"🔊 Локальное воспроизведение")
                except Exception as e:
                    logger.warning(f"Не удалось воспроизвести локально: {e}")

            # ОТПРАВКА В СТРИМ - упрощенная версия
            if self.ffmpeg_manager and self.ffmpeg_manager.is_streaming:
                # Используем новый метод
                success = self.ffmpeg_manager.play_audio_file(cache_file)

                if success:
                    logger.info(f"📤 Аудио отправлено в стрим")
                else:
                    logger.error(f"❌ Не удалось отправить аудио в стрим")

            return cache_file

        except Exception as e:
            logger.error(f"❌ Ошибка Edge TTS: {e}", exc_info=True)
            return None

    async def _play_and_stream(self, audio_file: str):
        """Воспроизведение аудио и отправка в стрим - УПРОЩЕННАЯ ВЕРСИЯ"""
        try:
            # 1. Воспроизводим локально
            if self.pygame_available:
                try:
                    pygame.mixer.music.load(audio_file)
                    pygame.mixer.music.play()
                    logger.info(f"🔊 Локальное воспроизведение: {os.path.basename(audio_file)}")
                except Exception as e:
                    logger.warning(f"Не удалось воспроизвести локально: {e}")

            # 2. ПРОСТОЙ способ отправки в стрим
            if self.ffmpeg_manager and self.ffmpeg_manager.rtmp_url:
                # Используем отдельный FFmpeg процесс только для аудио
                # Это будет работать, но может конфликтовать с основным видео
                rtmp_url = self.ffmpeg_manager.rtmp_url

                # Команда для отправки только аудио
                cmd = [
                    'ffmpeg',
                    '-re',
                    '-i', audio_file,
                    '-c:a', 'aac',
                    '-b:a', '128k',
                    '-ar', '44100',
                    '-ac', '2',
                    '-f', 'flv',
                    rtmp_url
                ]

                logger.info(f"📤 Отправка аудио в стрим: {os.path.basename(audio_file)}")

                # Запускаем в отдельном процессе
                process = subprocess.Popen(
                    cmd,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL
                )

                # Ждем завершения (но не блокируем)
                def wait_for_audio():
                    process.wait()
                    if process.returncode == 0:
                        logger.info(f"✅ Аудио отправлено: {os.path.basename(audio_file)}")
                    else:
                        logger.warning(f"⚠️ Аудио процесс завершился с кодом: {process.returncode}")

                threading.Thread(target=wait_for_audio, daemon=True).start()

        except Exception as e:
            logger.error(f"❌ Ошибка отправки аудио: {e}")

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
            await asyncio.sleep(10)  # Пауза при ошибке


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


@app.route('/api/youtube/check_status/<broadcast_id>')
def check_youtube_status(broadcast_id):
    """Проверка статуса YouTube трансляции"""
    try:
        if not youtube_oauth or not youtube_oauth.youtube:
            return jsonify({
                'status': 'error',
                'message': 'YouTube API не инициализирован'
            })

        # Получаем информацию о трансляции
        request = youtube_oauth.youtube.liveBroadcasts().list(
            part='id,snippet,status,contentDetails',
            id=broadcast_id
        )
        response = request.execute()

        if 'items' not in response or len(response['items']) == 0:
            return jsonify({'status': 'not_found'})

        broadcast = response['items'][0]
        status = broadcast['status']['lifeCycleStatus']

        return jsonify({
            'status': status,
            'title': broadcast['snippet']['title'],
            'scheduled_start_time': broadcast['snippet'].get('scheduledStartTime'),
            'actual_start_time': broadcast['snippet'].get('actualStartTime'),
            'watch_url': f"https://youtube.com/watch?v={broadcast_id}",
            'is_live': status == 'live',
            'health_status': broadcast['status'].get('healthStatus', {}).get('status', 'unknown')
        })

    except Exception as e:
        logger.error(f"Ошибка проверки статуса: {e}")
        return jsonify({'status': 'error', 'message': str(e)})


@app.route('/api/youtube/start_live', methods=['POST'])
def start_live_manually():
    """Ручной запуск трансляции (если не началась автоматически)"""
    try:
        if not youtube_oauth or not youtube_oauth.broadcast_id:
            return jsonify({
                'status': 'error',
                'message': 'Нет активной трансляции'
            })

        success = youtube_oauth.start_broadcast()

        if success:
            return jsonify({
                'status': 'success',
                'message': 'Трансляция переведена в статус live'
            })
        else:
            return jsonify({
                'status': 'error',
                'message': 'Не удалось начать трансляцию. Проверьте статус в YouTube Studio.'
            })

    except Exception as e:
        logger.error(f"Ошибка ручного запуска: {e}")
        return jsonify({'status': 'error', 'message': str(e)})
    
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

# ... (другие роуты с заменой youtube_service_account на youtube_oauth)

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
    # Просто выберите новую тему
    topic = stream_manager.select_topic()
    return jsonify({'success': True, 'topic': topic, 'message': 'Дискуссия продолжается'})


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


@app.route('/api/send_message', methods=['POST'])
def api_send_message():
    """Ручная отправка сообщения от агента"""
    data = request.get_json()
    agent_id = data.get('agent_id', 0)
    text = data.get('text', '')

    if not text:
        return jsonify({'success': False, 'error': 'Текст обязателен'})

    # Находим агента
    agent = None
    for a in stream_manager.agents:
        if a.id == agent_id:
            agent = a
            break

    if not agent:
        return jsonify({'success': False, 'error': 'Агент не найден'})

    # Отправляем сообщение через WebSocket
    socketio.emit('new_message', {
        'agent_id': agent.id,
        'agent_name': agent.name,
        'message': text,
        'expertise': agent.expertise,
        'avatar': agent.avatar,
        'color': agent.color,
        'timestamp': datetime.now().isoformat()
    })

    return jsonify({'success': True, 'message': 'Сообщение отправлено'})


if __name__ == '__main__':
    # Регистрируем обработчики сигналов
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    print("=" * 70)
    print("🤖 AI AGENTS STREAM WITH FFMPEG (OAuth Version)")
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

    # Запускаем цикл дискуссии в отдельном потоке
    print("\n🔄 Запуск цикла дискуссии AI агентов...")
    discussion_thread = threading.Thread(target=start_discussion_loop, daemon=True)
    discussion_thread.start()
    print("✅ Цикл дискуссии запущен")

    # Статистика агентов
    print(f"👥 Загружено {len(stream_manager.agents)} AI агентов:")
    for agent in stream_manager.agents:
        print(f"   • {agent.name} - {agent.expertise}")

    print("\n" + "=" * 70)
    print("🌐 Веб-интерфейс доступен по адресу: http://localhost:5000")
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
        .controls { display: flex; gap: 10px; margin: 20px 0; }
        button { padding: 10px 20px; background: #4a69ff; color: white; border: none; border-radius: 5px; cursor: pointer; }
        button:hover { background: #3a59ef; }
        .status { padding: 10px; border-radius: 5px; margin: 10px 0; }
        .status-streaming { background: #1a5a1a; }
        .status-stopped { background: #5a1a1a; }
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
                .then(data => alert(data.message || 'Дискуссия запущена'))
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
                    alert(`YouTube трансляция запущена!\nСсылка: ${data.watch_url}`);
                } else if (data.status === 'auth_required') {
                    window.open(data.auth_url, '_blank');
                    alert('Требуется авторизация в YouTube. Откройте ссылку для авторизации.');
                } else {
                    alert('Ошибка: ' + (data.message || 'Неизвестная ошибка'));
                }
            })
            .catch(err => console.error('Error:', err));
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
                        ${agent.is_speaking ? '<div style="color: #4a69ff;">🎤 Говорит сейчас...</div>' : ''}
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