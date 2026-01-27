#!/usr/bin/env python3
"""
YouTube Live Streaming API - Сервисный аккаунт версия
Позволяет создавать и управлять трансляциями без ручного OAuth
"""

import os
import json
import time
import logging
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Any
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
import google.auth.transport.requests

logger = logging.getLogger(__name__)


class YouTubeServiceAccountStream:
    """Управление YouTube трансляциями через сервисный аккаунт"""

    # Скоупы для YouTube API
    SCOPES = [
        'https://www.googleapis.com/auth/youtube',
        'https://www.googleapis.com/auth/youtube.force-ssl',
        'https://www.googleapis.com/auth/youtube.readonly'
    ]

    def __init__(self, service_account_file: str, channel_id: Optional[str] = None):
        """
        Инициализация с сервисным аккаунтом

        Args:
            service_account_file: Путь к JSON файлу сервисного аккаунта
            channel_id: ID YouTube канала (необязательно)
        """
        self.service_account_file = service_account_file
        self.channel_id = channel_id
        self.youtube = None
        self.broadcast_id = None
        self.stream_id = None
        self.is_live = False
        self.credentials = None

        # Статистика и метрики
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

            # Создаем делегированные права (если нужно)
            # Для работы с YouTube API сервисному аккаунту нужен доступ к каналу
            if self.channel_id:
                from google.auth import impersonated_credentials
                # Здесь нужно настроить делегирование прав
                pass

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

            logger.info(f"🌊 Поток создан: {self.stream_id}")
            logger.info(f"🔑 Stream Key: {stream_key}")
            logger.info(f"📍 RTMP URL: rtmp://a.rtmp.youtube.com/live2/{stream_key}")

            self.metrics['streams_created'] += 1

            return {
                'stream_id': self.stream_id,
                'stream_key': stream_key,
                'ingestion_address': ingestion_address,
                'rtmp_url': f"rtmp://a.rtmp.youtube.com/live2/{stream_key}",
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
            'current_stream': self.stream_id
        }