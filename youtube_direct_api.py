import logging
import googleapiclient.discovery
import googleapiclient.errors

logger = logging.getLogger(__name__)


class YouTubeDirectStream:
    """Прямая трансляция на YouTube через API с использованием API ключа"""

    def __init__(self, api_key: str):
        """
        Инициализация с API ключом

        Args:
            api_key: API ключ YouTube Data API v3
                    (можно получить в Google Cloud Console)
        """
        self.api_key = api_key
        self.youtube = None
        self.broadcast_id = None
        self.stream_id = None
        self.is_live = False
        self.stream_key = None
        self.rtmp_url = None

    def authenticate(self):
        """Аутентификация через API ключ"""
        try:
            # Создаем YouTube API клиент с API ключом
            self.youtube = googleapiclient.discovery.build(
                'youtube',
                'v3',
                developerKey=self.api_key
            )

            logger.info("✅ Аутентификация YouTube API успешна (API Key)")
            return True

        except Exception as e:
            logger.error(f"❌ Ошибка аутентификации: {e}")
            return False

    def create_live_broadcast(self, title: str, description: str = ""):
        """Создание трансляции (требует OAuth - только для чтения через API Key)"""
        logger.warning("⚠️  Создание трансляций требует OAuth аутентификации")
        logger.warning("Используйте OAuth для полного доступа к API")
        return None

    def get_stream_info(self, stream_key: str):
        """
        Получение информации о RTMP URL для стрима

        Args:
            stream_key: Ключ потока из YouTube Studio
        """
        try:
            self.stream_key = stream_key
            self.rtmp_url = f"rtmp://a.rtmp.youtube.com/live2/{stream_key}"

            return {
                'stream_key': stream_key,
                'rtmp_url': self.rtmp_url
            }

        except Exception as e:
            logger.error(f"❌ Ошибка получения информации о стриме: {e}")
            return {}

    def start_stream_with_ffmpeg(self, stream_key: str, title: str = None,
                                 description: str = None, ffmpeg_manager=None):
        """
        Запуск стрима с автоматическим запуском FFmpeg

        Args:
            stream_key: Ключ потока из YouTube Studio
            title: Заголовок стрима (для информации)
            description: Описание стрима (для информации)
            ffmpeg_manager: Менеджер FFmpeg
        """
        try:
            # 1. Получаем информацию о стриме
            stream_info = self.get_stream_info(stream_key)
            if not stream_info or 'stream_key' not in stream_info:
                logger.error("❌ Не удалось получить stream key")
                return False

            stream_key = stream_info['stream_key']
            rtmp_url = stream_info['rtmp_url']

            print(f"\n🔑 Stream Key: {stream_key}")
            print(f"📍 RTMP URL: {rtmp_url}")

            if title:
                print(f"📺 Название: {title}")
            if description:
                print(f"📝 Описание: {description}")

            # 2. Запускаем FFmpeg (если передан менеджер)
            if ffmpeg_manager:
                try:
                    # Устанавливаем stream key
                    ffmpeg_manager.set_stream_key(stream_key)

                    # Запускаем стрим
                    if ffmpeg_manager.start_stream():
                        print("✅ FFmpeg стрим запущен!")
                        print("⚠️  Примечание: API ключ только для чтения")
                        print("   Трансляция должна быть создана в YouTube Studio")
                        return True
                    else:
                        print("❌ Не удалось запустить FFmpeg")
                        return False

                except Exception as e:
                    logger.error(f"❌ Ошибка запуска FFmpeg: {e}")
                    return False

            # 3. Если менеджер не передан, возвращаем данные для ручного запуска
            else:
                print("\n⚠️  FFmpeg не запущен автоматически")
                print("Запустите FFmpeg вручную:")
                print(f"ffmpeg -f lavfi -i color=c=black:s=1920x1080:r=30 \\")
                print(f"       -f lavfi -i anullsrc \\")
                print(f"       -c:v libx264 -c:a aac \\")
                print(f"       -f flv {rtmp_url}")
                print("\n⚠️  Примечание: API ключ только для чтения")
                print("   Трансляция должна быть создана в YouTube Studio")

                return {
                    'stream_key': stream_key,
                    'rtmp_url': rtmp_url
                }

        except Exception as e:
            logger.error(f"❌ Ошибка запуска стрима: {e}")
            return False

    def get_live_broadcasts(self, max_results: int = 10):
        """
        Получение списка активных трансляций (только чтение)

        Args:
            max_results: Максимальное количество результатов
        """
        try:
            if not self.authenticate():
                return None

            request = self.youtube.liveBroadcasts().list(
                part='snippet,status,contentDetails',
                broadcastStatus='active',
                broadcastType='all',
                maxResults=max_results
            )

            response = request.execute()

            broadcasts = []
            for item in response.get('items', []):
                broadcast = {
                    'id': item['id'],
                    'title': item['snippet']['title'],
                    'description': item['snippet']['description'],
                    'status': item['status']['lifeCycleStatus'],
                    'privacy': item['status']['privacyStatus'],
                    'url': f"https://youtube.com/watch?v={item['id']}",
                    'scheduled_time': item['snippet'].get('scheduledStartTime'),
                    'actual_start_time': item['snippet'].get('actualStartTime')
                }
                broadcasts.append(broadcast)

            logger.info(f"📡 Найдено {len(broadcasts)} активных трансляций")
            return broadcasts

        except Exception as e:
            logger.error(f"❌ Ошибка получения трансляций: {e}")
            return None

    def search_live_streams(self, query: str = "", max_results: int = 10):
        """
        Поиск живых стримов

        Args:
            query: Поисковый запрос
            max_results: Максимальное количество результатов
        """
        try:
            if not self.authenticate():
                return None

            request = self.youtube.search().list(
                part='snippet',
                eventType='live',
                type='video',
                q=query,
                maxResults=max_results
            )

            response = request.execute()

            streams = []
            for item in response.get('items', []):
                stream = {
                    'video_id': item['id']['videoId'],
                    'title': item['snippet']['title'],
                    'channel': item['snippet']['channelTitle'],
                    'url': f"https://youtube.com/watch?v={item['id']['videoId']}",
                    'published_at': item['snippet']['publishedAt']
                }
                streams.append(stream)

            logger.info(f"🔍 Найдено {len(streams)} живых стримов")
            return streams

        except Exception as e:
            logger.error(f"❌ Ошибка поиска стримов: {e}")
            return None

    def get_video_statistics(self, video_id: str):
        """
        Получение статистики видео

        Args:
            video_id: ID видео на YouTube
        """
        try:
            if not self.authenticate():
                return None

            request = self.youtube.videos().list(
                part='statistics,snippet,liveStreamingDetails',
                id=video_id
            )

            response = request.execute()

            if not response.get('items'):
                return None

            item = response['items'][0]

            stats = {
                'video_id': video_id,
                'title': item['snippet']['title'],
                'view_count': item['statistics'].get('viewCount', '0'),
                'like_count': item['statistics'].get('likeCount', '0'),
                'comment_count': item['statistics'].get('commentCount', '0'),
                'published_at': item['snippet']['publishedAt']
            }

            # Если это live трансляция
            if 'liveStreamingDetails' in item:
                live_details = item['liveStreamingDetails']
                stats.update({
                    'concurrent_viewers': live_details.get('concurrentViewers', '0'),
                    'actual_start_time': live_details.get('actualStartTime'),
                    'actual_end_time': live_details.get('actualEndTime'),
                    'scheduled_start_time': live_details.get('scheduledStartTime')
                })

            return stats

        except Exception as e:
            logger.error(f"❌ Ошибка получения статистики: {e}")
            return None

    def check_stream_health(self, stream_key: str):
        """
        Проверка здоровья стрима

        Args:
            stream_key: Ключ потока
        """
        try:
            # Создаем тестовое соединение
            import socket

            rtmp_url = f"rtmp://a.rtmp.youtube.com/live2/{stream_key}"
            host = "a.rtmp.youtube.com"
            port = 1935

            # Проверяем доступность сервера
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(5)

            result = sock.connect_ex((host, port))

            if result == 0:
                return {
                    'status': 'healthy',
                    'message': 'Сервер доступен',
                    'rtmp_url': rtmp_url,
                    'stream_key': stream_key
                }
            else:
                return {
                    'status': 'unhealthy',
                    'message': 'Сервер недоступен',
                    'rtmp_url': rtmp_url,
                    'stream_key': stream_key
                }

        except Exception as e:
            return {
                'status': 'error',
                'message': str(e),
                'stream_key': stream_key
            }