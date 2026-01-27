#!/usr/bin/env python3
"""
🤖 AI Stream с FFmpeg стримингом на YouTube
Версия с сервисным аккаунтом YouTube API
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
from flask import Flask, render_template, request, jsonify
from flask_socketio import SocketIO, emit
import signal
import shutil

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

# Попробуем импортировать Google API для сервисного аккаунта
YOUTUBE_SERVICE_ACCOUNT_AVAILABLE = False
youtube_service_account = None

try:
    from google.oauth2 import service_account
    from googleapiclient.discovery import build
    from googleapiclient.errors import HttpError


    # Ищем файл сервисного аккаунта в текущей директории
    def find_service_account_file():
        # Список возможных имен файлов
        possible_filenames = [
            'service-account.json',
            'service_account.json',
            'google-service-account.json',
            'google_service_account.json',
            'youtube-service-account.json',
            'youtube_service_account.json'
        ]

        # Проверяем каждый файл
        for filename in possible_filenames:
            if os.path.exists(filename):
                print(f"✅ Файл сервисного аккаунта найден: {filename}")
                return filename

        # Проверяем все JSON файлы в текущей директории
        for file in os.listdir('.'):
            if file.endswith('.json'):
                try:
                    with open(file, 'r') as f:
                        content = json.load(f)
                        # Проверяем, что это файл сервисного аккаунта
                        if 'type' in content and content['type'] == 'service_account':
                            print(f"✅ Найден файл сервисного аккаунта: {file}")
                            return file
                except:
                    continue

        print("⚠️ Файл сервисного аккаунта не найден в текущей директории.")
        print("📁 Содержимое текущей директории:")
        for item in os.listdir('.'):
            print(f"  - {item}")

        return None


    # Ищем файл
    SERVICE_ACCOUNT_FILE = find_service_account_file()

    if SERVICE_ACCOUNT_FILE:
        YOUTUBE_SERVICE_ACCOUNT_AVAILABLE = True
        print(f"🎯 Будет использован файл: {SERVICE_ACCOUNT_FILE}")
    else:
        YOUTUBE_SERVICE_ACCOUNT_AVAILABLE = False
        print("❌ Файл сервисного аккаунта не найден.")
        print("\n📋 Как получить файл:")
        print("1. Создайте проект в Google Cloud Console")
        print("2. Включите YouTube Data API v3")
        print("3. Создайте сервисный аккаунт")
        print("4. Скачайте JSON ключ")
        print("5. Сохраните как 'service-account.json' в текущей папке")

    # ID канала можно оставить None, если не нужен
    YOUTUBE_CHANNEL_ID = None

except ImportError:
    print("⚠️ Google API модуль не найден.")
    print("Для автоматических трансляций установите:")
    print("pip install google-api-python-client google-auth-httplib2 google-auth-oauthlib")
    YOUTUBE_SERVICE_ACCOUNT_AVAILABLE = False
except Exception as e:
    print(f"⚠️ Неожиданная ошибка при импорте Google API: {e}")
    YOUTUBE_SERVICE_ACCOUNT_AVAILABLE = False

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


# ========== YOUTUBE SERVICE ACCOUNT API ==========

class YouTubeServiceAccountStream:
    """Управление YouTube трансляциями через сервисный аккаунт"""

    def __init__(self, service_account_file: str, channel_id: Optional[str] = None):
        self.service_account_file = service_account_file
        self.channel_id = channel_id
        self.youtube = None
        self.broadcast_id = None
        self.stream_id = None
        self.is_live = False
        self.credentials = None
        self.stream_key = None
        self.rtmp_url = None

        # Скоупы для YouTube API
        self.SCOPES = [
            'https://www.googleapis.com/auth/youtube',
            'https://www.googleapis.com/auth/youtube.force-ssl',
            'https://www.googleapis.com/auth/youtube.readonly'
        ]

        # Статистика
        self.metrics = {
            'streams_created': 0,
            'broadcasts_created': 0,
            'errors': []
        }

        logger.info(f"Инициализация YouTube API с сервисным аккаунтом: {service_account_file}")

    def authenticate(self) -> bool:
        """Аутентификация через сервисный аккаунт"""
        try:
            if not os.path.exists(self.service_account_file):
                logger.error(f"❌ Файл сервисного аккаунта не найден: {self.service_account_file}")
                return False

            # Загружаем сервисный аккаунт
            self.credentials = service_account.Credentials.from_service_account_file(
                self.service_account_file,
                scopes=self.SCOPES
            )

            # Создаем YouTube API клиент
            self.youtube = build(
                'youtube',
                'v3',
                credentials=self.credentials
            )

            logger.info("✅ Аутентификация через сервисный аккаунт успешна")

            # Проверяем доступ к API
            return self.test_api_access()

        except Exception as e:
            logger.error(f"❌ Ошибка аутентификации: {e}")
            self.metrics['errors'].append(str(e))
            return False

    def test_api_access(self) -> bool:
        """Проверка доступа к YouTube API"""
        try:
            # Простой запрос для проверки доступа
            request = self.youtube.channels().list(
                part="snippet",
                mine=True
            )
            response = request.execute()

            if 'items' in response:
                channel_info = response['items'][0]['snippet']
                logger.info(f"📺 Канал: {channel_info['title']}")
                logger.info(f"📝 Описание: {channel_info.get('description', 'Нет описания')[:100]}...")
                return True

            return False

        except HttpError as e:
            if e.resp.status == 403:
                logger.error("❌ Нет доступа к YouTube API. Проверьте:")
                logger.error("1. Активирован ли YouTube Data API v3 в Google Cloud")
                logger.error("2. Добавлен ли сервисный аккаунт в Google Workspace")
                logger.error("3. Есть ли у сервисного аккаунта доступ к каналу")
            else:
                logger.error(f"❌ Ошибка API: {e}")
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
        """
        Создание трансляции

        Args:
            title: Заголовок трансляции
            description: Описание
            privacy_status: public/unlisted/private
            scheduled_time: Время начала (если None - начать сейчас)
        """
        try:
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
            logger.info(f"⏰ Время начала: {scheduled_time}")

            self.metrics['broadcasts_created'] += 1

            return response

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
        """
        Создание потока для трансляции

        Args:
            title: Название потока
            resolution: Разрешение (240p/360p/480p/720p/1080p)
            frame_rate: Частота кадров
        """
        try:
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
        """Начало трансляции (перевод в статус 'live')"""
        try:
            if not self.broadcast_id:
                logger.error("❌ Нет активной трансляции")
                return False

            request = self.youtube.liveBroadcasts().transition(
                broadcastStatus='live',
                id=self.broadcast_id,
                part='status'
            )

            response = request.execute()
            self.is_live = True

            logger.info("🎬 ТРАНСЛЯЦИЯ НАЧАЛАСЬ!")
            logger.info(f"📺 Ссылка: https://youtube.com/watch?v={self.broadcast_id}")

            return True

        except Exception as e:
            logger.error(f"❌ Ошибка начала трансляции: {e}")
            self.metrics['errors'].append(str(e))
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

    def get_stream_key_info(self) -> Optional[Dict[str, str]]:
        """Получение информации о stream key"""
        try:
            if not self.stream_id:
                return None

            request = self.youtube.liveStreams().list(
                part='cdn',
                id=self.stream_id
            )

            response = request.execute()

            if not response.get('items'):
                return None

            cdn_info = response['items'][0]['cdn']
            stream_key = cdn_info['ingestionInfo']['streamName']

            return {
                'stream_key': stream_key,
                'rtmp_url': f"rtmp://a.rtmp.youtube.com/live2/{stream_key}",
                'ingestion_address': cdn_info['ingestionInfo']['ingestionAddress'],
                'frame_rate': cdn_info.get('frameRate', '30fps'),
                'resolution': cdn_info.get('resolution', '1080p')
            }

        except Exception as e:
            logger.error(f"❌ Ошибка получения stream key: {e}")
            return None

    def get_chat_id(self) -> Optional[str]:
        """Получение ID чата трансляции"""
        try:
            if not self.broadcast_id:
                return None

            request = self.youtube.liveBroadcasts().list(
                part='snippet',
                id=self.broadcast_id
            )

            response = request.execute()

            if response.get('items'):
                return response['items'][0]['snippet'].get('liveChatId')

            return None

        except Exception as e:
            logger.error(f"❌ Ошибка получения chat ID: {e}")
            return None

    def update_broadcast(
            self,
            title: Optional[str] = None,
            description: Optional[str] = None
    ) -> bool:
        """Обновление информации о трансляции"""
        try:
            if not self.broadcast_id:
                logger.error("❌ Нет активной трансляции для обновления")
                return False

            # Получаем текущие данные
            request = self.youtube.liveBroadcasts().list(
                part='snippet',
                id=self.broadcast_id
            )

            response = request.execute()
            snippet = response['items'][0]['snippet']

            # Обновляем поля
            if title:
                snippet['title'] = title
            if description:
                snippet['description'] = description

            # Отправляем обновление
            update_request = self.youtube.liveBroadcasts().update(
                part='snippet',
                body={
                    'id': self.broadcast_id,
                    'snippet': snippet
                }
            )

            update_response = update_request.execute()
            logger.info("📝 Информация о трансляции обновлена")

            return True

        except Exception as e:
            logger.error(f"❌ Ошибка обновления: {e}")
            return False

    def list_broadcasts(
            self,
            status: str = "all",  # all, active, completed, upcoming
            max_results: int = 10
    ) -> List[Dict[str, Any]]:
        """Список трансляций"""
        try:
            broadcast_status = None
            if status == "active":
                broadcast_status = "active"
            elif status == "completed":
                broadcast_status = "completed"
            elif status == "upcoming":
                broadcast_status = "upcoming"

            request = self.youtube.liveBroadcasts().list(
                part='snippet,status,contentDetails',
                broadcastStatus=broadcast_status,
                maxResults=max_results
            )

            response = request.execute()
            broadcasts = []

            for item in response.get('items', []):
                broadcast = {
                    'id': item['id'],
                    'title': item['snippet']['title'],
                    'description': item['snippet'].get('description', ''),
                    'status': item['status']['lifeCycleStatus'],
                    'privacy': item['status']['privacyStatus'],
                    'url': f"https://youtube.com/watch?v={item['id']}",
                    'scheduled_start': item['snippet'].get('scheduledStartTime'),
                    'actual_start': item['snippet'].get('actualStartTime'),
                    'actual_end': item['snippet'].get('actualEndTime'),
                    'chat_id': item['snippet'].get('liveChatId'),
                    'is_default_broadcast': item['status'].get('isDefaultBroadcast', False)
                }
                broadcasts.append(broadcast)

            logger.info(f"📋 Найдено {len(broadcasts)} трансляций")
            return broadcasts

        except Exception as e:
            logger.error(f"❌ Ошибка получения списка трансляций: {e}")
            return []

    def start_full_stream(
            self,
            title: str,
            description: str = "",
            privacy_status: str = "unlisted",
            resolution: str = "1080p"
    ) -> Optional[Dict[str, Any]]:
        """
        Полный процесс запуска трансляции

        Args:
            title: Заголовок трансляции
            description: Описание
            privacy_status: Статус приватности
            resolution: Разрешение видео
        """
        try:
            # 1. Аутентификация
            if not self.authenticate():
                return None

            # 2. Создание трансляции
            broadcast = self.create_live_broadcast(
                title=title,
                description=description,
                privacy_status=privacy_status
            )

            if not broadcast:
                return None

            # 3. Создание потока
            stream_info = self.create_stream(
                title=f"Stream for: {title[:50]}",
                resolution=resolution
            )

            if not stream_info:
                return None

            # 4. Привязка
            if not self.bind_broadcast_to_stream():
                return None

            # 5. Получаем финальную информацию
            stream_key_info = self.get_stream_key_info()

            result = {
                'success': True,
                'broadcast_id': self.broadcast_id,
                'stream_id': self.stream_id,
                'watch_url': f"https://youtube.com/watch?v={self.broadcast_id}",
                'stream_key': stream_info['stream_key'],
                'rtmp_url': stream_info['rtmp_url'],
                'chat_id': self.get_chat_id(),
                'stream_info': stream_key_info,
                'message': "Трансляция создана, запустите FFmpeg для начала стрима"
            }

            print("\n" + "=" * 70)
            print("🎬 YOUTUBE ТРАНСЛЯЦИЯ ГОТОВА К ЗАПУСКУ!")
            print("=" * 70)
            print(f"📺 Ссылка: {result['watch_url']}")
            print(f"🔑 Stream Key: {result['stream_key']}")
            print(f"📍 RTMP URL: {result['rtmp_url']}")
            print("=" * 70)
            print("\n⚠️  Запустите FFmpeg для начала стрима:")
            print(f"ffmpeg -f lavfi -i color=c=black:s=1920x1080:r=30 \\")
            print(f"       -f lavfi -i anullsrc \\")
            print(f"       -c:v libx264 -preset veryfast \\")
            print(f"       -c:a aac \\")
            print(f"       -f flv {result['rtmp_url']}")
            print("=" * 70)

            return result

        except Exception as e:
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
            'rtmp_url': self.rtmp_url
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
        self.ffmpeg_stdin = None
        self.start_time = None
        self.audio_queue = []
        self.is_playing_audio = False
        self.audio_sample_rate = 44100
        self.audio_channels = 2
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
        """Запуск FFmpeg стрима с передачей аудио"""
        if not self.stream_key:
            logger.error("❌ Stream Key не установлен!")
            return False

        try:
            self.start_time = time.time()

            # Базовая команда FFmpeg для YouTube
            ffmpeg_cmd = [
                'ffmpeg',
                '-re',  # Реальное время
                '-f', 'lavfi',
                '-i',
                f'color=c=black:s=1920x1080:r=30:drawtext=text="AI\\\\ Stream\\\\ {datetime.now().strftime("%H:%M")}":fontcolor=white:fontsize=48:x=(w-text_w)/2:y=(h-text_h)/2',
                '-f', 'lavfi',
                '-i', 'anullsrc=channel_layout=stereo:sample_rate=44100',
                '-c:v', 'libx264',
                '-preset', 'veryfast',
                '-tune', 'zerolatency',
                '-pix_fmt', 'yuv420p',
                '-g', '60',
                '-b:v', '4500k',
                '-c:a', 'aac',
                '-b:a', '128k',
                '-ar', '44100',
                '-ac', '2',
                '-f', 'flv',
                self.rtmp_url
            ]

            logger.info(f"🚀 Запуск FFmpeg: {' '.join(ffmpeg_cmd[:10])}...")

            # Запускаем FFmpeg
            self.stream_process = subprocess.Popen(
                ffmpeg_cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=False,
                bufsize=0
            )

            self.is_streaming = True
            self.ffmpeg_pid = self.stream_process.pid
            self.ffmpeg_stdin = self.stream_process.stdin

            # Запуск мониторинга
            threading.Thread(target=self._monitor_ffmpeg, daemon=True).start()

            logger.info(f"🎬 FFmpeg стрим запущен (PID: {self.ffmpeg_pid})")
            return True

        except Exception as e:
            logger.error(f"❌ Ошибка запуска FFmpeg: {e}", exc_info=True)
            return False

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

    def play_audio_file(self, audio_file: str):
        """Воспроизведение аудио файла (MP3) в стриме"""
        if not os.path.exists(audio_file):
            logger.error(f"❌ Аудио файл не найден: {audio_file}")
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

    def check_stream_connection(self):
        """Проверка подключения к YouTube"""
        if not self.rtmp_url:
            return {'connected': False, 'error': 'No RTMP URL'}

        try:
            # Команда для проверки подключения
            cmd = [
                'ffprobe',
                '-v', 'error',
                '-rw_timeout', '5000000',
                '-timeout', '5000000',
                '-show_entries', 'stream=codec_name',
                self.rtmp_url
            ]

            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=10
            )

            # Проверяем наличие ошибок в stderr
            if "Connection refused" in result.stderr or "Cannot open" in result.stderr:
                return {'connected': False, 'error': result.stderr[:200]}

            # Если нет критических ошибок, считаем подключение возможным
            return {
                'connected': True if result.returncode == 0 else 'maybe',
                'output': result.stderr[:500]
            }

        except subprocess.TimeoutExpired:
            return {'connected': False, 'error': 'Connection timeout'}
        except Exception as e:
            logger.error(f"Ошибка проверки подключения: {e}")
            return {'connected': False, 'error': str(e)}


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

        try:
            pygame.mixer.init(frequency=44100, size=-16, channels=2, buffer=2048)
            self.pygame_available = True
        except:
            self.pygame_available = False
            logger.warning("⚠️ Pygame не доступен для локального воспроизведения")

        logger.info("Edge TTS Manager инициализирован")

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
                # Используем ThreadPoolExecutor для запуска в отдельном потоке
                loop = asyncio.get_event_loop()
                await loop.run_in_executor(
                    None,
                    self.ffmpeg_manager.play_audio_file,
                    audio_file
                )
                logger.info(f"📤 Аудио отправлено в стрим: {os.path.basename(audio_file)}")

        except Exception as e:
            logger.error(f"Ошибка воспроизведения: {e}")

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


# ========== ГЛОБАЛЬНЫЕ ОБЪЕКТЫ ==========

ffmpeg_manager = FFmpegStreamManager()
stream_manager = AIStreamManager(ffmpeg_manager)

# Инициализация YouTube Service Account - ИСПРАВЛЕННАЯ ВЕРСИЯ
youtube_service_account = None

if YOUTUBE_SERVICE_ACCOUNT_AVAILABLE and SERVICE_ACCOUNT_FILE:
    try:
        print(f"\n🔧 Инициализация YouTube Service Account...")
        print(f"📄 Файл: {SERVICE_ACCOUNT_FILE}")
        print(f"📁 Абсолютный путь: {os.path.abspath(SERVICE_ACCOUNT_FILE)}")

        # Проверяем существование файла
        if not os.path.exists(SERVICE_ACCOUNT_FILE):
            print(f"❌ Файл не найден по указанному пути!")
            # Пробуем найти в текущей директории
            current_dir = os.path.dirname(os.path.abspath(__file__))
            alt_path = os.path.join(current_dir, 'service-account.json')
            if os.path.exists(alt_path):
                SERVICE_ACCOUNT_FILE = alt_path
                print(f"✅ Файл найден по альтернативному пути: {SERVICE_ACCOUNT_FILE}")

        if os.path.exists(SERVICE_ACCOUNT_FILE):
            # Читаем содержимое файла для отладки
            try:
                with open(SERVICE_ACCOUNT_FILE, 'r') as f:
                    content = json.load(f)
                    print(f"✅ Файл валиден, email: {content.get('client_email', 'не указан')}")
            except Exception as e:
                print(f"⚠️ Ошибка чтения файла: {e}")

            # Создаем экземпляр YouTubeServiceAccountStream
            youtube_service_account = YouTubeServiceAccountStream(
                service_account_file=SERVICE_ACCOUNT_FILE,
                channel_id=None  # Можно оставить None
            )

            # Пробуем аутентифицироваться
            if youtube_service_account.authenticate():
                print("✅ YouTube Service Account успешно инициализирован!")
            else:
                print("❌ Не удалось аутентифицироваться через сервисный аккаунт")
                youtube_service_account = None
        else:
            print(f"❌ Файл сервисного аккаунта не найден: {SERVICE_ACCOUNT_FILE}")

    except Exception as e:
        print(f"❌ Ошибка инициализации YouTube Service Account: {e}")
        import traceback

        traceback.print_exc()
        youtube_service_account = None
else:
    print("ℹ️ YouTube Service Account не будет использоваться")


# ========== АСИНХРОННЫЙ ЦИКЛ ==========

async def discussion_loop():
    """Основной цикл дискуссии"""
    await asyncio.sleep(2)
    logger.info("🔄 Запуск цикла дискуссии")
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

@app.route('/')
def index():
    """Главная страница"""
    youtube_status = {
        'available': youtube_service_account is not None,
        'authenticated': youtube_service_account is not None and youtube_service_account.youtube is not None,
        'has_broadcast': youtube_service_account is not None and youtube_service_account.broadcast_id is not None,
        'is_live': youtube_service_account is not None and youtube_service_account.is_live,
        'broadcast_id': youtube_service_account.broadcast_id if youtube_service_account else None,
        'stream_key': youtube_service_account.stream_key if youtube_service_account else None,
        'rtmp_url': youtube_service_account.rtmp_url if youtube_service_account else None
    }

    return render_template('index.html',
                           agents=stream_manager.get_agents_state(),
                           topic=stream_manager.current_topic or "Загрузка темы...",
                           stats=stream_manager.get_stats(),
                           youtube_status=youtube_status)


@app.route('/health')
def health():
    """Проверка здоровья"""
    youtube_status = {
        'available': youtube_service_account is not None,
        'authenticated': youtube_service_account is not None and youtube_service_account.youtube is not None,
        'has_broadcast': youtube_service_account is not None and youtube_service_account.broadcast_id is not None,
        'is_live': youtube_service_account is not None and youtube_service_account.is_live
    }

    return jsonify({
        'status': 'ok',
        'time': datetime.now().isoformat(),
        'agents': len(stream_manager.agents),
        'streaming': ffmpeg_manager.is_streaming,
        'discussion_active': stream_manager.is_discussion_active,
        'youtube_service_account': youtube_status
    })


@app.route('/api/start_stream', methods=['POST'])
def start_stream():
    """Запуск FFmpeg стрима (ручной ввод stream key)"""
    try:
        # Принимаем данные
        if request.is_json:
            data = request.get_json()
        else:
            try:
                data = json.loads(request.data.decode('utf-8'))
            except:
                return jsonify({
                    'status': 'error',
                    'message': 'Content-Type должен быть application/json'
                }), 415

        stream_key = data.get('stream_key', '')
        if not stream_key:
            return jsonify({
                'status': 'error',
                'message': 'Stream Key обязателен'
            }), 400

        logger.info(f"📨 Ручной запуск стрима")

        # Устанавливаем ключ
        ffmpeg_manager.set_stream_key(stream_key)

        # Настраиваем видео источник
        video_source = data.get('video_source', 'black')
        ffmpeg_manager.set_video_source(video_source)

        # Запускаем стрим
        use_audio = data.get('use_audio', True)
        if ffmpeg_manager.start_stream(use_audio=use_audio):
            return jsonify({
                'status': 'started',
                'rtmp_url': ffmpeg_manager.rtmp_url,
                'pid': ffmpeg_manager.ffmpeg_pid,
                'video_source': ffmpeg_manager.video_source,
                'message': 'YouTube стрим успешно запущен'
            })
        else:
            return jsonify({
                'status': 'error',
                'message': 'Не удалось запустить FFmpeg процесс'
            }), 500

    except Exception as e:
        logger.error(f"Ошибка запуска стрима: {e}", exc_info=True)
        return jsonify({
            'status': 'error',
            'message': f'Внутренняя ошибка сервера: {str(e)}'
        }), 500


@app.route('/api/start_youtube_stream', methods=['POST'])
def start_youtube_stream():
    """Запуск стрима через YouTube Service Account API"""
    try:
        if not youtube_service_account:
            return jsonify({
                'status': 'error',
                'message': 'YouTube Service Account не настроен. Проверьте наличие service-account.json'
            }), 501

        # Получаем параметры
        if request.is_json:
            data = request.get_json()
        else:
            data = request.form

        title = data.get('title', "🤖 AI Agents Live: Научные дебаты ИИ")
        description = data.get('description', Config.STREAM_DESCRIPTION)
        privacy_status = data.get('privacy_status', 'unlisted')
        resolution = data.get('resolution', '1080p')

        logger.info(f"🎬 Запуск YouTube стрима через Service Account: {title}")

        # Создаем трансляцию через YouTube API
        result = youtube_service_account.start_full_stream(
            title=title,
            description=description,
            privacy_status=privacy_status,
            resolution=resolution
        )

        if result and result.get('success'):
            # Устанавливаем stream key в FFmpeg менеджер
            ffmpeg_manager.set_stream_key(result['stream_key'])

            # Запускаем FFmpeg стрим
            if ffmpeg_manager.start_stream():
                # Запускаем YouTube трансляцию (переводим в статус live)
                youtube_service_account.start_broadcast()

                return jsonify({
                    'status': 'started',
                    'broadcast_id': youtube_service_account.broadcast_id,
                    'stream_id': youtube_service_account.stream_id,
                    'watch_url': f"https://youtube.com/watch?v={youtube_service_account.broadcast_id}",
                    'stream_key': youtube_service_account.stream_key,
                    'rtmp_url': youtube_service_account.rtmp_url,
                    'pid': ffmpeg_manager.ffmpeg_pid,
                    'chat_id': youtube_service_account.get_chat_id(),
                    'message': 'YouTube трансляция создана и стрим запущен'
                })
            else:
                return jsonify({
                    'status': 'error',
                    'message': 'Трансляция создана, но не удалось запустить FFmpeg стрим'
                }), 500
        else:
            return jsonify({
                'status': 'error',
                'message': 'Не удалось создать YouTube трансляцию'
            }), 500

    except Exception as e:
        logger.error(f"Ошибка запуска YouTube стрима: {e}", exc_info=True)
        return jsonify({
            'status': 'error',
            'message': f'Внутренняя ошибка сервера: {str(e)}'
        }), 500


@app.route('/api/youtube_control', methods=['POST'])
def youtube_control():
    """Управление YouTube трансляцией"""
    try:
        if not youtube_service_account:
            return jsonify({
                'status': 'error',
                'message': 'YouTube Service Account не настроен'
            }), 501

        if request.is_json:
            data = request.get_json()
        else:
            data = request.form

        action = data.get('action', '')

        if action == 'get_info':
            try:
                info = youtube_service_account.get_stream_key_info()
                return jsonify({
                    'status': 'success',
                    'broadcast_id': youtube_service_account.broadcast_id,
                    'stream_id': youtube_service_account.stream_id,
                    'is_live': youtube_service_account.is_live,
                    'stream_info': info,
                    'metrics': youtube_service_account.get_metrics()
                })
            except Exception as e:
                return jsonify({
                    'status': 'error',
                    'message': f'Ошибка получения информации: {str(e)}'
                })

        elif action == 'update_info':
            try:
                title = data.get('title')
                description = data.get('description')
                result = youtube_service_account.update_broadcast(title, description)
                if result:
                    return jsonify({'status': 'updated'})
                return jsonify({'status': 'error', 'message': 'Не удалось обновить'})
            except Exception as e:
                return jsonify({'status': 'error', 'message': str(e)})

        elif action == 'end_stream':
            try:
                # Останавливаем FFmpeg
                ffmpeg_manager.stop_stream()
                # Завершаем YouTube трансляцию
                result = youtube_service_account.complete_broadcast()
                if result:
                    return jsonify({'status': 'ended'})
                return jsonify({'status': 'error', 'message': 'Не удалось завершить'})
            except Exception as e:
                return jsonify({'status': 'error', 'message': str(e)})

        elif action == 'get_chat_id':
            try:
                chat_id = youtube_service_account.get_chat_id()
                if chat_id:
                    return jsonify({'status': 'success', 'chat_id': chat_id})
                return jsonify({'status': 'error', 'message': 'Чат не найден'})
            except Exception as e:
                return jsonify({'status': 'error', 'message': str(e)})

        elif action == 'list_broadcasts':
            try:
                status = data.get('status', 'active')
                max_results = int(data.get('max_results', 10))
                broadcasts = youtube_service_account.list_broadcasts(status, max_results)
                return jsonify({
                    'status': 'success',
                    'broadcasts': broadcasts,
                    'count': len(broadcasts)
                })
            except Exception as e:
                return jsonify({'status': 'error', 'message': str(e)})

        elif action == 'start_broadcast':
            try:
                if youtube_service_account.start_broadcast():
                    return jsonify({'status': 'started'})
                return jsonify({'status': 'error', 'message': 'Не удалось начать трансляцию'})
            except Exception as e:
                return jsonify({'status': 'error', 'message': str(e)})

        else:
            return jsonify({
                'status': 'error',
                'message': 'Неизвестное действие',
                'available_actions': ['get_info', 'update_info', 'end_stream', 'get_chat_id', 'list_broadcasts',
                                      'start_broadcast']
            })

    except Exception as e:
        logger.error(f"Ошибка управления YouTube: {e}")
        return jsonify({'status': 'error', 'message': str(e)})


@app.route('/api/youtube_status')
def youtube_status():
    """Статус YouTube трансляции"""
    try:
        if not youtube_service_account:
            return jsonify({
                'available': False,
                'message': 'YouTube Service Account не настроен'
            })

        return jsonify({
            'available': True,
            'authenticated': youtube_service_account.youtube is not None,
            'has_broadcast': youtube_service_account.broadcast_id is not None,
            'has_stream': youtube_service_account.stream_id is not None,
            'is_live': youtube_service_account.is_live,
            'broadcast_id': youtube_service_account.broadcast_id,
            'stream_id': youtube_service_account.stream_id,
            'stream_key': youtube_service_account.stream_key,
            'rtmp_url': youtube_service_account.rtmp_url,
            'metrics': youtube_service_account.get_metrics()
        })
    except Exception as e:
        return jsonify({
            'available': False,
            'error': str(e)
        })


@app.route('/api/stop_stream', methods=['POST'])
def stop_stream():
    """Остановка стрима"""
    try:
        # Останавливаем YouTube трансляцию если активна
        if youtube_service_account and youtube_service_account.is_live:
            try:
                youtube_service_account.complete_broadcast()
            except Exception as e:
                logger.warning(f"Не удалось остановить YouTube трансляцию: {e}")

        # Останавливаем FFmpeg
        if ffmpeg_manager.stop_stream():
            return jsonify({'status': 'stopped', 'message': 'Стрим остановлен'})
        return jsonify({'status': 'error', 'message': 'Стрим не был запущен'})
    except Exception as e:
        logger.error(f"Ошибка остановки стрима: {e}")
        return jsonify({'status': 'error', 'message': str(e)})


@app.route('/api/stream_status')
def stream_status():
    """Статус стрима"""
    status = ffmpeg_manager.get_status()

    # Добавляем информацию о YouTube Service Account
    if youtube_service_account:
        status['youtube'] = {
            'available': True,
            'authenticated': youtube_service_account.youtube is not None,
            'has_broadcast': youtube_service_account.broadcast_id is not None,
            'is_live': youtube_service_account.is_live,
            'broadcast_id': youtube_service_account.broadcast_id,
            'stream_id': youtube_service_account.stream_id,
            'stream_key': youtube_service_account.stream_key,
            'rtmp_url': youtube_service_account.rtmp_url
        }
    else:
        status['youtube'] = {'available': False}

    return jsonify(status)


@app.route('/api/stream_stats')
def stream_stats():
    """Статистика стрима"""
    stats = stream_manager.get_stats()
    stats.update(ffmpeg_manager.get_status())

    # Добавляем информацию о YouTube
    if youtube_service_account:
        stats['youtube'] = {
            'broadcast_id': youtube_service_account.broadcast_id,
            'is_live': youtube_service_account.is_live,
            'stream_id': youtube_service_account.stream_id,
            'stream_key': youtube_service_account.stream_key,
            'rtmp_url': youtube_service_account.rtmp_url,
            'metrics': youtube_service_account.get_metrics()
        }

    return jsonify(stats)


@app.route('/api/control', methods=['POST'])
def control():
    """Управление дискуссией"""
    try:
        data = request.get_json()
        action = data.get('action', '')

        if action == 'start_discussion':
            stream_manager.is_discussion_active = True
            return jsonify({'status': 'started'})

        elif action == 'stop_discussion':
            stream_manager.is_discussion_active = False
            return jsonify({'status': 'stopped'})

        elif action == 'change_topic':
            topic = stream_manager.select_topic()
            return jsonify({'status': 'changed', 'topic': topic})

        elif action == 'get_topic':
            return jsonify({'topic': stream_manager.current_topic})

        else:
            return jsonify({'status': 'error', 'message': 'Неизвестное действие'})

    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)})


@app.route('/api/test_audio', methods=['POST'])
def test_audio():
    """Тестирование звука в стриме"""
    try:
        if not ffmpeg_manager.is_streaming:
            return jsonify({
                'status': 'error',
                'message': 'Стрим не запущен. Запустите стрим сначала.'
            }), 400

        data = request.get_json()
        text = data.get('text', 'Тестовое сообщение для проверки звука на стриме.')
        voice = data.get('voice', 'male_ru')

        # Запускаем тест в отдельном потоке
        def run_test():
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            loop.run_until_complete(
                stream_manager.tts_manager.speak_direct(text, voice)
            )

        threading.Thread(target=run_test, daemon=True).start()

        return jsonify({
            'status': 'success',
            'message': f'Тестовое аудио запущено: "{text[:50]}..."',
            'voice': voice
        })

    except Exception as e:
        logger.error(f"Ошибка тестирования аудио: {e}")
        return jsonify({'status': 'error', 'message': str(e)})


@app.route('/api/test_youtube_connection')
def test_youtube_connection():
    """Тест подключения к YouTube"""
    try:
        if not ffmpeg_manager.stream_key:
            return jsonify({
                'status': 'error',
                'message': 'Stream Key не установлен'
            })

        result = ffmpeg_manager.check_stream_connection()
        return jsonify(result)
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)})


@app.route('/youtube-control')
def youtube_control_page():
    """Страница управления YouTube API"""
    youtube_status = {
        'available': youtube_service_account is not None,
        'authenticated': youtube_service_account is not None and youtube_service_account.youtube is not None,
        'has_broadcast': youtube_service_account is not None and youtube_service_account.broadcast_id is not None,
        'is_live': youtube_service_account is not None and youtube_service_account.is_live
    }

    return render_template('youtube_control.html',
                           youtube_status=youtube_status)


# ========== WEBSOCKET HANDLERS ==========

@socketio.on('connect')
def handle_connect():
    """Подключение клиента"""
    client_id = request.sid
    logger.info(f"📱 Клиент подключился: {client_id}")

    youtube_status = {
        'available': youtube_service_account is not None,
        'authenticated': youtube_service_account is not None and youtube_service_account.youtube is not None,
        'has_broadcast': youtube_service_account is not None and youtube_service_account.broadcast_id is not None,
        'is_live': youtube_service_account is not None and youtube_service_account.is_live,
        'broadcast_id': youtube_service_account.broadcast_id if youtube_service_account else None,
        'stream_key': youtube_service_account.stream_key if youtube_service_account else None,
        'rtmp_url': youtube_service_account.rtmp_url if youtube_service_account else None
    }

    # Отправляем начальное состояние
    socketio.emit('connected', {
        'status': 'connected',
        'client_id': client_id,
        'agents': stream_manager.get_agents_state(),
        'topic': stream_manager.current_topic or stream_manager.select_topic(),
        'stats': stream_manager.get_stats(),
        'stream_status': ffmpeg_manager.get_status(),
        'server_time': datetime.now().isoformat(),
        'youtube_status': youtube_status
    })


@socketio.on('disconnect')
def handle_disconnect():
    """Отключение клиента"""
    logger.info(f"📱 Клиент отключился: {request.sid}")


@socketio.on('request_update')
def handle_update_request():
    """Запрос обновления"""
    youtube_status = {
        'available': youtube_service_account is not None,
        'authenticated': youtube_service_account is not None and youtube_service_account.youtube is not None,
        'has_broadcast': youtube_service_account is not None and youtube_service_account.broadcast_id is not None,
        'is_live': youtube_service_account is not None and youtube_service_account.is_live,
        'broadcast_id': youtube_service_account.broadcast_id if youtube_service_account else None,
        'stream_key': youtube_service_account.stream_key if youtube_service_account else None,
        'rtmp_url': youtube_service_account.rtmp_url if youtube_service_account else None
    }

    socketio.emit('update', {
        'agents': stream_manager.get_agents_state(),
        'topic': stream_manager.current_topic,
        'stats': stream_manager.get_stats(),
        'stream_status': ffmpeg_manager.get_status(),
        'youtube_status': youtube_status
    })


# ========== ЗАПУСК СЕРВЕРА ==========

def signal_handler(signum, frame):
    """Обработчик сигналов"""
    print(f"\n🛑 Получен сигнал {signum}. Завершение...")

    # Останавливаем стрим
    if ffmpeg_manager.is_streaming:
        ffmpeg_manager.stop_stream()

    # Останавливаем YouTube трансляцию если активна
    if youtube_service_account and youtube_service_account.is_live:
        try:
            youtube_service_account.complete_broadcast()
        except:
            pass

    sys.exit(0)


if __name__ == '__main__':
    # Регистрируем обработчики сигналов
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    print("=" * 70)
    print("🤖 AI AGENTS STREAM WITH FFMPEG")
    print("=" * 70)

    # Инициализация YouTube Service Account
    youtube_status_msg = "❌ Не настроен"
    if youtube_service_account:
        if youtube_service_account.youtube:
            youtube_status_msg = "✅ Настроен и аутентифицирован"
            metrics = youtube_service_account.get_metrics()
            print(f"   YouTube Service Account: {youtube_status_msg}")
            print(f"   Метрики: {metrics['broadcasts_created']} трансляций, {metrics['streams_created']} потоков")
        else:
            youtube_status_msg = "⚠️ Настроен, но не аутентифицирован"
            print(f"   YouTube Service Account: {youtube_status_msg}")
    else:
        print(f"   YouTube Service Account: {youtube_status_msg}")
        print(f"   Используйте ручной Stream Key или настройте сервисный аккаунт")

    # Информация о зависимостях
    print(f"\n📦 Версии зависимостей:")
    print(f"   Flask: 2.3.0")
    print(f"   Flask-SocketIO: 5.3.0")
    print(f"   OpenAI: >=1.3.0")
    print(f"   Edge TTS: >=6.1.9")
    print(f"   FFmpeg: системный")
    print(f"   Google API: Установлен" if YOUTUBE_SERVICE_ACCOUNT_AVAILABLE else "   Google API: Не установлен")

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
        .warning { background: #fff3cd; }
        button { margin: 5px; padding: 10px 20px; border: none; cursor: pointer; border-radius: 5px; }
        .btn-primary { background: #007bff; color: white; }
        .btn-success { background: #28a745; color: white; }
        .btn-danger { background: #dc3545; color: white; }
        .agent-card { display: inline-block; padding: 15px; margin: 10px; border-radius: 8px; }
        .speaking { border: 3px solid #28a745; }
        .message { background: white; padding: 10px; margin: 5px 0; border-radius: 5px; border-left: 4px solid #007bff; }
        .stream-key { font-family: monospace; background: #f8f9fa; padding: 10px; border-radius: 5px; margin: 10px 0; }
        .youtube-status { padding: 10px; margin: 10px 0; border-radius: 5px; }
        .youtube-active { background: #d4edda; border-left: 5px solid #28a745; }
        .youtube-inactive { background: #f8d7da; border-left: 5px solid #dc3545; }
        .youtube-warning { background: #fff3cd; border-left: 5px solid #ffc107; }
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
        <div id="youtube-service-status" class="status warning">
            Проверка доступности YouTube Service Account...
        </div>
        <div>
            <button class="btn-primary" onclick="manualStream()">🔑 Ручной запуск стрима</button>
            <button class="btn-success" onclick="youtubeServiceAccountStream()">🚀 Автоматический YouTube стрим (Service Account)</button>
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
            updateYouTubeStatus(data.youtube_status);
            document.getElementById('current-topic').textContent = data.topic;
        });

        socket.on('update', function(data) {
            updateSystemStatus(data);
            updateAgents(data.agents);
            updateYouTubeStatus(data.youtube_status);
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

        function updateYouTubeStatus(youtube) {
            const statusDiv = document.getElementById('youtube-service-status');

            if(youtube.available) {
                if(youtube.authenticated) {
                    statusDiv.className = 'youtube-status youtube-active';
                    statusDiv.innerHTML = `<strong>YouTube Service Account:</strong> ✅ Настроен и аутентифицирован`;

                    if(youtube.has_broadcast) {
                        statusDiv.innerHTML += `<br><strong>Трансляция:</strong> ${youtube.is_live ? 'В эфире 🟢' : 'Не в эфире 🔴'}`;
                        statusDiv.innerHTML += `<br><strong>ID:</strong> ${youtube.broadcast_id}`;
                        statusDiv.innerHTML += `<br><strong>Stream Key:</strong> ${youtube.stream_key || 'Не указан'}`;
                    }
                } else {
                    statusDiv.className = 'youtube-status youtube-warning';
                    statusDiv.innerHTML = `<strong>YouTube Service Account:</strong> ⚠️ Настроен, но не аутентифицирован`;
                }
            } else {
                statusDiv.className = 'youtube-status youtube-inactive';
                statusDiv.innerHTML = `<strong>YouTube Service Account:</strong> ❌ Не настроен. Используйте ручной ввод Stream Key`;
            }
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

        function youtubeServiceAccountStream() {
            if(!confirm('Запустить автоматический YouTube стрим через Service Account API?\n(Требуется service-account.json)')) {
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
                        alert(`✅ YouTube трансляция создана через Service Account!\nСмотреть: ${data.watch_url}`);
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
        .broadcast-item { border: 1px solid #ddd; padding: 10px; margin: 10px 0; border-radius: 5px; }
        .stream-key { font-family: monospace; background: #f8f9fa; padding: 10px; border-radius: 5px; }
    </style>
</head>
<body>
    <h1>🎬 YouTube Service Account Control Panel</h1>

    <div id="youtube-status" class="status offline">
        YouTube Service Account: Проверка доступности...
    </div>

    <div class="panel">
        <h3>Автоматический запуск YouTube трансляции через Service Account</h3>
        <div>
            <label>Название трансляции:</label><br>
            <input type="text" id="stream-title" value="🤖 AI Agents Live: Научные дебаты ИИ">
        </div>
        <div>
            <label>Описание:</label><br>
            <textarea id="stream-description" rows="8"></textarea>
        </div>
        <div>
            <label>Приватность:</label><br>
            <select id="privacy-status">
                <option value="unlisted">Unlisted (по ссылке)</option>
                <option value="public">Public (публично)</option>
                <option value="private">Private (приватно)</option>
            </select>
        </div>
        <button class="btn btn-success" onclick="startYoutubeStream()">🎬 Создать YouTube трансляцию</button>
        <button class="btn" onclick="checkYouTubeStatus()">🔄 Проверить статус</button>
        <button class="btn" onclick="listBroadcasts()">📋 Список трансляций</button>
    </div>

    <div class="panel" id="stream-controls" style="display: none;">
        <h3>Управление трансляцией</h3>
        <div id="stream-info" class="status info">Информация не доступна</div>
        <div id="stream-key-display" class="stream-key" style="display: none;"></div>
        <button class="btn" onclick="updateStreamInfo()">✏️ Обновить информацию</button>
        <button class="btn" onclick="getChatId()">💬 Получить ID чата</button>
        <button class="btn" onclick="startBroadcast()">▶️ Начать трансляцию (Live)</button>
        <button class="btn btn-danger" onclick="endYoutubeStream()">🛑 Завершить трансляцию</button>
    </div>

    <div class="panel" id="broadcasts-list" style="display: none;">
        <h3>Список трансляций</h3>
        <div id="broadcasts-container"></div>
    </div>

    <div class="panel">
        <h3>Статус FFmpeg</h3>
        <div id="ffmpeg-status" class="status">Загрузка...</div>
        <button class="btn" onclick="checkFFmpegStatus()">🔄 Обновить статус FFmpeg</button>
        <button class="btn" onclick="testYoutubeConnection()">🔗 Тест подключения к YouTube</button>
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
            fetch('/api/youtube_status')
            .then(res => res.json())
            .then(data => {
                const statusDiv = document.getElementById('youtube-status');
                if(data.available) {
                    statusDiv.className = 'status online';
                    let html = 'YouTube Service Account: Доступен ✅<br>';

                    if(data.authenticated) {
                        html += 'Аутентификация: Успешна<br>';
                    } else {
                        html += 'Аутентификация: Не пройдена<br>';
                    }

                    if(data.has_broadcast) {
                        html += `Трансляция: ${data.is_live ? 'В эфире 🟢' : 'Не в эфире 🔴'}<br>`;
                        html += `ID: ${data.broadcast_id}`;
                        document.getElementById('stream-controls').style.display = 'block';
                        updateStreamInfoDisplay(data);
                    }

                    statusDiv.innerHTML = html;
                } else {
                    statusDiv.className = 'status offline';
                    statusDiv.innerHTML = 'YouTube Service Account: Не доступен. Проверьте наличие service-account.json';
                }
            })
            .catch(err => {
                document.getElementById('youtube-status').className = 'status offline';
                document.getElementById('youtube-status').innerHTML = 'YouTube Service Account: Ошибка подключения';
            });
        }

        function startYoutubeStream() {
            const title = document.getElementById('stream-title').value;
            const description = document.getElementById('stream-description').value;
            const privacy = document.getElementById('privacy-status').value;

            if(!title.trim()) {
                alert('Введите название трансляции');
                return;
            }

            fetch('/api/start_youtube_stream', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({title, description, privacy_status: privacy})
            })
            .then(res => res.json())
            .then(data => {
                if(data.status === 'started') {
                    alert('✅ YouTube трансляция создана через Service Account!\\nСсылка: ' + data.watch_url);
                    document.getElementById('stream-controls').style.display = 'block';

                    // Показываем stream key
                    const keyDiv = document.getElementById('stream-key-display');
                    keyDiv.style.display = 'block';
                    keyDiv.innerHTML = `<strong>Stream Key:</strong> ${data.stream_key}<br><strong>RTMP URL:</strong> ${data.rtmp_url}`;

                    updateStreamInfoDisplay({
                        available: true,
                        authenticated: true,
                        has_broadcast: true,
                        is_live: true,
                        broadcast_id: data.broadcast_id,
                        stream_id: data.stream_id,
                        stream_key: data.stream_key,
                        rtmp_url: data.rtmp_url
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
                    alert('❌ Ошибка обновления: ' + data.message);
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
                if(data.status === 'success') {
                    alert('💬 ID чата: ' + data.chat_id);
                } else {
                    alert('❌ Чат не найден: ' + data.message);
                }
            });
        }

        function startBroadcast() {
            fetch('/api/youtube_control', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({action: 'start_broadcast'})
            })
            .then(res => res.json())
            .then(data => {
                if(data.status === 'started') {
                    alert('✅ Трансляция переведена в статус Live!');
                    checkYouTubeStatus();
                } else {
                    alert('❌ Ошибка: ' + data.message);
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
                        document.getElementById('stream-key-display').style.display = 'none';
                        document.getElementById('stream-info').innerHTML = 'Информация не доступна';
                        checkYouTubeStatus();
                    } else {
                        alert('❌ Ошибка завершения: ' + data.message);
                    }
                });
            }
        }

        function listBroadcasts() {
            fetch('/api/youtube_control', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({
                    action: 'list_broadcasts',
                    status: 'all',
                    max_results: 20
                })
            })
            .then(res => res.json())
            .then(data => {
                if(data.status === 'success') {
                    const container = document.getElementById('broadcasts-container');
                    const listDiv = document.getElementById('broadcasts-list');

                    listDiv.style.display = 'block';

                    if(data.count > 0) {
                        let html = `<p>Найдено ${data.count} трансляций:</p>`;

                        data.broadcasts.forEach(broadcast => {
                            html += `<div class="broadcast-item">
                                <strong>${broadcast.title}</strong><br>
                                <small>ID: ${broadcast.id}</small><br>
                                <small>Статус: ${broadcast.status}</small><br>
                                <small>Приватность: ${broadcast.privacy}</small><br>
                                <small>URL: <a href="${broadcast.url}" target="_blank">${broadcast.url}</a></small>
                            </div>`;
                        });

                        container.innerHTML = html;
                    } else {
                        container.innerHTML = '<p>Нет доступных трансляций</p>';
                    }
                } else {
                    alert('❌ Ошибка получения списка: ' + data.message);
                }
            });
        }

        function updateStreamInfoDisplay(data) {
            const infoDiv = document.getElementById('stream-info');
            let html = '';

            if(data.broadcast_id) {
                html += `<strong>ID трансляции:</strong> ${data.broadcast_id}<br>`;
                html += `<strong>Статус:</strong> ${data.is_live ? 'В эфире 🟢' : 'Не в эфире 🔴'}<br>`;
                html += `<strong>Stream Key:</strong> ${data.stream_key || 'Не указан'}<br>`;
                html += `<strong>RTMP URL:</strong> ${data.rtmp_url || 'Не указан'}<br>`;
                html += `<strong>Watch URL:</strong> <a href="https://youtube.com/watch?v=${data.broadcast_id}" target="_blank">https://youtube.com/watch?v=${data.broadcast_id}</a>`;
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
                                           RTMP: ${data.rtmp_url || 'Не указан'}<br>
                                           YouTube: ${data.youtube.available ? 'Доступен' : 'Не доступен'}`;
                } else {
                    statusDiv.className = 'status offline';
                    statusDiv.innerHTML = 'FFmpeg: Не запущен';
                }
            })
            .catch(err => {
                document.getElementById('ffmpeg-status').innerHTML = 'FFmpeg: Ошибка проверки';
            });
        }

        function testYoutubeConnection() {
            fetch('/api/test_youtube_connection')
            .then(res => res.json())
            .then(data => {
                alert(`Результат теста подключения:\nПодключение: ${data.connected ? '✅ Успешно' : '❌ Ошибка'}\nСообщение: ${data.message || data.error || 'Нет информации'}`);
            });
        }
    </script>
</body>
</html>''')

    # Запускаем поток дискуссии
    print("🔄 Запуск цикла дискуссии...")
    discussion_thread = threading.Thread(target=start_discussion_loop, daemon=True)
    discussion_thread.start()

    print("🚀 Запуск веб-сервера...")
    print("🌐 Основной интерфейс: http://localhost:5000")
    print("🎬 YouTube API интерфейс: http://localhost:5000/youtube-control")
    print("🔧 API Endpoints:")
    print("   GET  /health                     - Проверка здоровья")
    print("   POST /api/start_stream           - Ручной запуск стрима")
    print("   POST /api/start_youtube_stream   - Автоматический запуск через YouTube Service Account")
    print("   POST /api/youtube_control        - Управление YouTube трансляцией")
    print("   GET  /api/stream_status          - Статус стрима")
    print("   POST /api/test_audio             - Тест звука")
    print("")
    print("📝 Доступные методы запуска стрима:")
    print("   1. Ручной: Ввести Stream Key в основном интерфейсе")
    print("   2. Автоматический: Использовать YouTube Service Account (требуется service-account.json)")
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