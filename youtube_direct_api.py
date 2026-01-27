# youtube_direct_api.py
import os
import time
import json
import logging
from datetime import datetime
import google.auth
import googleapiclient.discovery
import googleapiclient.errors
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from google_auth_oauthlib.flow import InstalledAppFlow

logger = logging.getLogger(__name__)


class YouTubeDirectStream:
    """Прямая трансляция на YouTube через API"""

    # Scopes для YouTube API
    SCOPES = [
        "https://www.googleapis.com/auth/youtube",
        "https://www.googleapis.com/auth/youtube.force-ssl",
        "https://www.googleapis.com/auth/youtube.readonly"
    ]

    def __init__(self, client_secrets_file: str = "client_secrets.json"):
        self.client_secrets_file = client_secrets_file
        self.credentials = None
        self.youtube = None
        self.broadcast_id = None
        self.stream_id = None
        self.is_live = False

    def authenticate(self):
        """Аутентификация через OAuth 2.0"""
        creds = None

        # Файл token.json хранит токены
        if os.path.exists('token.json'):
            creds = Credentials.from_authorized_user_file('token.json', self.SCOPES)

        # Если нет валидных учетных данных
        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
            else:
                flow = InstalledAppFlow.from_client_secrets_file(
                    self.client_secrets_file, self.SCOPES)
                creds = flow.run_local_server(port=8080)

            # Сохраняем токены
            with open('token.json', 'w') as token:
                token.write(creds.to_json())

        self.credentials = creds

        # Создаем YouTube API клиент
        self.youtube = googleapiclient.discovery.build(
            'youtube', 'v3', credentials=creds)

        logger.info("✅ Аутентификация YouTube API успешна")
        return True

    def create_live_broadcast(self, title: str, description: str = ""):
        """Создание трансляции"""
        try:
            # Создаем трансляцию
            broadcast_body = {
                'snippet': {
                    'title': title,
                    'description': description,
                    'scheduledStartTime': datetime.now().isoformat()
                },
                'status': {
                    'privacyStatus': 'public',  # public, unlisted, private
                    'selfDeclaredMadeForKids': False
                },
                'contentDetails': {
                    'enableAutoStart': True,
                    'enableAutoStop': True,
                    'enableEmbed': True,
                    'recordFromStart': True,
                    'enableDvr': True,
                    'enableContentEncryption': False,
                    'enableLowLatency': False,
                    'projection': 'rectangular'
                }
            }

            # Выполняем запрос
            request = self.youtube.liveBroadcasts().insert(
                part='snippet,status,contentDetails',
                body=broadcast_body
            )

            response = request.execute()
            self.broadcast_id = response['id']

            logger.info(f"📡 Трансляция создана: {self.broadcast_id}")
            logger.info(f"📺 Смотреть: https://youtube.com/watch?v={self.broadcast_id}")

            return response

        except Exception as e:
            logger.error(f"❌ Ошибка создания трансляции: {e}")
            return None

    def start_stream_with_ffmpeg(self, title: str = None, description: str = None,
                                 ffmpeg_manager=None):
        """Запуск стрима с автоматическим запуском FFmpeg"""
        # 1. Создаем YouTube трансляцию
        if not self.start_stream(title, description):
            logger.error("❌ Не удалось создать YouTube трансляцию")
            return False

        # 2. Получаем stream key
        stream_info = self.get_stream_info()
        if not stream_info or 'stream_key' not in stream_info:
            logger.error("❌ Не удалось получить stream key")
            return False

        stream_key = stream_info['stream_key']
        rtmp_url = stream_info['rtmp_url']

        print(f"\n🔑 Получен Stream Key: {stream_key}")
        print(f"📍 RTMP URL: {rtmp_url}")

        # 3. Запускаем FFmpeg (если передан менеджер)
        if ffmpeg_manager:
            try:
                # Устанавливаем stream key
                ffmpeg_manager.set_stream_key(stream_key)

                # Запускаем стрим
                if ffmpeg_manager.start_stream():
                    print("✅ FFmpeg стрим запущен!")
                    return True
                else:
                    print("❌ Не удалось запустить FFmpeg")
                    # Отменяем YouTube трансляцию
                    self.end_stream()
                    return False

            except Exception as e:
                logger.error(f"❌ Ошибка запуска FFmpeg: {e}")
                self.end_stream()
                return False

        # 4. Если менеджер не передан, возвращаем данные для ручного запуска
        else:
            print("\n⚠️  FFmpeg не запущен автоматически")
            print("Запустите FFmpeg вручную:")
            print(
                f"ffmpeg -f lavfi -i color=c=black:s=1920x1080:r=30 -f lavfi -i anullsrc -c:v libx264 -c:a aac -f flv {rtmp_url}")

            return {
                'broadcast_id': self.broadcast_id,
                'stream_id': self.stream_id,
                'stream_key': stream_key,
                'rtmp_url': rtmp_url,
                'watch_url': f"https://youtube.com/watch?v={self.broadcast_id}"
            }

    def create_stream(self):
        """Создание потока"""
        try:
            stream_body = {
                'snippet': {
                    'title': 'AI Agents Stream'
                },
                'cdn': {
                    'frameRate': '30fps',
                    'ingestionType': 'rtmp',
                    'resolution': '1080p'
                }
            }

            request = self.youtube.liveStreams().insert(
                part='snippet,cdn',
                body=stream_body
            )

            response = request.execute()
            self.stream_id = response['id']

            logger.info(f"🌊 Поток создан: {self.stream_id}")

            # Получаем ключ потока
            stream_key = response['cdn']['ingestionInfo']['streamName']
            ingestion_address = response['cdn']['ingestionInfo']['ingestionAddress']

            logger.info(f"🔑 Ключ потока: {stream_key}")
            logger.info(f"📍 Адрес: {ingestion_address}")

            return response

        except Exception as e:
            logger.error(f"❌ Ошибка создания потока: {e}")
            return None

    def bind_broadcast_to_stream(self):
        """Привязка трансляции к потоку"""
        try:
            request = self.youtube.liveBroadcasts().bind(
                part='id,contentDetails',
                id=self.broadcast_id,
                streamId=self.stream_id
            )

            response = request.execute()
            logger.info("🔗 Трансляция привязана к потоку")

            return response

        except Exception as e:
            logger.error(f"❌ Ошибка привязки: {e}")
            return None

    def transition_to_live(self):
        """Перевод трансляции в статус 'live'"""
        try:
            request = self.youtube.liveBroadcasts().transition(
                broadcastStatus='live',
                id=self.broadcast_id,
                part='snippet,status'
            )

            response = request.execute()
            self.is_live = True

            logger.info("🎬 Трансляция начата!")

            return response

        except Exception as e:
            logger.error(f"❌ Ошибка старта трансляции: {e}")
            return None

    def start_stream(self, title: str = None, description: str = None):
        """Запуск стрима"""
        if not title:
            title = "🤖 AI Agents Live: Научные дебаты ИИ"

        if not description:
            description = """Автономные ИИ-агенты обсуждают науку в реальном времени.

Участники:
• Доктор Алексей Волков - Квантовая физика
• Профессор Мария Соколова - Нейробиология
• Доктор Иван Петров - Климатология
• Исследователь София Ковалева - ИИ и робототехника

Темы: Искусственный интеллект, квантовые вычисления, изменение климата, нейроинтерфейсы.

Стрим создан автоматически с помощью Python и OpenAI GPT-4."""

        # Шаг 1: Аутентификация
        if not self.authenticate():
            return False

        # Шаг 2: Создание трансляции
        broadcast = self.create_live_broadcast(title, description)
        if not broadcast:
            return False

        # Шаг 3: Создание потока
        stream = self.create_stream()
        if not stream:
            return False

        # Шаг 4: Привязка
        self.bind_broadcast_to_stream()

        # Шаг 5: Запуск live
        self.transition_to_live()

        # Получаем информацию для стрима
        stream_info = self.get_stream_info()

        print("\n" + "=" * 70)
        print("🎬 YOUTUBE СТРИМ ЗАПУЩЕН!")
        print("=" * 70)
        print(f"📺 Ссылка: https://youtube.com/watch?v={self.broadcast_id}")
        print(f"🔑 Stream Key: {stream_info['stream_key']}")
        print(f"📍 RTMP URL: {stream_info['rtmp_url']}")
        print("=" * 70)

        return True

    def get_stream_info(self):
        """Получение информации о потоке"""
        if not self.stream_id:
            return {}

        try:
            request = self.youtube.liveStreams().list(
                part='cdn',
                id=self.stream_id
            )

            response = request.execute()
            cdn_info = response['items'][0]['cdn']

            return {
                'stream_key': cdn_info['ingestionInfo']['streamName'],
                'rtmp_url': f"rtmp://a.rtmp.youtube.com/live2/{cdn_info['ingestionInfo']['streamName']}",
                'ingestion_address': cdn_info['ingestionInfo']['ingestionAddress']
            }

        except Exception as e:
            logger.error(f"❌ Ошибка получения информации: {e}")
            return {}

    def update_broadcast_info(self, title: str = None, description: str = None):
        """Обновление информации о трансляции"""
        try:
            body = {'id': self.broadcast_id, 'part': 'snippet'}

            if title or description:
                snippet = {}
                if title:
                    snippet['title'] = title
                if description:
                    snippet['description'] = description

                body['snippet'] = snippet

            request = self.youtube.liveBroadcasts().update(**body)
            response = request.execute()

            logger.info("📝 Информация о трансляции обновлена")
            return response

        except Exception as e:
            logger.error(f"❌ Ошибка обновления: {e}")
            return None

    def end_stream(self):
        """Завершение трансляции"""
        try:
            if self.broadcast_id:
                request = self.youtube.liveBroadcasts().transition(
                    broadcastStatus='complete',
                    id=self.broadcast_id,
                    part='id,status'
                )

                response = request.execute()
                self.is_live = False

                logger.info("🛑 Трансляция завершена")
                return response

        except Exception as e:
            logger.error(f"❌ Ошибка завершения: {e}")
            return None

    def get_chat_id(self):
        """Получение ID чата трансляции"""
        try:
            request = self.youtube.liveBroadcasts().list(
                part='snippet',
                id=self.broadcast_id
            )

            response = request.execute()
            chat_id = response['items'][0]['snippet']['liveChatId']

            logger.info(f"💬 ID чата: {chat_id}")
            return chat_id

        except Exception as e:
            logger.error(f"❌ Ошибка получения чата: {e}")
            return None