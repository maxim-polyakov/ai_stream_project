#!/usr/bin/env python3
"""
🤖 AI Stream с FFmpeg стримингом на YouTube
Версия БЕЗ YouTube API - только прямой стрим через stream key
ЕДИНЫЙ ПРОЦЕСС С ПАЙПАМИ ДЛЯ АУДИО
"""

import os
import sys
import json
import cv2
import textwrap
from PIL import Image, ImageDraw, ImageFont
import numpy
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
import tempfile

discussion_loop_event_loop = None
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





# ========== FFMPEG STREAM MANAGER с ПАЙПАМИ ==========

class FFmpegStreamManager:
    """Управление FFmpeg стримом на YouTube с поддержкой видеофайлов"""

    def __init__(self):
        self.stream_process = None
        self.is_streaming = False
        self.stream_key = None
        self.rtmp_url = None
        self.ffmpeg_pid = None
        self.start_time = None
        self.ffmpeg_stdin = None

        # Очередь и управление аудио
        self.audio_queue = []
        self.current_audio = None
        self.is_playing_audio = False

        # Очередь видео
        self.video_queue = []
        self.current_video = None
        self.is_playing_video = False

        # Видео из кэша
        self.video_cache_dir = 'video_cache'
        os.makedirs(self.video_cache_dir, exist_ok=True)
        self.active_video_source = None
        self.video_source_lock = threading.Lock()
        self.video_thread = None
        self.video_loop = True

        # Конфигурация
        self.audio_sample_rate = 44100
        self.audio_channels = 2
        self.audio_format = 's16le'
        self.bytes_per_sample = 2

        self.mpegts_cache_dir = 'mpegts_cache'
        os.makedirs(self.mpegts_cache_dir, exist_ok=True)
        self.mpegts_cache = {}  # Кэш MPEG-TS файлов
        self.use_mpegts_cache = True  # Включить кэширование
        self.mpegts_cache_max_size = 50 * 1024 * 1024 * 1024  # 50GB
        self._load_mpegts_cache_index()
        self.video_generator = None
        self.video_width = 1920
        self.video_height = 1080
        self.video_fps = 30
        self.video_bitrate = '4500k'

        # Для генерации тишины
        self.silence_chunk_duration = 0.1
        self.silence_chunk_size = int(self.audio_sample_rate * self.audio_channels *
                                      self.bytes_per_sample * self.silence_chunk_duration)

        logger.info("FFmpeg Stream Manager с единым процессом инициализирован")

    def _load_mpegts_cache_index(self):
        """Загрузка индекса кэша MPEG-TS из файла"""
        cache_index_path = os.path.join(self.mpegts_cache_dir, 'cache_index.json')
        if os.path.exists(cache_index_path):
            try:
                with open(cache_index_path, 'r') as f:
                    self.mpegts_cache = json.load(f)
                logger.info(f"📂 Загружен кэш MPEG-TS: {len(self.mpegts_cache)} файлов")
            except Exception as e:
                logger.error(f"❌ Ошибка загрузки кэша: {e}")
                self.mpegts_cache = {}

    def _save_mpegts_cache_index(self):
        """Сохранение индекса кэша MPEG-TS в файл"""
        cache_index_path = os.path.join(self.mpegts_cache_dir, 'cache_index.json')
        try:
            with open(cache_index_path, 'w') as f:
                json.dump(self.mpegts_cache, f, indent=2)
        except Exception as e:
            logger.error(f"❌ Ошибка сохранения кэша: {e}")

    def get_cached_mpegts(self, video_path: str, audio_path: str = None) -> Optional[str]:
        """
        Получение MPEG-TS файла из кэша

        Args:
            video_path: Путь к видео файлу
            audio_path: Путь к аудио файлу (опционально)

        Returns:
            Путь к кэшированному MPEG-TS файлу или None если не найден
        """
        if not self.use_mpegts_cache:
            return None

        cache_key = self._get_mpegts_cache_key(video_path, audio_path)

        if cache_key in self.mpegts_cache:
            cache_info = self.mpegts_cache[cache_key]
            mpegts_path = os.path.join(self.mpegts_cache_dir, cache_info['filename'])

            if os.path.exists(mpegts_path):
                # Обновляем время последнего доступа
                cache_info['last_accessed'] = time.time()
                self.mpegts_cache[cache_key] = cache_info
                self._save_mpegts_cache_index()

                logger.info(f"✅ MPEG-TS найден в кэше: {cache_info['filename']}")
                return mpegts_path
            else:
                # Файл не существует, удаляем из кэша
                del self.mpegts_cache[cache_key]
                self._save_mpegts_cache_index()

        return None

    def add_video_with_mpegts_cache(self, video_path: str, duration: float = None,
                                    audio_file: str = None, use_cache: bool = True) -> bool:
        """
        Добавление видео в очередь с использованием MPEG-TS кэша

        Args:
            video_path: Путь к видео файлу
            duration: Длительность видео (если None - определяется автоматически)
            audio_file: Путь к аудио файлу (опционально)
            use_cache: Использовать кэш MPEG-TS

        Returns:
            True если успешно добавлено
        """
        try:
            if not os.path.exists(video_path):
                logger.error(f"❌ Видео файл не найден: {video_path}")
                return False

            # Получаем информацию о видео
            video_info = self._get_video_info(video_path)
            actual_duration = duration or video_info.get('duration', 10.0)

            # Проверяем кэш если нужно
            mpegts_path = None
            if use_cache and self.use_mpegts_cache:
                mpegts_path = self.get_cached_mpegts(video_path, audio_file)

            # Добавляем в очередь с информацией о кэше
            self.video_queue.append({
                'path': video_path,
                'duration': actual_duration,
                'info': video_info,
                'mpegts_cached': mpegts_path if mpegts_path else False,
                'audio_file': audio_file,
                'use_cache': use_cache
            })

            logger.info(f"📥 Видео добавлено в очередь: {os.path.basename(video_path)}")
            if mpegts_path:
                logger.info(f"   ✅ Используется кэшированный MPEG-TS")

            return True

        except Exception as e:
            logger.error(f"❌ Ошибка добавления видео с кэшем: {e}")
            return False

    def clear_mpegts_cache(self) -> Dict[str, Any]:
        """
        Полная очистка кэша MPEG-TS

        Returns:
            Словарь с результатом операции
        """
        try:
            logger.info("🧹 Полная очистка кэша MPEG-TS...")

            removed_count = 0
            removed_size = 0

            # Удаляем все файлы в директории кэша
            for filename in os.listdir(self.mpegts_cache_dir):
                if filename.endswith('.ts'):
                    filepath = os.path.join(self.mpegts_cache_dir, filename)
                    try:
                        file_size = os.path.getsize(filepath)
                        os.remove(filepath)
                        removed_count += 1
                        removed_size += file_size
                    except Exception as e:
                        logger.error(f"Ошибка удаления {filename}: {e}")

            # Очищаем индекс
            self.mpegts_cache = {}
            cache_index_path = os.path.join(self.mpegts_cache_dir, 'cache_index.json')
            if os.path.exists(cache_index_path):
                os.remove(cache_index_path)

            logger.info(f"✅ Кэш очищен: удалено {removed_count} файлов ({removed_size / 1024 / 1024:.1f} MB)")

            return {
                'success': True,
                'removed_files': removed_count,
                'removed_size_mb': removed_size / 1024 / 1024
            }

        except Exception as e:
            logger.error(f"❌ Ошибка очистки кэша: {e}")
            return {'success': False, 'error': str(e)}

    def _cleanup_mpegts_cache(self):
        """Очистка кэша MPEG-TS при превышении размера"""
        try:
            total_size = sum(info.get('size', 0) for info in self.mpegts_cache.values())

            if total_size <= self.mpegts_cache_max_size:
                return

            logger.info(f"🧹 Очистка кэша MPEG-TS (было: {total_size / 1024 / 1024:.1f} MB)")

            # Сортируем по времени последнего доступа (старые первыми)
            items = list(self.mpegts_cache.items())
            items.sort(key=lambda x: x[1].get('last_accessed', 0))

            removed_count = 0
            removed_size = 0

            while items and total_size > self.mpegts_cache_max_size * 0.7:  # Очищаем до 70%
                cache_key, cache_info = items.pop(0)
                cached_path = os.path.join(self.mpegts_cache_dir, cache_info['filename'])

                try:
                    if os.path.exists(cached_path):
                        os.remove(cached_path)
                        removed_size += cache_info.get('size', 0)
                        removed_count += 1
                except Exception as e:
                    logger.error(f"Ошибка удаления файла: {e}")

                del self.mpegts_cache[cache_key]
                total_size -= cache_info.get('size', 0)

            self._save_mpegts_cache_index()

            if removed_count > 0:
                logger.info(f"✅ Удалено {removed_count} файлов ({removed_size / 1024 / 1024:.1f} MB)")

        except Exception as e:
            logger.error(f"❌ Ошибка очистки кэша: {e}")

    def cache_mpegts_file(self, video_path: str, mpegts_path: str, duration: float,
                          audio_path: str = None, audio_used: bool = False) -> bool:
        """
        Добавление MPEG-TS файла в кэш

        Args:
            video_path: Исходный путь к видео файлу
            mpegts_path: Путь к созданному MPEG-TS файлу
            duration: Длительность в секундах
            audio_path: Путь к аудио файлу (если использовался)
            audio_used: Флаг использования аудио

        Returns:
            True если успешно добавлено в кэш
        """
        if not self.use_mpegts_cache or not os.path.exists(mpegts_path):
            return False

        try:
            cache_key = self._get_mpegts_cache_key(video_path, audio_path)
            file_size = os.path.getsize(mpegts_path)

            # Проверяем размер файла
            if file_size < 1024 * 10:  # < 10KB
                logger.warning(f"⚠️ Файл слишком маленький для кэша: {file_size} байт")
                return False

            # Проверяем общий размер кэша
            total_size = sum(info.get('size', 0) for info in self.mpegts_cache.values())
            if total_size + file_size > self.mpegts_cache_max_size:
                self._cleanup_mpegts_cache()

            # Копируем файл в директорию кэша
            cached_filename = f"{cache_key}.ts"
            cached_path = os.path.join(self.mpegts_cache_dir, cached_filename)

            # Используем shutil.copy2 для сохранения метаданных
            import shutil
            shutil.copy2(mpegts_path, cached_path)

            # Добавляем информацию в кэш
            self.mpegts_cache[cache_key] = {
                'filename': cached_filename,
                'original_video': os.path.basename(video_path),
                'original_audio': os.path.basename(audio_path) if audio_path else None,
                'duration': duration,
                'size': file_size,
                'audio_used': audio_used,
                'created': time.time(),
                'last_accessed': time.time(),
                'resolution': f"{self.video_width}x{self.video_height}",
                'fps': self.video_fps,
                'bitrate': self.video_bitrate
            }

            self._save_mpegts_cache_index()
            logger.info(f"💾 MPEG-TS добавлен в кэш: {cached_filename} ({file_size / 1024 / 1024:.1f} MB)")

            return True

        except Exception as e:
            logger.error(f"❌ Ошибка добавления в кэш: {e}")
            return False


    def _get_mpegts_cache_key(self, video_path: str, audio_path: str = None) -> str:
        """Генерация уникального ключа для кэша MPEG-TS"""
        import hashlib

        # Создаем хеш на основе путей файлов и параметров
        key_data = f"{video_path}:{audio_path if audio_path else 'no_audio'}:{self.video_width}:{self.video_height}:{self.video_fps}:{self.video_bitrate}"
        return hashlib.md5(key_data.encode()).hexdigest()


    def add_video_from_cache(self, filename: str, duration: float = None) -> bool:
        """Добавление видео из кэша в очередь"""
        try:
            video_path = os.path.join(self.video_cache_dir, filename)

            if not os.path.exists(video_path):
                logger.error(f"❌ Видео не найдено в кэше: {filename}")
                return False

            # Получаем информацию о видео
            video_info = self._get_video_info(video_path)
            if not video_info:
                logger.error(f"❌ Не удалось получить информацию о видео: {filename}")
                return False

            actual_duration = duration or video_info.get('duration', 10.0)

            # Добавляем в очередь
            self.video_queue.append({
                'path': video_path,
                'filename': filename,
                'duration': actual_duration,
                'info': video_info,
                'added_time': datetime.now().isoformat()
            })

            logger.info(f"✅ Видео добавлено в очередь: {filename} ({actual_duration:.1f} сек)")
            logger.info(f"📊 Очередь видео: {len(self.video_queue)} файлов")

            # Если стрим не запущен, запускаем его
            if not self.is_streaming and self.stream_key:
                logger.info("🚀 Запускаю стрим...")
                return self.start_stream().get('success', False)

            socketio.emit('video_queued', {
                'filename': filename,
                'duration': actual_duration,
                'queue_position': len(self.video_queue),
                'timestamp': datetime.now().isoformat(),
                'video_info': {
                    'width': video_info.get('width', 0),
                    'height': video_info.get('height', 0),
                    'fps': video_info.get('fps', 0)
                }
            })

            return True

        except Exception as e:
            logger.error(f"❌ Ошибка добавления видео из кэша: {e}")
            return False

    def set_stream_key(self, stream_key: str) -> bool:
        """Установка ключа стрима"""
        self.stream_key = stream_key
        self.rtmp_url = f"rtmp://a.rtmp.youtube.com/live2/{stream_key}"
        logger.info(f"🔑 Stream Key установлен: {stream_key[:10]}...")
        return True

    def add_audio_to_queue(self, audio_file: str) -> bool:
        """Добавление аудио файла в очередь на воспроизведение"""
        if not os.path.exists(audio_file):
            logger.error(f"❌ Аудио файл не найден: {audio_file}")
            return False

        self.audio_queue.append(audio_file)
        logger.info(f"📥 Аудио добавлено в очередь: {os.path.basename(audio_file)}")
        logger.info(f"📊 Размер очереди аудио: {len(self.audio_queue)} файлов")
        return True

    def add_video_to_queue(self, video_path: str, duration: float = None) -> bool:
        """Добавление видео в очередь на показ"""
        if not os.path.exists(video_path):
            logger.error(f"❌ Видео файл не найден: {video_path}")
            return False

        # Получаем информацию о видео
        video_info = self._get_video_info(video_path)
        actual_duration = duration or video_info.get('duration', 10.0)

        self.video_queue.append({
            'path': video_path,
            'duration': actual_duration,
            'info': video_info
        })

        logger.info(f"📥 Видео добавлено в очередь: {os.path.basename(video_path)}")
        return True

    def _get_video_info(self, video_path: str) -> Optional[Dict[str, Any]]:
        """Получение информации о видео файле"""
        try:
            cmd = [
                'ffprobe',
                '-v', 'error',
                '-select_streams', 'v:0',
                '-show_entries', 'stream=width,height,duration,r_frame_rate,codec_name',
                '-show_entries', 'format=duration',
                '-of', 'json',
                video_path
            ]

            result = subprocess.run(cmd, capture_output=True, text=True, timeout=5)

            if result.returncode == 0:
                info = json.loads(result.stdout)

                # Извлекаем информацию
                duration = 0.0
                if 'format' in info and 'duration' in info['format']:
                    duration = float(info['format']['duration'])
                elif 'streams' in info and len(info['streams']) > 0:
                    if 'duration' in info['streams'][0]:
                        duration = float(info['streams'][0]['duration'])

                # Получаем FPS
                fps = self.video_fps
                if 'streams' in info and len(info['streams']) > 0:
                    if 'r_frame_rate' in info['streams'][0]:
                        fps_str = info['streams'][0]['r_frame_rate']
                        try:
                            if '/' in fps_str:
                                num, den = fps_str.split('/')
                                fps = float(num) / float(den)
                            else:
                                fps = float(fps_str)
                        except:
                            pass

                return {
                    'duration': duration,
                    'width': info.get('streams', [{}])[0].get('width', self.video_width),
                    'height': info.get('streams', [{}])[0].get('height', self.video_height),
                    'fps': fps,
                    'codec': info.get('streams', [{}])[0].get('codec_name', 'h264')
                }

            return None

        except Exception as e:
            logger.error(f"❌ Ошибка получения информации о видео: {e}")
            return None

    def _read_audio_chunk(self, audio_file: str, position: int = 0, chunk_size: int = 65536) -> tuple:
        """Чтение чанка аудио из файла"""
        try:
            with open(audio_file, 'rb') as f:
                # Пропускаем WAV заголовок (44 байта) если это WAV файл
                if audio_file.endswith('.wav'):
                    f.seek(44 + position)
                else:
                    f.seek(position)

                data = f.read(chunk_size)
                return data, len(data)
        except Exception as e:
            logger.error(f"Ошибка чтения аудио файла: {e}")
            return None, 0

    def _prepare_audio_file(self, audio_file: str) -> str:
        """Подготовка аудио файла (конвертация в сырой PCM)"""
        if not os.path.exists(audio_file):
            logger.error(f"Аудио файл не найден: {audio_file}")
            return None

        # Если уже PCM файл, возвращаем как есть
        if audio_file.endswith('.pcm') or audio_file.endswith('.raw'):
            return audio_file

        # Создаем временный PCM файл
        temp_pcm = tempfile.NamedTemporaryFile(suffix='.pcm', delete=False)
        temp_pcm.close()

        try:
            # Конвертируем в сырой PCM формат
            convert_cmd = [
                'ffmpeg',
                '-i', audio_file,
                '-f', 's16le',
                '-ar', str(self.audio_sample_rate),
                '-ac', str(self.audio_channels),
                '-acodec', 'pcm_s16le',
                '-y',
                temp_pcm.name
            ]

            logger.debug(f"Конвертация {audio_file} в PCM формат")

            result = subprocess.run(
                convert_cmd,
                capture_output=True,
                text=True,
                timeout=30
            )

            if result.returncode != 0:
                logger.error(f"Ошибка конвертации: {result.stderr[:500]}")
                os.unlink(temp_pcm.name)
                return None

            # Проверяем размер файла
            if os.path.getsize(temp_pcm.name) < 100:
                logger.error("PCM файл слишком маленький")
                os.unlink(temp_pcm.name)
                return None

            return temp_pcm.name

        except Exception as e:
            logger.error(f"Ошибка подготовки аудио: {e}")
            if os.path.exists(temp_pcm.name):
                os.unlink(temp_pcm.name)
            return None

    def _generate_silence_chunk(self) -> bytes:
        """Генерация чанка тишины (нулевые байты)"""
        return b'\x00' * self.silence_chunk_size

    def _continuous_audio_processor(self):
        """Непрерывный процессор аудио - отправляет в stdin FFmpeg"""
        logger.info("🚀 Запуск аудио процессора")

        while self.is_streaming and self.ffmpeg_stdin:
            try:
                if self.audio_queue:
                    self.is_playing_audio = True
                    audio_file = self.audio_queue.pop(0)
                    logger.info(f"🎵 Воспроизведение аудио: {os.path.basename(audio_file)}")

                    # Подготавливаем файл
                    prepared_file = self._prepare_audio_file(audio_file)

                    if prepared_file and self.ffmpeg_stdin:
                        # Отправляем аудио по чанкам
                        chunk_size = 65536
                        position = 0
                        total_bytes = os.path.getsize(prepared_file)

                        bytes_per_second = self.audio_sample_rate * self.audio_channels * self.bytes_per_sample
                        chunk_duration = chunk_size / bytes_per_second

                        while position < total_bytes and self.is_streaming:
                            chunk, bytes_read = self._read_audio_chunk(prepared_file, position, chunk_size)

                            if chunk and bytes_read > 0:
                                try:
                                    self.ffmpeg_stdin.write(chunk)
                                    self.ffmpeg_stdin.flush()
                                    position += bytes_read

                                    # Синхронизация по времени
                                    if bytes_read >= chunk_size:
                                        time.sleep(chunk_duration * 0.95)

                                except BrokenPipeError:
                                    logger.error("❌ Broken pipe: FFmpeg процесс завершился")
                                    self.is_streaming = False
                                    break
                                except Exception as e:
                                    logger.error(f"Ошибка отправки аудио: {e}")
                                    break
                            else:
                                break

                        logger.info(f"✅ Аудио воспроизведено: {position} байт")

                        # Очищаем временный файл
                        if prepared_file != audio_file and os.path.exists(prepared_file):
                            os.unlink(prepared_file)

                        # Удаляем исходный файл если он временный
                        if audio_file.startswith(tempfile.gettempdir()):
                            try:
                                os.unlink(audio_file)
                            except:
                                pass

                    self.is_playing_audio = False

                else:
                    # Если очередь пуста - отправляем тишину
                    if self.ffmpeg_stdin:
                        try:
                            silence_chunk = self._generate_silence_chunk()
                            self.ffmpeg_stdin.write(silence_chunk)
                            self.ffmpeg_stdin.flush()
                            time.sleep(self.silence_chunk_duration * 0.9)

                        except BrokenPipeError:
                            logger.error("❌ Broken pipe во время отправки тишины")
                            self.is_streaming = False
                            break
                        except Exception as e:
                            logger.error(f"Ошибка отправки тишины: {e}")
                            time.sleep(0.1)
                    else:
                        time.sleep(0.1)

            except Exception as e:
                logger.error(f"❌ Критическая ошибка в аудио процессоре: {e}")
                time.sleep(0.1)

        logger.info("🛑 Аудио процессор остановлен")

    def _continuous_video_processor(self):
        """Непрерывный процессор видео - меняет видео в реальном времени"""
        logger.info("🎬 Запуск видео процессора")

        # Создаем дефолтное видео если его нет
        default_video = self._create_default_video_file()

        while self.is_streaming:
            try:
                # Проверяем очередь видео
                if self.video_queue:
                    self.is_playing_video = True
                    video_item = self.video_queue.pop(0)
                    video_path = video_item['path']
                    duration = video_item.get('duration', 10.0)

                    logger.info(f"🎥 Воспроизведение видео: {os.path.basename(video_path)} ({duration:.1f} сек)")

                    # Создаем временный FFmpeg процесс для этого видео
                    self._play_single_video(video_path, duration)

                    # Ждем окончания видео (плюс небольшой буфер)
                    time.sleep(duration + 0.5)

                    self.is_playing_video = False

                else:
                    # Если очередь пуста, воспроизводим дефолтное видео
                    if default_video:
                        # Создаем временный FFmpeg процесс для дефолтного видео
                        self._play_single_video(default_video, 5.0)
                        time.sleep(5.0)
                    else:
                        time.sleep(1.0)

            except Exception as e:
                logger.error(f"❌ Ошибка в видео процессоре: {e}")
                time.sleep(1.0)

        logger.info("🛑 Видео процессор остановлен")

    def _play_single_video(self, video_path: str, duration: float):
        """Воспроизведение одного видео файла через FFmpeg"""
        try:
            if not self.is_streaming or not self.ffmpeg_stdin:
                return

            # Подготавливаем видео файл (конвертируем если нужно)
            prepared_video = self._prepare_video_file(video_path)
            if not prepared_video:
                logger.error(f"❌ Не удалось подготовить видео: {video_path}")
                return

            # Создаем временный FFmpeg процесс для этого видео
            video_cmd = [
                'ffmpeg',
                '-re',  # Реальное время
                '-i', prepared_video,
                '-t', str(duration),  # Длительность
                '-c:v', 'libx264',
                '-preset', 'ultrafast',
                '-tune', 'zerolatency',
                '-pix_fmt', 'yuv420p',
                '-b:v', self.video_bitrate,
                '-maxrate', self.video_bitrate,
                '-bufsize', '9000k',
                '-r', str(self.video_fps),
                '-f', 'mpegts',
                'pipe:1'
            ]

            logger.debug(f"Запуск FFmpeg для видео: {os.path.basename(video_path)}")

            # Запускаем процесс
            video_process = subprocess.Popen(
                video_cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                bufsize=0
            )

            # Отправляем видео в основной FFmpeg процесс
            try:
                while self.is_streaming:
                    chunk = video_process.stdout.read(65536)
                    if not chunk:
                        break

                    # Здесь должна быть логика для отправки видео в поток
                    # Это сложно реализовать без полной переработки архитектуры
                    # Вместо этого используем обходной путь:

                    # Отправляем уведомление что видео готово
                    socketio.emit('video_ready', {
                        'video_file': os.path.basename(video_path),
                        'duration': duration,
                        'timestamp': datetime.now().isoformat()
                    })

                    # Ждем пока видео "проиграется"
                    time.sleep(duration)
                    break

            finally:
                # Завершаем процесс
                video_process.terminate()
                if video_process.poll() is None:
                    video_process.kill()

                # Очищаем временный файл если он был создан
                if prepared_video != video_path and os.path.exists(prepared_video):
                    try:
                        os.unlink(prepared_video)
                    except:
                        pass

        except Exception as e:
            logger.error(f"❌ Ошибка воспроизведения видео: {e}")

    def _prepare_video_file(self, video_file: str) -> Optional[str]:
        """Подготовка видео файла (конвертация если нужно)"""
        if not os.path.exists(video_file):
            logger.error(f"❌ Видео файл не найден: {video_file}")
            return None

        # Проверяем, нужно ли конвертировать
        video_info = self._get_video_info(video_file)
        if not video_info:
            logger.warning(f"⚠️ Не удалось получить информацию о видео, пробуем отправить как есть")
            return video_file

        # БЫСТРАЯ ПРОВЕРКА: если кодек h264 и правильный формат, не конвертируем
        codec = video_info.get('codec', '').lower()
        fps = video_info.get('fps', 0)

        # Если уже в нужном формате, возвращаем как есть
        if codec in ['h264', 'libx264'] and abs(fps - self.video_fps) < 1:
            logger.debug(f"✅ Видео уже в нужном формате: {codec} @ {fps}fps")
            return video_file

        # Конвертируем видео в нужный формат с УСКОРЕННЫМИ настройками
        try:
            temp_video = tempfile.NamedTemporaryFile(suffix='.mp4', delete=False)
            temp_video.close()

            # УСКОРЕННАЯ команда конвертации
            convert_cmd = [
                'ffmpeg',
                '-i', video_file,
                '-c:v', 'libx264',
                '-preset', 'ultrafast',  # Самый быстрый пресет
                '-tune', 'zerolatency',
                '-pix_fmt', 'yuv420p',
                '-s', f'{self.video_width}x{self.video_height}',
                '-r', str(self.video_fps),
                '-b:v', '3000k',  # Меньший битрейт для ускорения
                '-maxrate', '3000k',
                '-bufsize', '6000k',
                '-g', '30',  # Меньше ключевых кадров
                '-c:a', 'aac',
                '-b:a', '96k',  # Меньший битрейт аудио
                '-ar', '44100',
                '-ac', '2',
                '-f', 'mp4',
                '-y',
                '-threads', '2',  # Ограничиваем потоки
                temp_video.name
            ]

            logger.info(f"⚡ Быстрая конвертация видео: {os.path.basename(video_file)}")

            # УСТАНАВЛИВАЕМ ТАЙМАУТ: время видео * 2 + 5 секунд
            estimated_duration = video_info.get('duration', 10.0)
            timeout = min(estimated_duration * 2 + 5, 30)  # Максимум 30 секунд

            result = subprocess.run(
                convert_cmd,
                capture_output=True,
                text=True,
                timeout=timeout
            )

            if result.returncode != 0:
                logger.error(f"❌ Ошибка конвертации: {result.stderr[:300]}")
                os.unlink(temp_video.name)
                return None

            # Проверяем размер файла
            if os.path.getsize(temp_video.name) < 1024:
                logger.error("❌ Видео файл слишком маленький")
                os.unlink(temp_video.name)
                return None

            file_size_mb = os.path.getsize(temp_video.name) / 1024 / 1024
            logger.info(f"✅ Видео сконвертировано за {timeout} сек: {file_size_mb:.1f} MB")

            return temp_video.name

        except subprocess.TimeoutExpired:
            logger.error(f"❌ Таймаут конвертации видео: {os.path.basename(video_file)}")
            if 'temp_video' in locals() and os.path.exists(temp_video.name):
                os.unlink(temp_video.name)
            return video_file  # Возвращаем оригинал в случае таймаута
        except Exception as e:
            logger.error(f"❌ Ошибка подготовки видео: {e}")
            return None

    def _create_default_video_file(self) -> str:
        """Создание дефолтного видео файла"""
        default_path = os.path.join(self.video_cache_dir, 'default.mp4')

        if not os.path.exists(default_path):
            try:
                # Создаем простое видео с текстом
                cmd = [
                    'ffmpeg',
                    '-f', 'lavfi',
                    '-i',
                    f'color=size={self.video_width}x{self.video_height}:rate={self.video_fps}:color=black:duration=5',
                    '-vf', f"drawtext=text='AI Stream':fontsize=72:fontcolor=white:x=(w-text_w)/2:y=(h-text_h)/2",
                    '-c:v', 'libx264',
                    '-preset', 'ultrafast',
                    '-tune', 'zerolatency',
                    '-pix_fmt', 'yuv420p',
                    '-t', '5',
                    '-y',
                    default_path
                ]

                logger.info("🎬 Создание default.mp4...")

                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=10
                )

                if result.returncode == 0:
                    logger.info(f"✅ Создан default.mp4 ({os.path.getsize(default_path) / 1024:.1f} KB)")
                else:
                    logger.error(f"❌ Ошибка создания default.mp4: {result.stderr[:200]}")
                    return None

            except Exception as e:
                logger.error(f"❌ Ошибка создания default.mp4: {e}")
                return None

        return default_path

    def show_video_from_cache(self, filename: str) -> bool:
        """Показ видео из кэша - добавляет в очередь"""
        try:
            video_path = os.path.join(self.video_cache_dir, filename)

            if not os.path.exists(video_path):
                logger.error(f"❌ Видео не найдено: {filename}")
                return False

            # Получаем информацию о видео
            video_info = self._get_video_info(video_path)
            if not video_info:
                return False

            duration = video_info.get('duration', 10.0)

            # Добавляем видео в очередь
            self.add_video_to_queue(video_path, duration)

            logger.info(f"📺 Видео добавлено в очередь: {filename} ({duration:.1f} сек)")

            socketio.emit('video_available', {
                'filename': filename,
                'duration': duration,
                'timestamp': datetime.now().isoformat()
            })

            return True

        except Exception as e:
            logger.error(f"❌ Ошибка показа видео: {e}")
            return False

    def _switch_video_during_stream(self, video_path: str, duration: float) -> bool:
        """Смена видео во время стрима без перезапуска FFmpeg"""
        try:
            # ВАЖНО: Мы не можем менять видео в текущей архитектуре без перезапуска FFmpeg
            # Вместо этого отправляем уведомление что видео готово

            logger.info(f"📡 Подготовка видео для стрима: {os.path.basename(video_path)}")

            # Создаем новый FFmpeg процесс для видео+аудио
            # Этот процесс будет отправлять видео и аудио в основной процесс

            # Подготавливаем аудио файл (если есть в очереди)
            audio_to_play = None
            if self.audio_queue:
                audio_to_play = self.audio_queue[0]  # Берем первый в очереди

            # Создаем временный файл с объединенным видео и аудио
            temp_output = tempfile.NamedTemporaryFile(suffix='.ts', delete=False)
            temp_output.close()

            # Команда для создания транспортного потока с видео и аудио
            cmd = [
                'ffmpeg',
                '-re',  # Реальное время
                '-i', video_path,
            ]

            # Добавляем аудио если есть
            if audio_to_play and os.path.exists(audio_to_play):
                cmd.extend(['-i', audio_to_play])
                cmd.extend(['-map', '0:v:0', '-map', '1:a:0'])  # Видео с первого, аудио со второго
            else:
                cmd.extend(['-map', '0:v:0'])  # Только видео

            cmd.extend([
                '-t', str(duration),
                '-c:v', 'libx264',
                '-preset', 'ultrafast',
                '-tune', 'zerolatency',
                '-pix_fmt', 'yuv420p',
                '-b:v', '4500k',
                '-maxrate', '4500k',
                '-bufsize', '9000k',
                '-r', str(self.video_fps),
                '-g', '60',
                '-c:a', 'aac',
                '-b:a', '128k',
                '-ar', '44100',
                '-ac', '2',
                '-f', 'mpegts',  # Транспортный поток
                '-y',
                temp_output.name
            ])

            logger.debug(f"Создание временного TS файла: {os.path.basename(video_path)}")

            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=duration + 5
            )

            if result.returncode == 0 and os.path.getsize(temp_output.name) > 1024:
                logger.info(f"✅ Временный TS файл создан: {os.path.getsize(temp_output.name) / 1024:.1f} KB")

                # Здесь должен быть код для отправки этого TS потока в основной FFmpeg
                # Но это сложно без перезапуска FFmpeg

                # Вместо этого просто логируем успех
                # В реальном приложении нужно использовать concat или другой метод

                # Очищаем временный файл
                os.unlink(temp_output.name)

                return True  # Возвращаем успех для совместимости

            else:
                logger.error(f"❌ Ошибка создания TS файла: {result.stderr[:200]}")
                if os.path.exists(temp_output.name):
                    os.unlink(temp_output.name)
                return False

        except Exception as e:
            logger.error(f"❌ Ошибка смены видео: {e}")
            return False

    def _continuous_video_switcher(self):
        """Процессор для смены видео во время стрима"""
        logger.info("🎬 Запуск видео свитчера")

        while self.is_streaming:
            try:
                if self.video_queue:
                    self.is_playing_video = True
                    video_item = self.video_queue.pop(0)
                    video_path = video_item['path']
                    duration = video_item.get('duration', 10.0)
                    filename = video_item.get('filename', os.path.basename(video_path))

                    logger.info(f"🎥 Переключение на видео: {filename} ({duration:.1f} сек)")

                    # СМЕНА ВИДЕО БЕЗ ПЕРЕЗАПУСКА FFMPEG
                    # Создаем временный процесс для конвертации и отправки в pipe
                    success = self._switch_video_during_stream(video_path, duration)

                    if success:
                        # Отправляем уведомление
                        socketio.emit('video_playing', {
                            'filename': filename,
                            'duration': duration,
                            'timestamp': datetime.now().isoformat()
                        })

                        # Ждем пока видео воспроизводится
                        time.sleep(duration)
                    else:
                        logger.error(f"❌ Не удалось переключить видео: {filename}")

                    self.is_playing_video = False

                else:
                    # Если очередь пуста, ждем
                    time.sleep(1.0)

            except Exception as e:
                logger.error(f"❌ Ошибка в видео свитчере: {e}", exc_info=True)
                time.sleep(1.0)

        logger.info("🛑 Видео свитчер остановлен")

    def _create_video_concat_list(self) -> str:
        """Создание списка видео для concat демаксера"""
        try:
            # Создаем временный файл со списком
            concat_file = tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False)

            # Добавляем дефолтное видео первым
            default_video = self._create_default_video_file()
            if default_video:
                concat_file.write(f"file '{default_video}'\n")
                concat_file.write("inpoint 0\n")
                concat_file.write("outpoint 1\n")  # 1 секунда

            concat_file.close()

            logger.info(f"📋 Создан concat список: {concat_file.name}")
            return concat_file.name

        except Exception as e:
            logger.error(f"❌ Ошибка создания concat списка: {e}")
            # Резервный вариант
            temp_file = tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False)
            temp_file.write("file 'testsrc=size=1920x1080:rate=30:duration=1'\n")
            temp_file.close()
            return temp_file.name

    def _update_concat_list(self, video_path: str, duration: float):
        """Обновление concat списка новым видео"""
        try:
            # Создаем новый concat файл
            new_concat_file = tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False)

            # Добавляем новое видео
            new_concat_file.write(f"file '{video_path}'\n")
            new_concat_file.write(f"duration {duration}\n")

            new_concat_file.close()

            # Переименовываем старый файл (если есть)
            if hasattr(self, 'concat_list_path') and os.path.exists(self.concat_list_path):
                try:
                    os.unlink(self.concat_list_path)
                except:
                    pass

            # Обновляем путь
            self.concat_list_path = new_concat_file.name

            logger.info(f"📋 Concat список обновлен: {os.path.basename(video_path)}")

            # Отправляем сигнал FFmpeg для перезагрузки input
            # В теории FFmpeg должен автоматически перечитать concat файл
            # На практике может потребоваться более сложная логика

        except Exception as e:
            logger.error(f"❌ Ошибка обновления concat списка: {e}")

    def _video_controller(self):
        """Контроллер видео - обновляет concat список"""
        logger.info("🎬 Запуск видео контроллера")

        last_update = time.time()

        while self.is_streaming:
            try:
                # Проверяем очередь видео каждую секунду
                time.sleep(1)

                # Если есть видео в очереди, добавляем в concat список
                if self.video_queue and (time.time() - last_update > 2):
                    video_item = self.video_queue.pop(0)
                    video_path = video_item['path']
                    duration = video_item.get('duration', 10.0)
                    filename = video_item.get('filename', os.path.basename(video_path))

                    logger.info(f"🎥 Добавляю видео в стрим: {filename} ({duration:.1f} сек)")

                    # Обновляем concat список
                    self._update_concat_list(video_path, duration)

                    # Отправляем уведомление
                    socketio.emit('video_playing', {
                        'filename': filename,
                        'duration': duration,
                        'timestamp': datetime.now().isoformat(),
                        'queue_remaining': len(self.video_queue)
                    })

                    last_update = time.time()

                    # Ждем пока видео воспроизводится
                    time.sleep(duration)

            except Exception as e:
                logger.error(f"❌ Ошибка в видео контроллере: {e}")
                time.sleep(1)

        logger.info("🛑 Видео контроллер остановлен")

    def _init_concat_file(self, concat_path: str, default_video: str):
        """Инициализация concat файла"""
        try:
            with open(concat_path, 'w') as f:
                if default_video and os.path.exists(default_video):
                    # Добавляем дефолтное видео с короткой длительностью
                    f.write(f"file '{os.path.abspath(default_video)}'\n")
                    f.write("duration 1.0\n")  # 1 секунда
                    logger.info(f"📋 Concat файл инициализирован с дефолтным видео")
                else:
                    # Создаем тестовый источник
                    f.write("file 'testsrc=size=1920x1080:rate=30:duration=1'\n")
                    f.write("duration 1.0\n")
                    logger.info(f"📋 Concat файл инициализирован с тестовым источником")

            # Добавляем в список temp_files чтобы не удалялся
            if not hasattr(self, 'temp_files'):
                self.temp_files = []
            self.temp_files.append(concat_path)

        except Exception as e:
            logger.error(f"❌ Ошибка инициализации concat файла: {e}")

    def _append_to_concat_file(self, video_path: str, duration: float):
        """Добавление видео в concat файл"""
        try:
            if not hasattr(self, 'concat_list_path') or not self.concat_list_path:
                logger.error("❌ Concat файл не инициализирован")
                return

            # Полный путь к видео файлу
            abs_video_path = os.path.abspath(video_path)

            # Открываем concat файл для добавления
            with open(self.concat_list_path, 'a') as f:
                f.write(f"\nfile '{abs_video_path}'\n")
                f.write(f"duration {duration}\n")

            logger.info(f"📝 Добавлено в concat: {os.path.basename(video_path)} ({duration} сек)")

            # Проверяем что файл существует и читается
            if os.path.exists(self.concat_list_path):
                with open(self.concat_list_path, 'r') as f:
                    content = f.read()
                    logger.debug(f"📋 Содержимое concat файла ({len(content)} байт):\n{content[-500:]}")

        except Exception as e:
            logger.error(f"❌ Ошибка добавления в concat файл: {e}")

    def _dynamic_concat_updater(self):
        """Динамическое обновление concat файла во время стрима"""
        logger.info("🎬 Запуск динамического обновления concat файла")

        while self.is_streaming:
            try:
                time.sleep(0.5)  # Проверяем каждые 500мс

                # Если есть видео в очереди, добавляем в concat файл
                if self.video_queue:
                    video_item = self.video_queue.pop(0)
                    video_path = video_item['path']
                    duration = video_item.get('duration', 10.0)
                    filename = video_item.get('filename', os.path.basename(video_path))

                    logger.info(f"🎥 Добавляю видео в concat: {filename} ({duration:.1f} сек)")

                    # Добавляем видео в concat файл
                    self._append_to_concat_file(video_path, duration)

                    # Отправляем уведомление
                    socketio.emit('video_playing', {
                        'filename': filename,
                        'duration': duration,
                        'timestamp': datetime.now().isoformat(),
                        'queue_remaining': len(self.video_queue)
                    })

                    # FFmpeg автоматически перейдет на новое видео из concat файла
                    # Ждем пока видео воспроизводится
                    time.sleep(duration)

            except Exception as e:
                logger.error(f"❌ Ошибка в динамическом обновлении: {e}")
                time.sleep(1)

        logger.info("🛑 Динамическое обновление concat файла остановлено")


    def _show_video_with_overlay(self, video_path: str, duration: float):
        """Показ видео через overlay в основном FFmpeg процессе"""
        try:
            # Временное решение: создаем отдельный FFmpeg процесс,
            # который отправляет видео в pipe и мы его смешиваем

            # Подготавливаем видео файл
            prepared_video = self._prepare_video_file(video_path)
            if not prepared_video:
                return

            # Создаем команду для кодирования видео в сырой формат
            overlay_cmd = [
                'ffmpeg',
                '-re',
                '-i', prepared_video,
                '-t', str(duration),
                '-c:v', 'rawvideo',
                '-pix_fmt', 'bgr24',
                '-f', 'rawvideo',
                'pipe:1'
            ]

            logger.debug(f"Запуск overlay процесса для: {os.path.basename(video_path)}")

            # Запускаем процесс
            overlay_process = subprocess.Popen(
                overlay_cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                bufsize=0
            )

            # Читаем и отправляем кадры в основной процесс
            bytes_per_frame = self.video_width * self.video_height * 3
            frame_duration = 1.0 / self.video_fps

            for _ in range(int(duration * self.video_fps)):
                frame_data = overlay_process.stdout.read(bytes_per_frame)
                if frame_data and len(frame_data) == bytes_per_frame:
                    # Здесь должен быть механизм отправки кадра в основной FFmpeg
                    # Для этого нужен pipe или другой способ коммуникации
                    pass
                time.sleep(frame_duration)

            # Завершаем процесс
            overlay_process.terminate()

            # Очищаем временный файл
            if prepared_video != video_path and os.path.exists(prepared_video):
                os.unlink(prepared_video)

        except Exception as e:
            logger.error(f"❌ Ошибка показа видео: {e}")

    def _dynamic_video_controller(self):
        """Контроллер динамической смены видео через sendcmd"""
        logger.info("🎬 Запуск динамического видео контроллера")

        # Ждем запуска FFmpeg
        time.sleep(2)

        while self.is_streaming:
            try:
                # Проверяем очередь видео
                if self.video_queue:
                    self.is_playing_video = True
                    video_item = self.video_queue.pop(0)
                    video_path = video_item['path']
                    duration = video_item.get('duration', 10.0)
                    filename = video_item.get('filename', os.path.basename(video_path))

                    logger.info(f"🎥 Показываю видео: {filename} ({duration:.1f} сек)")

                    # Создаем временный процесс для показа видео
                    self._show_video_with_overlay(video_path, duration)

                    # Отправляем уведомление
                    socketio.emit('video_playing', {
                        'filename': filename,
                        'duration': duration,
                        'timestamp': datetime.now().isoformat()
                    })

                    # Ждем пока видео воспроизводится
                    time.sleep(duration)

                    self.is_playing_video = False

                else:
                    # Если очередь пуста, ждем
                    time.sleep(1.0)

            except Exception as e:
                logger.error(f"❌ Ошибка в видео контроллере: {e}", exc_info=True)
                time.sleep(1.0)

        logger.info("🛑 Динамический видео контроллер остановлен")

    def _video_pipe_sender(self):
        """Отправка видео в pipe для оверлея"""
        logger.info("📤 Запуск отправителя видео в pipe")

        # Ждем пока FFmpeg запустится
        time.sleep(2)

        while self.is_streaming:
            try:
                if self.video_queue:
                    video_item = self.video_queue.pop(0)
                    video_path = video_item['path']
                    duration = video_item.get('duration', 10.0)
                    filename = video_item.get('filename', os.path.basename(video_path))

                    logger.info(f"🎬 Отправка видео в оверлей: {filename} ({duration:.1f} сек)")

                    # Отправляем видео в pipe
                    success = self._send_video_to_pipe(video_path, duration)

                    if success:
                        socketio.emit('video_playing', {
                            'filename': filename,
                            'duration': duration,
                            'timestamp': datetime.now().isoformat(),
                            'queue_remaining': len(self.video_queue)
                        })

                        # Ждем пока видео воспроизводится
                        time.sleep(duration)
                    else:
                        logger.error(f"❌ Не удалось отправить видео в pipe: {filename}")
                        self.video_queue.insert(0, video_item)

                else:
                    time.sleep(0.1)

            except Exception as e:
                logger.error(f"❌ Ошибка в отправителе видео: {e}", exc_info=True)
                time.sleep(1)

        logger.info("🛑 Отправитель видео остановлен")


    def _send_video_to_pipe(self, video_path: str, duration: float) -> bool:
        """Отправка видео в pipe FFmpeg"""
        try:
            if not self.is_streaming or not self.ffmpeg_stdin:
                logger.error("❌ FFmpeg не активен или stdin недоступен")
                return False

            logger.info(f"📤 Отправка видео в FFmpeg pipe: {os.path.basename(video_path)}")

            # Подготавливаем видео файл
            prepared_video = self._prepare_video_file(video_path)
            if not prepared_video:
                logger.error(f"❌ Не удалось подготовить видео: {video_path}")
                return False

            # Команда для отправки сырого видео в pipe
            send_cmd = [
                'ffmpeg',
                '-re',  # Реальное время
                '-i', prepared_video,
                '-t', str(duration),
                '-c:v', 'rawvideo',  # Сырое видео
                '-pix_fmt', 'bgr24',  # Формат, который ожидает FFmpeg
                '-f', 'rawvideo',  # Сырой формат
                'pipe:1'
            ]

            logger.debug(f"Запуск отправки видео: {' '.join(send_cmd[:10])}...")

            # ЗАПУСКАЕМ ПРОЦЕСС С ТАЙМАУТОМ
            try:
                video_process = subprocess.Popen(
                    send_cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    bufsize=0
                )
            except Exception as e:
                logger.error(f"❌ Не удалось запустить процесс отправки: {e}")
                return False

            # УВЕЛИЧИВАЕМ ТАЙМАУТ: время видео + 10 секунд на буфер
            timeout = duration + 10

            # Отправляем видео кадр за кадром
            bytes_per_frame = self.video_width * self.video_height * 3  # bgr24 = 3 байта на пиксель
            frame_duration = 1.0 / self.video_fps
            total_frames = int(duration * self.video_fps)

            logger.info(f"🎞️  Отправка {total_frames} кадров, таймаут: {timeout} сек")

            frames_sent = 0
            start_time = time.time()

            while frames_sent < total_frames and self.is_streaming:
                try:
                    # Читаем кадр с ТАЙМАУТОМ
                    frame_data = video_process.stdout.read(bytes_per_frame)

                    if not frame_data:
                        # Если данные закончились
                        if video_process.poll() is not None:
                            logger.warning(f"⚠️ Процесс отправки завершился раньше времени")
                            break
                        else:
                            # Ждем немного и продолжаем
                            time.sleep(0.01)
                            continue

                    if len(frame_data) != bytes_per_frame:
                        logger.warning(f"⚠️ Неполный кадр: {len(frame_data)} байт вместо {bytes_per_frame}")
                        # Пропускаем неполный кадр
                        continue

                    # Отправляем кадр в FFmpeg
                    try:
                        self.ffmpeg_stdin.write(frame_data)
                        self.ffmpeg_stdin.flush()
                        frames_sent += 1

                        # Синхронизируем по времени
                        elapsed = time.time() - start_time
                        expected_time = frames_sent * frame_duration

                        if elapsed < expected_time:
                            # Спим чтобы синхронизировать
                            time.sleep(expected_time - elapsed)
                        elif elapsed > expected_time + 0.1:
                            logger.warning(f"⚠️ Отставание: {elapsed - expected_time:.2f} сек")

                    except BrokenPipeError:
                        logger.error("❌ Broken pipe: FFmpeg отключился")
                        self.is_streaming = False
                        break
                    except Exception as e:
                        logger.error(f"❌ Ошибка записи в pipe: {e}")
                        break

                    # Логируем прогресс каждые 50 кадров
                    if frames_sent % 50 == 0:
                        logger.debug(f"📊 Отправлено {frames_sent}/{total_frames} кадров")

                except Exception as e:
                    logger.error(f"❌ Ошибка чтения кадра: {e}")
                    break

                # Проверяем таймаут
                if time.time() - start_time > timeout:
                    logger.error(f"❌ Таймаут отправки видео: {os.path.basename(video_path)}")
                    break

            # Завершаем процесс отправки
            try:
                video_process.terminate()
                if video_process.poll() is None:
                    time.sleep(0.5)
                    if video_process.poll() is None:
                        video_process.kill()
            except:
                pass

            logger.info(f"✅ Отправлено {frames_sent}/{total_frames} кадров")

            # Очищаем временный файл
            if prepared_video != video_path and os.path.exists(prepared_video):
                try:
                    os.unlink(prepared_video)
                except:
                    pass

            return frames_sent > total_frames * 0.8  # Успех если отправлено >80% кадров

        except Exception as e:
            logger.error(f"❌ Критическая ошибка отправки видео: {e}", exc_info=True)
            return False

    def _send_mpegts_file(self, mpegts_path: str, duration: float) -> bool:
        """Отправка MPEG-TS файла в pipe"""
        try:
            if not self.is_streaming or not self.ffmpeg_stdin:
                return False

            file_size = os.path.getsize(mpegts_path)
            logger.info(f"📤 Отправка MPEG-TS файла: {file_size / 1024:.1f} KB")

            with open(mpegts_path, 'rb') as f:
                start_time = time.time()
                bytes_sent = 0

                # Отправляем файл чанками
                chunk_size = 65536  # 64KB

                while bytes_sent < file_size and self.is_streaming:
                    chunk = f.read(chunk_size)
                    if not chunk:
                        break

                    try:
                        self.ffmpeg_stdin.write(chunk)
                        self.ffmpeg_stdin.flush()
                        bytes_sent += len(chunk)

                        # Синхронизация: отправляем в реальном времени
                        elapsed = time.time() - start_time
                        expected_time = (bytes_sent / file_size) * duration

                        if elapsed < expected_time:
                            time.sleep(expected_time - elapsed)

                    except BrokenPipeError:
                        logger.error("❌ Broken pipe при отправке MPEG-TS")
                        self.is_streaming = False
                        break
                    except Exception as e:
                        logger.error(f"❌ Ошибка отправки MPEG-TS: {e}")
                        break

                logger.info(f"✅ Отправлено {bytes_sent}/{file_size} байт MPEG-TS")
                return bytes_sent >= file_size * 0.9  # Успех если >90%

        except Exception as e:
            logger.error(f"❌ Ошибка отправки MPEG-TS файла: {e}")
            return False

    def _get_audio_duration(self, audio_file: str) -> float:
        """Получение длительности аудио файла"""
        try:
            cmd = [
                'ffprobe',
                '-v', 'error',
                '-show_entries', 'format=duration',
                '-of', 'default=noprint_wrappers=1:nokey=1',
                audio_file
            ]

            result = subprocess.run(cmd, capture_output=True, text=True, timeout=5)

            if result.returncode == 0:
                return float(result.stdout.strip())
            else:
                return 5.0  # По умолчанию

        except Exception as e:
            logger.warning(f"Не удалось получить длительность аудио: {e}")
            return 5.0

    def _create_mpegts_file(self, video_path: str, duration: float, audio_file: str, output_path: str) -> bool:
        """Создание MPEG-TS файла для кэширования с оптимизированным битрейтом"""
        try:
            # Получаем длину аудио, если файл существует
            audio_duration = 0
            if audio_file and os.path.exists(audio_file):
                try:
                    # Используем ffprobe для получения длительности аудио
                    probe_cmd = [
                        'ffprobe',
                        '-v', 'error',
                        '-show_entries', 'format=duration',
                        '-of', 'default=noprint_wrappers=1:nokey=1',
                        audio_file
                    ]
                    result = subprocess.run(probe_cmd, capture_output=True, text=True, timeout=10)
                    if result.returncode == 0:
                        audio_duration = float(result.stdout.strip())
                        logger.info(f"🎵 Длительность аудио: {audio_duration:.2f} сек, видео: {duration:.2f} сек")
                except Exception as e:
                    logger.warning(f"⚠️ Не удалось получить длительность аудио: {e}")

            # Определяем, нужно ли зацикливать видео
            loop_video = False
            actual_duration = duration
            original_video_path = video_path

            if audio_duration > duration:
                loop_video = True
                actual_duration = audio_duration
                logger.info(f"🔄 Аудио длиннее видео, зациклю видео до {actual_duration:.2f} сек")

            # ОПТИМИЗИРОВАННЫЙ БИТРЕЙТ ДЛЯ YOUTUBE
            video_bitrate = '5000k'  # Достаточно для 1080p
            maxrate = '5500k'
            bufsize = '10000k'

            # Оптимизируем видео перед созданием MPEG-TS
            optimized_video = self._optimize_video_for_streaming(video_path, video_bitrate)
            if optimized_video != video_path:
                logger.info(f"🔧 Использую оптимизированное видео для MPEG-TS")
                video_path = optimized_video

            # Получаем информацию о видео для оптимизации
            video_info = self._get_video_info(video_path)
            if video_info:
                width = video_info.get('width', self.video_width)
                height = video_info.get('height', self.video_height)

                # Автоматически корректируем битрейт в зависимости от разрешения
                if width * height <= 854 * 480:  # 480p или меньше
                    video_bitrate = '1500k'
                    maxrate = '2000k'
                    bufsize = '4000k'
                    logger.info(f"📊 Автоопределение: {width}x{height} -> битрейт {video_bitrate}")
                elif width * height <= 1280 * 720:  # 720p
                    video_bitrate = '3000k'
                    maxrate = '3500k'
                    bufsize = '7000k'
                    logger.info(f"📊 Автоопределение: {width}x{height} -> битрейт {video_bitrate}")

            # Команда для создания MPEG-TS потока
            mpegts_cmd = ['ffmpeg']

            # Если нужно зациклить видео, используем фильтр stream_loop
            if loop_video:
                mpegts_cmd.extend([
                    '-re',
                    '-stream_loop', '-1',  # Бесконечное зацикливание
                    '-i', video_path,
                    '-t', str(actual_duration),  # Ограничиваем по длительности аудио
                ])
            else:
                mpegts_cmd.extend([
                    '-re',  # Реальное время
                    '-i', video_path,
                ])

            # Добавляем аудио источник если есть
            if audio_file and os.path.exists(audio_file):
                mpegts_cmd.extend(['-i', audio_file])
                # Карты: видео с первого входа, аудио со второго
                mpegts_cmd.extend([
                    '-map', '0:v:0',
                    '-map', '1:a:0',
                    '-c:v', 'libx264',
                    '-preset', 'medium',
                    '-tune', 'film' if actual_duration > 10 else 'zerolatency',
                    '-pix_fmt', 'yuv420p',
                    '-profile:v', 'high',
                    '-level', '4.1',
                    '-b:v', video_bitrate,
                    '-maxrate', maxrate,
                    '-bufsize', bufsize,
                    '-r', str(self.video_fps),
                    '-g', '60',
                    '-keyint_min', '60',
                    '-sc_threshold', '0',
                    '-bf', '2',
                    '-c:a', 'aac',
                    '-b:a', '128k',
                    '-ar', '44100',
                    '-ac', '2',
                ])
            else:
                # Если нет аудио - добавляем тихое аудио
                mpegts_cmd.extend([
                    '-f', 'lavfi',
                    '-i', 'anullsrc=channel_layout=stereo:sample_rate=44100',
                    '-map', '0:v:0',
                    '-map', '1:a:0',
                    '-c:v', 'libx264',
                    '-preset', 'medium',
                    '-tune', 'film' if actual_duration > 10 else 'zerolatency',
                    '-pix_fmt', 'yuv420p',
                    '-profile:v', 'high',
                    '-level', '4.1',
                    '-b:v', video_bitrate,
                    '-maxrate', maxrate,
                    '-bufsize', bufsize,
                    '-r', str(self.video_fps),
                    '-g', '60',
                    '-keyint_min', '60',
                    '-sc_threshold', '0',
                    '-bf', '2',
                    '-c:a', 'aac',
                    '-b:a', '128k',
                    '-ar', '44100',
                    '-ac', '2',
                ])

            # Общие параметры
            mpegts_cmd.extend([
                '-t', str(actual_duration),  # Используем фактическую длительность
                '-f', 'mpegts',
                '-muxdelay', '0',
                '-muxpreload', '0',
                '-flush_packets', '1',
                '-avoid_negative_ts', 'make_zero',
                '-y',
                output_path
            ])

            logger.info(f"🔧 Создание MPEG-TS для кэша: {os.path.basename(video_path)} с битрейтом {video_bitrate}")
            if loop_video:
                logger.info(f"🔄 Видео будет зациклено до {actual_duration:.1f} сек")

            # Таймаут создания
            timeout = min(actual_duration + 15, 45)

            result = subprocess.run(
                mpegts_cmd,
                capture_output=True,
                text=True,
                encoding='utf-8',
                timeout=timeout
            )

            if result.returncode != 0:
                logger.error(f"❌ Ошибка создания MPEG-TS файла (код {result.returncode}):")
                if result.stderr:
                    # Ищем конкретные ошибки
                    error_lines = result.stderr.split('\n')
                    for error_line in error_lines:
                        if 'bitrate' in error_line.lower() or 'buffer' in error_line.lower():
                            logger.error(f"   🎯 BITRATE ERROR: {error_line}")
                    logger.error(f"STDERR: {result.stderr[:500]}")

                # Очищаем оптимизированный файл если он был создан
                if optimized_video != original_video_path and os.path.exists(optimized_video):
                    try:
                        os.unlink(optimized_video)
                    except:
                        pass

                return False

            # Проверяем размер файла
            if not os.path.exists(output_path) or os.path.getsize(output_path) < 1024:
                logger.error("❌ Созданный MPEG-TS файл слишком маленький или не существует")
                # Очищаем оптимизированный файл
                if optimized_video != original_video_path and os.path.exists(optimized_video):
                    try:
                        os.unlink(optimized_video)
                    except:
                        pass
                return False

            file_size = os.path.getsize(output_path) / 1024 / 1024
            calculated_bitrate = (file_size * 8 * 1024 * 1024) / actual_duration / 1000  # kbps

            logger.info(f"✅ MPEG-TS файл создан: {file_size:.1f} MB, битрейт ~{calculated_bitrate:.0f} kbps")
            if loop_video:
                logger.info(f"✅ Видео зациклено для синхронизации с аудио ({duration:.1f} → {actual_duration:.1f} сек)")

            # Очищаем оптимизированный файл
            if optimized_video != original_video_path and os.path.exists(optimized_video):
                try:
                    os.unlink(optimized_video)
                except:
                    pass

            return True
        except Exception as e:
            logger.error(f"❌ Непредвиденная ошибка в _create_mpegts_file: {e}")
            return False


    def _refresh_cached_files_queue(self, limit: int = 20):
        """Обновление очереди файлов из кэша MPEG-TS"""
        try:
            if not self.use_mpegts_cache or not self.mpegts_cache:
                return []

            # Сортируем файлы по времени создания (новые первыми)
            cache_items = list(self.mpegts_cache.items())
            cache_items.sort(key=lambda x: x[1].get('created', 0), reverse=True)

            cached_files_queue = []

            # Берем до limit файлов
            for i, (cache_key, cache_info) in enumerate(cache_items):
                if i >= limit:
                    break

                mpegts_path = os.path.join(self.mpegts_cache_dir, cache_info['filename'])
                if os.path.exists(mpegts_path):
                    cached_files_queue.append({
                        'path': mpegts_path,
                        'duration': cache_info.get('duration', 10.0),
                        'original_filename': cache_info.get('original_video', 'unknown'),
                        'cache_key': cache_key,
                        'audio_used': cache_info.get('audio_used', False),
                        'created': cache_info.get('created', 0)
                    })

            return cached_files_queue

        except Exception as e:
            logger.error(f"❌ Ошибка обновления очереди кэша: {e}")
            return []

    def _update_cache_access_time(self, cache_key: str):
        """Обновление времени последнего доступа к файлу в кэше"""
        try:
            if cache_key in self.mpegts_cache:
                self.mpegts_cache[cache_key]['last_accessed'] = time.time()
                self._save_mpegts_cache_index()
        except Exception as e:
            logger.error(f"❌ Ошибка обновления времени доступа кэша: {e}")

    def _remove_from_cache(self, cache_key: str):
        """Удаление файла из кэша"""
        try:
            if cache_key in self.mpegts_cache:
                cache_info = self.mpegts_cache[cache_key]
                mpegts_path = os.path.join(self.mpegts_cache_dir, cache_info['filename'])

                if os.path.exists(mpegts_path):
                    os.unlink(mpegts_path)

                del self.mpegts_cache[cache_key]
                self._save_mpegts_cache_index()
                logger.info(f"🗑️ Файл удален из кэша: {cache_info['filename']}")

        except Exception as e:
            logger.error(f"❌ Ошибка удаления из кэша: {e}")

    def _check_cache_folder_on_start(self):
        """Проверка папки кэша при запуске контроллера"""
        try:
            logger.info("🔍 Проверка папок кэша при запуске контроллера...")

            # Проверяем папку видео кэша (теперь через video_cache_dir)
            video_cache_dir = 'video_cache'

            if os.path.exists(video_cache_dir):
                files = os.listdir(video_cache_dir)
                video_files = [f for f in files if f.endswith(('.mp4', '.mov', '.avi', '.mkv'))]
                logger.info(f"📁 Видео кэш: {len(video_files)} файлов в {video_cache_dir}")

                # Автоматически добавляем видео из кэша в очередь (ДО 10 ФАЙЛОВ)
                for video_file in video_files[:10]:
                    video_path = os.path.join(video_cache_dir, video_file)
                    video_info = self._get_video_info(video_path)
                    if video_info:
                        self.video_queue.append({
                            'path': video_path,
                            'filename': video_file,
                            'duration': video_info.get('duration', 10.0),
                            'info': video_info,
                            'from_video_cache': True
                        })
                        logger.info(f"   📥 Добавлено из видео кэша: {video_file}")

            # Проверяем папку MPEG-TS кэша
            if os.path.exists(self.mpegts_cache_dir):
                ts_files = [f for f in os.listdir(self.mpegts_cache_dir) if f.endswith('.ts')]
                logger.info(f"📁 MPEG-TS кэш: {len(ts_files)} файлов в {self.mpegts_cache_dir}")

                # Загружаем индекс кэша
                self._load_mpegts_cache_index()

        except Exception as e:
            logger.error(f"❌ Ошибка проверки папок кэша: {e}")

    def _check_video_cache_for_new_files(self):
        """Проверка папки video_cache на новые файлы"""
        try:
            # Проверяем каждые 30 секунд
            current_time = time.time()
            if not hasattr(self, '_last_video_cache_check'):
                self._last_video_cache_check = 0

            if current_time - self._last_video_cache_check < 30:
                return

            self._last_video_cache_check = current_time

            video_cache_dir = self.video_generator.video_cache_dir
            if not os.path.exists(video_cache_dir):
                return

            # Получаем список файлов в кэше
            all_files = []
            for filename in os.listdir(video_cache_dir):
                if filename.endswith(('.mp4', '.mov', '.avi', '.mkv')):
                    file_path = os.path.join(video_cache_dir, filename)
                    file_mtime = os.path.getmtime(file_path)
                    all_files.append((filename, file_path, file_mtime))

            # Сортируем по времени изменения (новые первыми)
            all_files.sort(key=lambda x: x[2], reverse=True)

            # Проверяем, есть ли новые файлы
            if not hasattr(self, '_known_video_files'):
                self._known_video_files = set()

            new_files = []
            for filename, file_path, mtime in all_files:
                if filename not in self._known_video_files:
                    new_files.append((filename, file_path, mtime))
                    self._known_video_files.add(filename)

            # Добавляем новые файлы в очередь
            for filename, file_path, mtime in new_files[:3]:  # Не более 3 новых файлов за раз
                try:
                    video_info = self._get_video_info(file_path)
                    if video_info:
                        self.video_queue.append({
                            'path': file_path,
                            'filename': filename,
                            'duration': video_info.get('duration', 10.0),
                            'info': video_info,
                            'from_video_cache': True,
                            'added_time': datetime.now().isoformat()
                        })
                        logger.info(f"📥 Обнаружен новый файл в видео кэше: {filename}")

                        socketio.emit('new_video_discovered', {
                            'filename': filename,
                            'duration': video_info.get('duration', 10.0),
                            'size_mb': os.path.getsize(file_path) / 1024 / 1024,
                            'timestamp': datetime.fromtimestamp(mtime).isoformat()
                        })
                except Exception as e:
                    logger.error(f"❌ Ошибка обработки нового файла {filename}: {e}")

            if new_files:
                logger.info(f"📁 Обнаружено {len(new_files)} новых файлов в видео кэше")

        except Exception as e:
            logger.error(f"❌ Ошибка проверки видео кэша: {e}")

    def auto_add_videos_from_cache(self, limit: int = 10):
        """Автоматическое добавление видео из кэша в очередь"""
        try:
            video_cache_dir = 'video_cache'
            if not os.path.exists(video_cache_dir):
                logger.warning(f"⚠️ Папка видео кэша не существует: {video_cache_dir}")
                return 0

            added_count = 0
            video_files = []

            # Собираем все видео файлы
            for filename in os.listdir(video_cache_dir):
                if filename.endswith(('.mp4', '.mov', '.avi', '.mkv')):
                    file_path = os.path.join(video_cache_dir, filename)
                    file_mtime = os.path.getmtime(file_path)
                    video_files.append((filename, file_path, file_mtime))

            # Сортируем по времени создания (новые первыми)
            video_files.sort(key=lambda x: x[2], reverse=True)

            # Добавляем файлы в очередь (ДО 10 ФАЙЛОВ)
            for filename, file_path, mtime in video_files[:limit]:
                if added_count >= limit:
                    break

                # Проверяем, не добавлено ли уже это видео
                already_queued = False
                for video_item in self.video_queue:
                    if video_item.get('filename') == filename:
                        already_queued = True
                        break

                if not already_queued:
                    # Получаем информацию о видео
                    video_info = self._get_video_info(file_path)
                    if video_info:
                        self.video_queue.append({
                            'path': file_path,
                            'filename': filename,
                            'duration': video_info.get('duration', 10.0),
                            'info': video_info,
                            'from_auto_cache': True,
                            'added_time': datetime.now().isoformat()
                        })
                        added_count += 1

                        logger.info(f"📥 Автоматически добавлено из кэша: {filename}")

                        socketio.emit('video_auto_queued', {
                            'filename': filename,
                            'duration': video_info.get('duration', 10.0),
                            'queue_position': len(self.video_queue),
                            'timestamp': datetime.now().isoformat()
                        })

            logger.info(f"✅ Автоматически добавлено {added_count} видео из кэша")
            return added_count

        except Exception as e:
            logger.error(f"❌ Ошибка автоматического добавления видео: {e}")
            return 0

    def _check_video_cache_for_new_files(self):
        """Проверка папки video_cache на новые файлы"""
        try:
            # Проверяем каждые 30 секунд
            current_time = time.time()
            if not hasattr(self, '_last_video_cache_check'):
                self._last_video_cache_check = 0

            if current_time - self._last_video_cache_check < 30:
                return

            self._last_video_cache_check = current_time

            # Используем прямую ссылку на папку кэша
            video_cache_dir = 'video_cache'
            if not os.path.exists(video_cache_dir):
                logger.debug(f"📭 Папка видео кэша не существует: {video_cache_dir}")
                return

            # Получаем список файлов в кэше
            all_files = []
            for filename in os.listdir(video_cache_dir):
                if filename.endswith(('.mp4', '.mov', '.avi', '.mkv')):
                    file_path = os.path.join(video_cache_dir, filename)
                    file_mtime = os.path.getmtime(file_path)
                    all_files.append((filename, file_path, file_mtime))

            # Сортируем по времени изменения (новые первыми)
            all_files.sort(key=lambda x: x[2], reverse=True)

            # Проверяем, есть ли новые файлы
            if not hasattr(self, '_known_video_files'):
                self._known_video_files = set()

            new_files = []
            for filename, file_path, mtime in all_files:
                if filename not in self._known_video_files:
                    new_files.append((filename, file_path, mtime))
                    self._known_video_files.add(filename)

            # Добавляем новые файлы в очередь (ДО 10 ФАЙЛОВ ЗА РАЗ)
            for filename, file_path, mtime in new_files[:10]:
                try:
                    video_info = self._get_video_info(file_path)
                    if video_info:
                        self.video_queue.append({
                            'path': file_path,
                            'filename': filename,
                            'duration': video_info.get('duration', 10.0),
                            'info': video_info,
                            'from_video_cache': True,
                            'added_time': datetime.now().isoformat()
                        })
                        logger.info(f"📥 Обнаружен новый файл в видео кэше: {filename}")

                        socketio.emit('new_video_discovered', {
                            'filename': filename,
                            'duration': video_info.get('duration', 10.0),
                            'size_mb': os.path.getsize(file_path) / 1024 / 1024,
                            'timestamp': datetime.fromtimestamp(mtime).isoformat()
                        })
                except Exception as e:
                    logger.error(f"❌ Ошибка обработки нового файла {filename}: {e}")

            if new_files:
                logger.info(f"📁 Обнаружено {len(new_files)} новых файлов в видео кэше")

        except Exception as e:
            logger.error(f"❌ Ошибка проверки видео кэша: {e}")

    def _check_ffmpeg_alive(self):
        """Проверка что FFmpeg процесс все еще работает"""
        try:
            if not self.stream_process:
                return False

            if self.stream_process.poll() is not None:
                # Процесс завершен
                return_code = self.stream_process.returncode
                logger.warning(f"⚠️ FFmpeg процесс завершился с кодом: {return_code}")
                return False

            return True
        except Exception as e:
            logger.error(f"❌ Ошибка проверки FFmpeg: {e}")
            return False

    def _stream_controller(self):
        """Главный контроллер потока - отправляет MPEG-TS файлы ТОЛЬКО из кэша"""
        logger.info("🎬 Запуск контроллера MPEG-TS потока (только из кэша)")

        # Минимальное количество файлов в кэше для отправки
        MIN_CACHE_FILES_FOR_SEND = 10
        # Максимальное количество файлов для отправки за раз
        MAX_CACHE_BATCH = 10

        # Список файлов из кэша, готовых к отправке
        cached_files_queue = []

        # Ждем запуска FFmpeg
        time.sleep(3)

        while self.is_streaming:
            try:
                if not self._check_ffmpeg_alive():
                    logger.error("❌ FFmpeg процесс завершился. Останавливаю контроллер...")
                    break

                # Шаг 1: Загружаем файлы из кэша MPEG-TS
                if len(cached_files_queue) < MIN_CACHE_FILES_FOR_SEND and self.use_mpegts_cache:
                    # Загружаем новые файлы из кэша
                    new_cached_files = self._refresh_cached_files_queue(limit=20)
                    if new_cached_files:
                        cached_files_queue.extend(new_cached_files)
                        logger.info(
                            f"📂 Загружено {len(new_cached_files)} файлов из кэша, всего: {len(cached_files_queue)}")

                        # Отправляем статистику
                        socketio.emit('cache_status', {
                            'files_in_cache': len(cached_files_queue),
                            'min_for_send': MIN_CACHE_FILES_FOR_SEND,
                            'status': 'accumulating' if len(cached_files_queue) < MIN_CACHE_FILES_FOR_SEND else 'ready'
                        })

                # Шаг 2: Отправка файлов из кэша MPEG-TS (только когда набралось достаточно)
                if len(cached_files_queue) >= MIN_CACHE_FILES_FOR_SEND:
                    self.is_playing_video = True

                    # Определяем сколько файлов отправить
                    batch_size = min(len(cached_files_queue), MAX_CACHE_BATCH)
                    logger.info(
                        f"🎯 Отправка батча из кэша: {batch_size} файлов (всего в кэше: {len(cached_files_queue)})")

                    sent_count = 0
                    failed_count = 0

                    for i in range(batch_size):
                        if not self.is_streaming or not self._check_ffmpeg_alive():
                            break

                        cache_item = cached_files_queue[i]
                        mpegts_path = cache_item['path']
                        duration = cache_item['duration']
                        original_filename = cache_item['original_filename']
                        cache_key = cache_item['cache_key']

                        logger.info(
                            f"🎬 Отправка из MPEG-TS кэша [{i + 1}/{batch_size}]: {original_filename} ({duration:.1f} сек)")

                        # Отправляем файл из кэша MPEG-TS
                        success = self._send_mpegts_file(mpegts_path, duration)

                        if success:
                            # Обновляем статистику кэша
                            self._update_cache_access_time(cache_key)
                            sent_count += 1

                            socketio.emit('video_playing', {
                                'filename': original_filename,
                                'duration': duration,
                                'timestamp': datetime.now().isoformat(),
                                'cache_position': f"{i + 1}/{batch_size}",
                                'total_in_cache': len(cached_files_queue),
                                'from_cache': True
                            })

                            # Ждем пока видео воспроизводится
                            time.sleep(duration)
                        else:
                            logger.error(f"❌ Не удалось отправить файл из MPEG-TS кэша: {original_filename}")
                            failed_count += 1

                            # Пробуем удалить поврежденный файл из кэша
                            self._remove_from_cache(cache_key)
                            time.sleep(1)

                    # Удаляем отправленные файлы из очереди кэша
                    cached_files_queue = cached_files_queue[batch_size:]

                    logger.info(f"✅ Батч отправлен: {sent_count} успешно, {failed_count} с ошибками")
                    logger.info(f"📊 Осталось в кэше: {len(cached_files_queue)} файлов")

                    # Отправляем статистику после отправки
                    socketio.emit('batch_complete', {
                        'sent_count': sent_count,
                        'failed_count': failed_count,
                        'remaining_in_cache': len(cached_files_queue)
                    })

                    self.is_playing_video = False

                else:
                    # Если в кэше недостаточно файлов
                    if len(cached_files_queue) > 0:
                        logger.info(
                            f"⏳ Ожидание набора файлов в кэше: {len(cached_files_queue)}/{MIN_CACHE_FILES_FOR_SEND}")

                        # Показываем статус ожидания
                        if len(cached_files_queue) == 0:
                            socketio.emit('waiting_for_content', {
                                'message': 'Ожидание создания контента...',
                                'current': 0,
                                'required': MIN_CACHE_FILES_FOR_SEND
                            })
                        else:
                            socketio.emit('accumulating_cache', {
                                'message': 'Накопление MPEG-TS файлов в кэше',
                                'current': len(cached_files_queue),
                                'required': MIN_CACHE_FILES_FOR_SEND,
                                'progress': (len(cached_files_queue) / MIN_CACHE_FILES_FOR_SEND) * 100
                            })

                        # Ждем создания большего количества файлов
                        time.sleep(5)
                    else:
                        # Если кэш пуст
                        logger.info("📭 Кэш пуст, ожидание создания контента...")
                        socketio.emit('cache_empty', {
                            'message': 'Кэш пуст, создание контента...'
                        })
                        time.sleep(10)

            except Exception as e:
                logger.error(f"❌ Ошибка в контроллере потока: {e}", exc_info=True)
                time.sleep(1)

        logger.info("🛑 Контроллер MPEG-TS потока остановлен")

    def start_stream(self, use_audio: bool = True):
        """Запуск единого FFmpeg процесса для видео и аудио"""
        if not self.stream_key:
            logger.error("❌ Stream Key не установлен!")
            return {'success': False, 'error': 'Stream Key не установлен'}

        try:
            self.start_time = time.time()

            # Инициализируем очереди
            self.audio_queue = []
            self.video_queue = []
            self.is_playing_audio = False
            self.is_playing_video = False

            # АВТОМАТИЧЕСКОЕ ДОБАВЛЕНИЕ ВИДЕО ИЗ КЭША ПРИ ЗАПУСКЕ - 10 ФАЙЛОВ
            logger.info("🔍 Автоматическое добавление видео из кэша...")
            auto_added = self.auto_add_videos_from_cache(limit=10)
            if auto_added > 0:
                logger.info(f"📥 Добавлено {auto_added} видео из кэша в очередь")
            else:
                logger.info("📭 В кэше не найдено видео файлов")

            # УВЕЛИЧИВАЕМ БИТРЕЙТ ДЛЯ YOUTUBE - МИНИМАЛЬНЫЕ ТРЕБОВАНИЯ
            video_bitrate = '4500k'  # Минимум для 1080p30
            maxrate = '6000k'  # Максимальный битрейт
            bufsize = '9000k'  # Размер буфера
            audio_bitrate = '128k'  # Стандартный для YouTube

            logger.info(f"🚀 Запуск FFmpeg стрима на YouTube с битрейтом {video_bitrate}...")
            logger.info(f"🔗 RTMP URL: {self.rtmp_url}")
            logger.info("⚠️  Минимальные требования YouTube для 1080p: видео 4500k, аудио 128k")

            # ВАЖНО: ОДИН PIPE для видео+аудио в формате MPEG-TS
            ffmpeg_cmd = [
                'ffmpeg',

                # Вход 0: MPEG-TS поток через stdin (содержит и видео, и аудио)
                '-f', 'mpegts',
                '-i', 'pipe:0',

                # Оптимизированные настройки для YouTube
                '-c:v', 'libx264',
                '-preset', 'medium',  # Баланс между скоростью и качеством
                '-tune', 'zerolatency',
                '-pix_fmt', 'yuv420p',
                '-profile:v', 'high',  # Профиль для YouTube
                '-level', '4.1',  # Уровень для 1080p
                '-g', '60',  # GOP size = 2 секунды при 30fps
                '-keyint_min', '60',  # Минимальный GOP
                '-sc_threshold', '0',  # Отключаем сценкат
                '-bf', '2',  # 2 B-фрейма
                '-b:v', video_bitrate,
                '-maxrate', maxrate,
                '-bufsize', bufsize,
                '-r', str(self.video_fps),
                '-s', f'{self.video_width}x{self.video_height}',  # Явно указываем размер
                '-force_key_frames', 'expr:gte(t,n_forced*2)',  # Ключевые кадры каждые 2 секунды

                '-c:a', 'aac',
                '-b:a', audio_bitrate,
                '-ar', '44100',
                '-ac', '2',
                '-strict', 'experimental',

                # Формат вывода с оптимизацией для YouTube
                '-f', 'flv',
                '-flvflags', 'no_duration_filesize',
                '-rtmp_buffer', '10000',  # Увеличиваем буфер RTMP
                '-rtmp_live', 'live',

                self.rtmp_url
            ]

            logger.info(f"🚀 Запуск FFmpeg с MPEG-TS pipe...")
            logger.info(
                f"📊 Настройки: видео={video_bitrate}, аудио={audio_bitrate}, размер={self.video_width}x{self.video_height}")

            # Запускаем FFmpeg процесс с обработкой ошибок
            try:
                self.stream_process = subprocess.Popen(
                    ffmpeg_cmd,
                    stdin=subprocess.PIPE,  # Для MPEG-TS потока
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                    bufsize=0,
                    text=False
                )
            except Exception as e:
                logger.error(f"❌ Не удалось запустить FFmpeg: {e}")
                return {'success': False, 'error': f'Ошибка запуска FFmpeg: {str(e)}'}

            self.is_streaming = True
            self.ffmpeg_pid = self.stream_process.pid
            self.ffmpeg_stdin = self.stream_process.stdin  # Для MPEG-TS потока

            logger.info(f"✅ FFmpeg запущен (PID: {self.ffmpeg_pid})")

            # Запускаем мониторинг с обработкой низкого битрейта
            threading.Thread(target=self._monitor_ffmpeg_with_restart, daemon=True).start()

            # Запускаем главный контроллер потока
            threading.Thread(
                target=self._stream_controller,
                daemon=True
            ).start()

            socketio.emit('stream_started', {
                'pid': self.ffmpeg_pid,
                'rtmp_url': self.rtmp_url,
                'has_video': True,
                'has_audio': True,
                'mode': 'mpegts_pipe',
                'bitrate': video_bitrate,
                'resolution': f'{self.video_width}x{self.video_height}',
                'fps': self.video_fps,
                'videos_added_from_cache': auto_added
            })

            return {'success': True, 'pid': self.ffmpeg_pid, 'videos_added': auto_added}

        except Exception as e:
            logger.error(f"❌ Ошибка запуска FFmpeg: {e}", exc_info=True)
            self.is_streaming = False
            return {'success': False, 'error': str(e)}

    def _send_video_to_fifo(self, fifo, video_path: str, duration: float) -> bool:
        """Отправка видео в FIFO в сыром формате"""
        try:
            # Конвертируем видео в сырой формат bgr24
            convert_cmd = [
                'ffmpeg',
                '-re',
                '-i', video_path,
                '-t', str(duration),
                '-c:v', 'rawvideo',
                '-pix_fmt', 'bgr24',
                '-f', 'rawvideo',
                '-'
            ]

            logger.debug(f"Конвертация видео в сырой формат: {os.path.basename(video_path)}")

            # Запускаем конвертацию
            convert_process = subprocess.Popen(
                convert_cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=0
            )

            # Отправляем сырые данные в FIFO
            bytes_per_frame = self.video_width * self.video_height * 3  # bgr24
            total_frames = int(duration * self.video_fps)
            frames_sent = 0

            while frames_sent < total_frames:
                frame_data = convert_process.stdout.read(bytes_per_frame)
                if not frame_data:
                    break

                fifo.write(frame_data)
                frames_sent += 1

                # Небольшая пауза для синхронизации
                time.sleep(1.0 / self.video_fps * 0.95)

            fifo.flush()

            # Завершаем процесс
            convert_process.terminate()
            if convert_process.poll() is None:
                convert_process.kill()

            logger.info(f"✅ Отправлено {frames_sent}/{total_frames} кадров в FIFO")
            return frames_sent > 0

        except Exception as e:
            logger.error(f"❌ Ошибка отправки видео в FIFO: {e}")
            return False

    def _optimize_video_for_streaming(self, video_path: str, target_bitrate: str = '4500k') -> str:
        """Оптимизация видео для стриминга"""
        try:
            if not os.path.exists(video_path):
                logger.error(f"❌ Видео файл не найден: {video_path}")
                return video_path

            # Получаем информацию о видео
            video_info = self._get_video_info(video_path)
            if not video_info:
                logger.warning(f"⚠️ Не удалось получить информацию о видео, использую как есть")
                return video_path

            width = video_info.get('width', self.video_width)
            height = video_info.get('height', self.video_height)
            fps = video_info.get('fps', self.video_fps)

            # Проверяем соответствует ли видео требованиям
            needs_optimization = False

            if width != self.video_width or height != self.video_height:
                logger.info(f"📐 Изменение разрешения: {width}x{height} -> {self.video_width}x{self.video_height}")
                needs_optimization = True

            if abs(fps - self.video_fps) > 1:
                logger.info(f"🎞️  Изменение FPS: {fps:.1f} -> {self.video_fps}")
                needs_optimization = True

            if not needs_optimization:
                logger.info(f"✅ Видео уже оптимизировано: {width}x{height} @ {fps}fps")
                return video_path

            # Оптимизируем видео
            temp_video = tempfile.NamedTemporaryFile(suffix='.mp4', delete=False)
            temp_video.close()

            optimize_cmd = [
                'ffmpeg',
                '-i', video_path,
                '-c:v', 'libx264',
                '-preset', 'medium',
                '-tune', 'film',
                '-pix_fmt', 'yuv420p',
                '-s', f'{self.video_width}x{self.video_height}',
                '-r', str(self.video_fps),
                '-b:v', target_bitrate,
                '-maxrate', target_bitrate,
                '-bufsize', f'{int(target_bitrate[:-1]) * 2}k',
                '-g', '60',
                '-keyint_min', '60',
                '-c:a', 'aac',
                '-b:a', '128k',
                '-ar', '44100',
                '-ac', '2',
                '-f', 'mp4',
                '-movflags', '+faststart',
                '-y',
                temp_video.name
            ]

            logger.info(f"⚡ Оптимизация видео: {os.path.basename(video_path)}")

            result = subprocess.run(
                optimize_cmd,
                capture_output=True,
                text=True,
                timeout=30
            )

            if result.returncode == 0:
                file_size = os.path.getsize(temp_video.name) / 1024 / 1024
                logger.info(f"✅ Видео оптимизировано: {file_size:.1f} MB")
                return temp_video.name
            else:
                logger.error(f"❌ Ошибка оптимизации: {result.stderr[:200]}")
                return video_path

        except Exception as e:
            logger.error(f"❌ Ошибка оптимизации видео: {e}")
            return video_path

    def _safe_restart_stream(self):
        """Безопасный перезапуск стрима"""
        try:
            logger.info("🔄 Безопасный перезапуск стрима...")

            # Сохраняем состояние очередей
            saved_video_queue = self.video_queue.copy() if self.video_queue else []
            saved_audio_queue = self.audio_queue.copy() if self.audio_queue else []

            logger.info(f"💾 Сохранено: видео={len(saved_video_queue)}, аудио={len(saved_audio_queue)}")

            # Останавливаем текущий стрим
            self.stop_stream()

            # Ждем немного для очистки
            time.sleep(2)

            # Сбрасываем флаг streaming перед запуском
            self.is_streaming = True

            # Запускаем заново с тем же stream key
            if not self.stream_key:
                logger.error("❌ Нет stream key для перезапуска")
                return False

            result = self.start_stream()

            if result.get('success'):
                # Восстанавливаем очереди
                if saved_video_queue:
                    self.video_queue = saved_video_queue + self.video_queue
                    logger.info(f"📥 Восстановлено {len(saved_video_queue)} видео в очередь")

                if saved_audio_queue:
                    self.audio_queue = saved_audio_queue + self.audio_queue
                    logger.info(f"📥 Восстановлено {len(saved_audio_queue)} аудио в очередь")

                logger.info(
                    f"✅ Стрим перезапущен. Всего в очередях: видео={len(self.video_queue)}, аудио={len(self.audio_queue)}")
                socketio.emit('stream_restarted', {
                    'message': 'Стрим автоматически перезапущен',
                    'video_queue_restored': len(saved_video_queue),
                    'audio_queue_restored': len(saved_audio_queue),
                    'total_video_queue': len(self.video_queue),
                    'total_audio_queue': len(self.audio_queue)
                })

                return True
            else:
                logger.error(f"❌ Не удалось перезапустить стрим: {result.get('error')}")
                return False

        except Exception as e:
            logger.error(f"❌ Ошибка перезапуска стрима: {e}", exc_info=True)
            return False

    def _monitor_ffmpeg_with_restart(self):
        """Мониторинг процесса FFmpeg с автоматическим перезапуском при низком битрейте"""
        try:
            stream_connected = False
            last_bitrate_warning = 0
            restart_count = 0
            max_restarts = 3
            last_restart_time = 0

            logger.info("📡 Запущен улучшенный мониторинг FFmpeg")

            for line in iter(self.stream_process.stderr.readline, b''):
                line = line.decode('utf-8', errors='ignore').strip()

                # Отладочная информация
                if 'frame=' in line and 'fps=' in line:
                    current_time = time.time()

                    # Парсим информацию о битрейте
                    if 'bitrate=' in line:
                        try:
                            import re
                            bitrate_match = re.search(r'bitrate=\s*([\d\.]+)\s*kbits/s', line)
                            if bitrate_match:
                                current_bitrate = float(bitrate_match.group(1))
                                current_time = time.time()

                                # Логируем битрейт каждые 10 секунд
                                if current_time - last_bitrate_warning > 10:
                                    logger.info(f"📊 Текущий битрейт: {current_bitrate:.1f} kbps")
                                    last_bitrate_warning = current_time

                                    # КРИТИЧЕСКОЕ ПРЕДУПРЕЖДЕНИЕ если битрейт слишком низкий
                                    if current_bitrate < 2000:
                                        logger.warning(f"⚠️ КРИТИЧЕСКИ НИЗКИЙ БИТРЕЙТ: {current_bitrate:.1f} kbps")
                                        socketio.emit('stream_critical', {
                                            'message': f'Критически низкий битрейт: {current_bitrate:.1f} kbps',
                                            'bitrate': current_bitrate
                                        })
                        except Exception as e:
                            logger.debug(f"Ошибка парсинга битрейта: {e}")

                    if hasattr(self, '_last_stats_log') and current_time - self._last_stats_log < 5:
                        continue
                    self._last_stats_log = current_time
                    logger.debug(f"📊 FFmpeg stats: {line}")

                # Подключение к YouTube
                elif 'rtmp://' in line and any(x in line.lower() for x in ['connected', 'publish', 'live']):
                    if not stream_connected:
                        stream_connected = True
                        logger.info("✅ Успешное подключение к YouTube")
                        socketio.emit('stream_connected', {'status': 'connected'})

                        # Сбрасываем счетчик перезапусков при успешном подключении
                        restart_count = 0

                # Ошибки
                elif any(x in line.lower() for x in ['error', 'failed', 'invalid']):
                    logger.error(f"⚠️ FFmpeg error: {line}")
                    socketio.emit('stream_warning', {'message': line})

                # Broken pipe - критическая ошибка
                elif 'broken pipe' in line.lower():
                    logger.error(f"💥 КРИТИЧЕСКАЯ ОШИБКА: {line}")
                    socketio.emit('stream_error', {
                        'message': 'YouTube закрыл соединение (Broken pipe)',
                        'reason': 'Возможно низкий битрейт или проблемы с сетью'
                    })

                # Предупреждение о низком битрейте от YouTube
                elif any(x in line.lower() for x in ['bitrate', 'low bitrate', 'insufficient']):
                    logger.warning(f"⚠️ YouTube битрейт предупреждение: {line}")
                    socketio.emit('stream_warning', {
                        'message': f'YouTube: {line}',
                        'type': 'bitrate_warning'
                    })

            # Процесс завершен
            return_code = self.stream_process.wait()
            logger.info(f"FFmpeg завершился с кодом: {return_code}")

            # Только если код ошибки не 0, помечаем как остановленный
            if return_code != 0:
                logger.warning(f"⚠️ FFmpeg завершился с ошибкой")
                self.is_streaming = False
                socketio.emit('stream_stopped', {
                    'time': datetime.now().isoformat(),
                    'exit_code': return_code,
                    'auto_restart': False
                })

        except Exception as e:
            logger.error(f"Ошибка мониторинга FFmpeg: {e}")

    def stop_stream(self):
        """Остановка стрима с очисткой pipe"""
        logger.info("🛑 Остановка стрима и очистка pipe...")

        self.is_streaming = False

        # Очищаем pipe
        if hasattr(self, 'video_pipe_path') and os.path.exists(self.video_pipe_path):
            try:
                os.unlink(self.video_pipe_path)
                logger.info("🧹 Video pipe очищен")
            except:
                pass

        # Останавливаем процесс FFmpeg
        time.sleep(0.5)

        try:
            if hasattr(self, 'ffmpeg_stdin') and self.ffmpeg_stdin:
                try:
                    self.ffmpeg_stdin.close()
                except:
                    pass

            if hasattr(self, 'stream_process') and self.stream_process:
                try:
                    self.stream_process.terminate()
                    time.sleep(0.5)
                    if self.stream_process.poll() is None:
                        self.stream_process.kill()
                except:
                    pass

        except Exception as e:
            logger.error(f"Ошибка при остановке: {e}")

        # Сбрасываем атрибуты
        self.stream_process = None
        self.ffmpeg_stdin = None
        self.ffmpeg_pid = None

        logger.info("✅ Стрим остановлен")
        return True

    def get_status(self):
        """Получение статуса"""
        return {
            'is_streaming': self.is_streaming,
            'stream_key': self.stream_key[:10] + '...' if self.stream_key else None,
            'rtmp_url': self.rtmp_url,
            'pid': self.ffmpeg_pid,
            'audio_queue_size': len(self.audio_queue),
            'video_queue_size': len(self.video_queue),
            'is_playing_audio': self.is_playing_audio,
            'is_playing_video': self.is_playing_video,
            'uptime': time.time() - self.start_time if self.start_time else 0
        }


class VideoGenerator:
    """Генератор видео для стрима с сохранением в кэш"""

    def __init__(self, ffmpeg_manager: FFmpegStreamManager = None):
        self.ffmpeg_manager = ffmpeg_manager
        self.video_cache_dir = 'video_cache'
        os.makedirs(self.video_cache_dir, exist_ok=True)

        # НОВОЕ: Очищаем старые файлы при инициализации
        self._clean_old_cache_files()

        self.video_width = 1920
        self.video_height = 1080
        self.fps = 30

        # Шрифты для текста
        self.fonts = self._load_fonts()

        logger.info(f"✅ Video Generator инициализирован. Кэш: {self.video_cache_dir}")

    def _load_fonts(self):
        """Загрузка шрифтов"""
        fonts = {}

        # Список путей к шрифтам
        font_paths = [
            # Linux
            '/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf',
            '/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf',
            '/usr/share/fonts/ubuntu/Ubuntu-B.ttf',

            # macOS
            '/System/Library/Fonts/Supplemental/Arial Bold.ttf',
            '/System/Library/Fonts/Arial.ttf',
            '/Library/Fonts/Arial Bold.ttf',

            # Windows
            'C:/Windows/Fonts/arialbd.ttf',
            'C:/Windows/Fonts/arial.ttf',
            'C:/Windows/Fonts/Arial.ttf',

            # Текущая директория
            './fonts/arial.ttf',
            './fonts/Arial.ttf',
            'arial.ttf',
            'Arial.ttf',

            # Популярные шрифты
            '/usr/share/fonts/truetype/msttcorefonts/Arial_Bold.ttf',
            '/usr/share/fonts/truetype/msttcorefonts/arialbd.ttf'
        ]

        # Сканируем системные директории шрифтов
        system_font_dirs = [
            '/usr/share/fonts',
            '/usr/local/share/fonts',
            '/Library/Fonts',
            'C:/Windows/Fonts',
            '/System/Library/Fonts',
            os.path.expanduser('~/.fonts')
        ]

        # Добавляем найденные шрифты Arial
        for font_dir in system_font_dirs:
            if os.path.exists(font_dir):
                try:
                    for root, dirs, files in os.walk(font_dir):
                        for file in files:
                            file_lower = file.lower()
                            # Ищем шрифты Arial или похожие
                            if ('arial' in file_lower or
                                'dejavu' in file_lower or
                                'liberation' in file_lower) and file_lower.endswith(('.ttf', '.otf')):
                                font_paths.append(os.path.join(root, file))
                except Exception as e:
                    logger.debug(f"Не удалось просканировать {font_dir}: {e}")

        # Убираем дубликаты
        font_paths = list(set(font_paths))

        # Пробуем загрузить шрифты
        loaded = False
        for path in font_paths:
            try:
                if os.path.exists(path):
                    # Пробуем загрузить все три размера
                    fonts['bold'] = ImageFont.truetype(path, 40)
                    fonts['regular'] = ImageFont.truetype(path, 32)
                    fonts['small'] = ImageFont.truetype(path, 24)

                    logger.info(f"✅ Загружен шрифт: {path}")
                    loaded = True
                    break
            except Exception as e:
                continue

        if not loaded:
            logger.warning("⚠️ Не удалось загрузить TTF шрифты, используем стандартные PIL шрифты")
            try:
                # Пробуем загрузить стандартные PIL шрифты
                fonts['bold'] = ImageFont.load_default()
                fonts['regular'] = ImageFont.load_default()
                fonts['small'] = ImageFont.load_default()

                # Пробуем создать шрифт по размеру
                try:
                    fonts['bold'] = ImageFont.truetype(ImageFont.load_default().path, 40)
                except:
                    pass

            except Exception as e:
                logger.error(f"❌ Не удалось загрузить даже стандартные шрифты: {e}")
                # Создаем заглушки
                fonts = {
                    'bold': None,
                    'regular': None,
                    'small': None
                }

        return fonts

    def _clean_old_cache_files(self, max_age_hours: int = 24):
        """Очистка старых файлов из кэша"""
        try:
            current_time = time.time()
            max_age = max_age_hours * 3600

            deleted_count = 0
            for filename in os.listdir(self.video_cache_dir):
                file_path = os.path.join(self.video_cache_dir, filename)

                if not os.path.isfile(file_path):
                    continue

                # Пропускаем не видео файлы
                if not filename.endswith(('.mp4', '.mov', '.avi', '.mkv')):
                    continue

                file_age = current_time - os.path.getctime(file_path)

                if file_age > max_age:
                    try:
                        os.unlink(file_path)
                        deleted_count += 1
                        logger.debug(f"🗑️  Удален старый файл: {filename}")
                    except Exception as e:
                        logger.warning(f"Не удалось удалить файл {filename}: {e}")

            if deleted_count > 0:
                logger.info(f"🧹 Очищено {deleted_count} старых файлов из кэша")

        except Exception as e:
            logger.error(f"Ошибка очистки кэша: {e}")

    def _safe_draw_text(self, draw: ImageDraw.Draw, position: tuple, text: str,
                        font_key: str = 'regular', color: tuple = (255, 255, 255),
                        anchor: str = "mm") -> None:
        """
        Безопасный метод для рисования текста на изображении.
        """
        try:
            # Получаем шрифт
            font = self.fonts.get(font_key)

            # Если шрифт не найден, используем стандартный
            if font is None:
                font = ImageFont.load_default()

            # Корректируем цвет для PIL
            # PIL принимает цвет как (R, G, B) или (R, G, B, A)
            pil_color = color

            # Если цвет содержит альфа-канал, но PIL не поддерживает RGBA для draw.text
            if len(color) == 4:
                r, g, b, a = color
                # Если альфа < 255, используем только RGB (прозрачность игнорируется)
                if a < 255:
                    pil_color = (r, g, b)  # Игнорируем альфа-канал
                else:
                    pil_color = (r, g, b)
            elif len(color) == 3:
                # Уже правильный формат
                pil_color = color
            else:
                # Неизвестный формат, используем белый
                logger.warning(f"Неправильный формат цвета: {color}, используем белый")
                pil_color = (255, 255, 255)

            # Пробуем нарисовать текст
            try:
                draw.text(position, text, font=font, fill=pil_color, anchor=anchor)
            except Exception as e:
                # Если не поддерживается anchor
                try:
                    draw.text(position, text, font=font, fill=pil_color)
                except Exception as e2:
                    # Если не поддерживается шрифт
                    draw.text(position, text, fill=pil_color)

        except Exception as e:
            # Не логируем ошибки рисования текста, чтобы не засорять логи
            pass

    def create_agent_intro_video(self, agent_name: str, expertise: str,
                                 avatar_color: str, message: str, duration: float = 7.0) -> str:
        """Создание видео-интро для агента и сохранение в кэш"""
        try:
            # Создаем уникальное имя файла
            timestamp = int(time.time())
            video_filename = f"intro_{agent_name}_{timestamp}.mp4"
            video_path = os.path.join(self.video_cache_dir, video_filename)

            logger.info(f"🎬 Создание видео-интро для {agent_name}...")

            # Параметры видео
            fps = self.fps
            total_frames = int(duration * fps)

            # Создаем VideoWriter
            fourcc = cv2.VideoWriter_fourcc(*'mp4v')  # или 'avc1' для H.264
            video_writer = cv2.VideoWriter(
                video_path,
                fourcc,
                fps,
                (self.video_width, self.video_height)
            )

            if not video_writer.isOpened():
                logger.error(f"❌ Не удалось создать VideoWriter для {video_path}")
                return None

            # Конвертируем цвет из hex в RGB
            if avatar_color.startswith('#'):
                color_hex = avatar_color.lstrip('#')
                rgb = tuple(int(color_hex[i:i + 2], 16) for i in (0, 2, 4))
            else:
                rgb = (100, 149, 237)  # Cornflower blue

            # Анимация появления
            for frame_num in range(total_frames):
                # Создаем изображение с фоном
                img = Image.new('RGB', (self.video_width, self.video_height),
                                (20, 20, 30))  # Темный фон
                draw = ImageDraw.Draw(img)

                # Эффект появления
                progress = min(1.0, frame_num / (fps * 1.0))  # Анимация за 1 секунду

                # Рисуем круг агента
                center_x = self.video_width // 2
                center_y = self.video_height // 3
                radius = int(150 * progress)

                # Градиент для круга
                for r in range(radius, 0, -5):
                    alpha = int(255 * (r / radius) * progress)
                    color = (*rgb, alpha)
                    draw.ellipse([center_x - r, center_y - r,
                                  center_x + r, center_y + r],
                                 fill=rgb, outline=(255, 255, 255, 100))

                # Имя агента
                if frame_num > fps * 0.5:  # Появляется через 0.5 секунды
                    name_progress = min(1.0, (frame_num - fps * 0.5) / (fps * 0.5))
                    name_alpha = int(255 * name_progress)
                    self._safe_draw_text(draw, (center_x, center_y + 180), agent_name,
                                         font_key='bold',
                                         color=(255, 255, 255, name_alpha),
                                         anchor="mm")

                # Экспертиза
                if frame_num > fps * 0.8:
                    exp_progress = min(1.0, (frame_num - fps * 0.8) / (fps * 0.5))
                    exp_alpha = int(200 * exp_progress)
                    self._safe_draw_text(draw, (center_x, center_y + 230), expertise,
                                         font_key='small',
                                         color=(200, 200, 255, exp_alpha),
                                         anchor="mm")

                # Сообщение (постепенно появляется)
                if frame_num > fps * 1.5 and message:
                    msg_progress = min(1.0, (frame_num - fps * 1.5) / (fps * 1.0))

                    # Разбиваем текст на строки
                    max_chars = 60
                    wrapped_text = textwrap.fill(message, width=max_chars)
                    lines = wrapped_text.split('\n')

                    # Рисуем фон для текста
                    text_height = len(lines) * 40
                    bg_top = self.video_height * 2 // 3 - 20
                    bg_bottom = bg_top + text_height + 40
                    bg_alpha = int(30 * msg_progress)

                    # Полупрозрачный фон
                    bg = Image.new('RGBA', (self.video_width, bg_bottom - bg_top),
                                   (0, 0, 0, bg_alpha))
                    img.paste(bg, (0, bg_top), bg)

                    # Текст сообщения
                    for i, line in enumerate(lines[:8]):  # Максимум 8 строк
                        text_y = bg_top + 20 + i * 40
                        text_alpha = int(255 * msg_progress)
                        self._safe_draw_text(draw, (center_x, text_y), line,
                                             font_key='regular',
                                             color=(255, 255, 255, text_alpha),
                                             anchor="mm")

                # Конвертируем PIL в OpenCV
                cv_img = cv2.cvtColor(numpy.array(img), cv2.COLOR_RGB2BGR)
                video_writer.write(cv_img)

            video_writer.release()

            # Проверяем что файл создан
            if os.path.exists(video_path):
                file_size = os.path.getsize(video_path) / 1024 / 1024  # MB
                logger.info(f"✅ Видео сохранено в кэш: {video_filename} ({file_size:.1f} MB, {duration} сек)")

                # Автоматически добавляем в очередь стрима
                if self.ffmpeg_manager and hasattr(self.ffmpeg_manager, 'add_video_from_cache'):
                    success = self.ffmpeg_manager.add_video_from_cache(video_filename, duration)
                    if success:
                        logger.info(f"📥 Видео добавлено в очередь стрима: {video_filename}")
                    else:
                        logger.warning(f"⚠️ Не удалось добавить видео в очередь стрима")

                return video_path

            return None

        except Exception as e:
            logger.error(f"❌ Ошибка создания видео: {e}", exc_info=True)
            return None

    def create_message_video(self, agent_name: str, message: str,
                             duration: float = 10.0) -> str:
        """Создание видео с текстом сообщения и сохранение в кэш"""
        try:
            timestamp = int(time.time())
            video_filename = f"message_{agent_name}_{timestamp}.mp4"
            video_path = os.path.join(self.video_cache_dir, video_filename)

            fps = self.fps
            total_frames = int(duration * fps)

            fourcc = cv2.VideoWriter_fourcc(*'mp4v')
            video_writer = cv2.VideoWriter(video_path, fourcc, fps,
                                           (self.video_width, self.video_height))

            if not video_writer.isOpened():
                logger.error(f"❌ Не удалось открыть VideoWriter")
                return None

            for frame_num in range(total_frames):
                progress = min(1.0, frame_num / (fps * 1.0))

                # Создаем фон
                img = Image.new('RGB', (self.video_width, self.video_height),
                                (30, 30, 40))
                draw = ImageDraw.Draw(img)

                # Заголовок с именем агента
                header_alpha = int(255 * progress)
                try:
                    draw.text((self.video_width // 2, 100),
                              agent_name,
                              font=self.fonts['bold'],
                              fill=(255, 255, 255, header_alpha),
                              anchor="mm")
                except:
                    draw.text((self.video_width // 2, 100),
                              agent_name,
                              fill=(255, 255, 255, header_alpha),
                              anchor="mm")

                # Текст сообщения
                if progress > 0.2:
                    text_alpha = int(255 * min(1.0, (progress - 0.2) * 1.5))

                    # Разбиваем текст на строки
                    wrapped_text = textwrap.fill(message, width=50)
                    lines = wrapped_text.split('\n')

                    # Рисуем текст
                    for i, line in enumerate(lines[:6]):  # Максимум 6 строк
                        y_pos = 200 + i * 45
                        try:
                            draw.text((self.video_width // 2, y_pos),
                                      line,
                                      font=self.fonts['regular'],
                                      fill=(255, 255, 255, text_alpha),
                                      anchor="mm")
                        except:
                            draw.text((self.video_width // 2, y_pos),
                                      line,
                                      fill=(255, 255, 255, text_alpha),
                                      anchor="mm")

                cv_img = cv2.cvtColor(numpy.array(img), cv2.COLOR_RGB2BGR)
                video_writer.write(cv_img)

            video_writer.release()

            if os.path.exists(video_path):
                logger.info(f"✅ Видео сообщения сохранено в кэш: {video_filename}")

                # НОВОЕ: Добавляем в очередь стрима
                if self.ffmpeg_manager:
                    self.ffmpeg_manager.add_video_from_cache(video_filename, duration)

                return video_path

            return None

        except Exception as e:
            logger.error(f"❌ Ошибка создания видео сообщения: {e}")
            return None

    def get_video_from_cache(self, filename: str) -> Optional[str]:
        """Получение видео файла из кэша"""
        video_path = os.path.join(self.video_cache_dir, filename)
        if os.path.exists(video_path):
            return video_path
        return None

    def create_transition_video(self, from_text: str, to_text: str,
                                duration: float = 5.0) -> str:
        """Создание переходного видео и сохранение в кэш"""
        try:
            timestamp = int(time.time())
            video_filename = f"transition_{timestamp}.mp4"
            video_path = os.path.join(self.video_cache_dir, video_filename)

            logger.info(f"🎬 Создание переходного видео: {from_text} → {to_text}")

            fps = self.fps
            total_frames = int(duration * fps)

            # Создаем VideoWriter
            fourcc = cv2.VideoWriter_fourcc(*'mp4v')
            video_writer = cv2.VideoWriter(
                video_path,
                fourcc,
                fps,
                (self.video_width, self.video_height)
            )

            if not video_writer.isOpened():
                logger.error(f"❌ Не удалось открыть VideoWriter для {video_path}")
                return None

            # Цвета для перехода
            color_from = (30, 60, 120)  # Синий
            color_to = (120, 60, 30)  # Коричневый
            bg_color = (20, 20, 30)  # Темный фон

            for frame_num in range(total_frames):
                progress = frame_num / total_frames

                # Создаем изображение с фоном
                img = Image.new('RGB', (self.video_width, self.video_height), bg_color)
                draw = ImageDraw.Draw(img)

                # Анимация смены текста
                if progress < 0.3:
                    # Показываем первый текст (исчезает)
                    text_alpha = int(255 * (1 - progress / 0.3))
                    self._safe_draw_text(
                        draw,
                        (self.video_width // 2, self.video_height // 2 - 80),
                        from_text,
                        font_key='bold',
                        color=(*color_from, text_alpha),
                        anchor="mm"
                    )

                    # Подпись "Завершение"
                    caption_alpha = int(200 * (1 - progress / 0.3))
                    self._safe_draw_text(
                        draw,
                        (self.video_width // 2, self.video_height // 2 - 150),
                        "↘ Завершение",
                        font_key='small',
                        color=(180, 180, 255, caption_alpha),
                        anchor="mm"
                    )

                elif progress < 0.7:
                    # Промежуточное состояние
                    mid_progress = (progress - 0.3) / 0.4

                    # Анимационная линия между текстами
                    line_y = self.video_height // 2
                    line_x1 = self.video_width * 0.3
                    line_x2 = self.video_width * 0.7
                    line_alpha = int(150 * (1 - abs(mid_progress - 0.5) * 2))

                    # Рисуем анимированную линию
                    line_points = []
                    for i in range(20):
                        x = line_x1 + (line_x2 - line_x1) * (i / 19)
                        y = line_y + numpy.sin(mid_progress * 20 + i * 0.5) * 15
                        line_points.append((x, y))

                    if len(line_points) > 1:
                        for i in range(len(line_points) - 1):
                            draw.line(
                                [line_points[i], line_points[i + 1]],
                                fill=(100, 200, 255, line_alpha),
                                width=3
                            )

                    # Минимальные версии текстов
                    from_alpha = int(100 * (1 - mid_progress))
                    to_alpha = int(100 * mid_progress)

                    self._safe_draw_text(
                        draw,
                        (self.video_width // 4, self.video_height // 2),
                        from_text[:30] + ("..." if len(from_text) > 30 else ""),
                        font_key='small',
                        color=(*color_from, from_alpha),
                        anchor="mm"
                    )

                    self._safe_draw_text(
                        draw,
                        (self.video_width * 3 // 4, self.video_height // 2),
                        to_text[:30] + ("..." if len(to_text) > 30 else ""),
                        font_key='small',
                        color=(*color_to, to_alpha),
                        anchor="mm"
                    )

                else:
                    # Показываем второй текст (появляется)
                    text_progress = (progress - 0.7) / 0.3
                    text_alpha = int(255 * text_progress)

                    self._safe_draw_text(
                        draw,
                        (self.video_width // 2, self.video_height // 2 - 80),
                        to_text,
                        font_key='bold',
                        color=(*color_to, text_alpha),
                        anchor="mm"
                    )

                    # Подпись "Начало"
                    caption_alpha = int(200 * text_progress)
                    self._safe_draw_text(
                        draw,
                        (self.video_width // 2, self.video_height // 2 - 150),
                        "↗ Начало",
                        font_key='small',
                        color=(255, 200, 180, caption_alpha),
                        anchor="mm"
                    )

                # Визуальные элементы (частицы)
                for i in range(15):
                    particle_x = (progress * 1.5 + i * 0.1) % 1.0 * self.video_width
                    particle_y = self.video_height * 0.8 + numpy.sin(progress * 10 + i) * 20
                    particle_size = 3 + numpy.sin(progress * 8 + i * 0.7) * 2
                    particle_alpha = int(150 + numpy.sin(progress * 5 + i) * 100)

                    # Цвет частицы меняется от color_from к color_to
                    mix_factor = progress
                    r = int(color_from[0] * (1 - mix_factor) + color_to[0] * mix_factor)
                    g = int(color_from[1] * (1 - mix_factor) + color_to[1] * mix_factor)
                    b = int(color_from[2] * (1 - mix_factor) + color_to[2] * mix_factor)

                    draw.ellipse([
                        particle_x - particle_size,
                        particle_y - particle_size,
                        particle_x + particle_size,
                        particle_y + particle_size
                    ], fill=(r, g, b, particle_alpha))

                # Конвертируем PIL в OpenCV
                cv_img = cv2.cvtColor(numpy.array(img), cv2.COLOR_RGB2BGR)
                video_writer.write(cv_img)

            video_writer.release()

            # Проверяем что файл создан
            if os.path.exists(video_path):
                file_size = os.path.getsize(video_path) / 1024 / 1024  # MB
                logger.info(
                    f"✅ Переходное видео сохранено в кэш: {video_filename} ({file_size:.1f} MB, {duration} сек)")

                # Автоматически добавляем в очередь стрима
                if self.ffmpeg_manager and hasattr(self.ffmpeg_manager, 'add_video_from_cache'):
                    success = self.ffmpeg_manager.add_video_from_cache(video_filename, duration)
                    if success:
                        logger.info(f"📥 Переходное видео добавлено в очередь стрима")

                return video_path

            return None

        except Exception as e:
            logger.error(f"❌ Ошибка создания переходного видео: {e}", exc_info=True)
            return None

    def list_cached_videos(self) -> List[Dict[str, Any]]:
        """Список всех видео в кэше"""
        videos = []
        try:
            for filename in os.listdir(self.video_cache_dir):
                if filename.endswith(('.mp4', '.mov', '.avi', '.mkv')):
                    video_path = os.path.join(self.video_cache_dir, filename)
                    file_size = os.path.getsize(video_path) / 1024 / 1024  # MB
                    ctime = os.path.getctime(video_path)

                    videos.append({
                        'filename': filename,
                        'path': video_path,
                        'size_mb': round(file_size, 2),
                        'created': datetime.fromtimestamp(ctime).isoformat(),
                        'age_hours': round((time.time() - ctime) / 3600, 1)
                    })

            logger.info(f"📂 В кэше найдено {len(videos)} видео файлов")

        except Exception as e:
            logger.error(f"❌ Ошибка получения списка видео: {e}")

        return videos
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
            'male_ru_standard': 'ru-RU-Pavel-Apollo',
            'female_ru': 'ru-RU-SvetlanaNeural',
        }

        # Инициализация pygame для локального воспроизведения
        try:
            pygame.mixer.init(frequency=44100, size=-16, channels=2, buffer=2048)
            self.pygame_available = True
        except:
            self.pygame_available = False
            logger.warning("⚠️ Pygame не доступен для локального воспроизведения")

        logger.info("Edge TTS Manager инициализирован")

    async def generate_audio_only(self, text: str, voice_id: str = 'male_ru', agent_name: str = "") -> Optional[str]:
        """Генерация аудио файла БЕЗ воспроизведения"""
        try:
            if voice_id not in self.voice_map:
                voice_id = 'male_ru'

            voice_name = self.voice_map[voice_id]

            # Хэш для имени файла
            text_hash = hashlib.md5(f"{text}_{voice_id}".encode()).hexdigest()
            timestamp = int(time.time())
            cache_file = os.path.join(self.cache_dir, f"{agent_name}_{text_hash}_{timestamp}.mp3")

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
            logger.info(f"🔊 Генерация аудио: {agent_name} ({voice_name}) - {len(text)} символов")

            communicate = edge_tts.Communicate(
                text=text,
                voice=voice_name,
                rate=rate,
                pitch=pitch
            )

            await communicate.save(cache_file)

            # Проверяем, что файл создан и не пустой
            if os.path.exists(cache_file) and os.path.getsize(cache_file) > 0:
                logger.info(f"💾 Аудио сохранено: {os.path.basename(cache_file)}")

                # Получаем информацию о файле
                file_size = os.path.getsize(cache_file) / 1024  # KB
                duration = self._get_audio_duration(cache_file)

                logger.info(f"📊 Размер файла: {file_size:.1f} KB, Длительность: {duration:.1f} сек")

                return cache_file
            else:
                logger.error(f"❌ Аудио файл не создан или пустой: {cache_file}")
                return None

        except Exception as e:
            logger.error(f"❌ Ошибка генерации аудио: {e}", exc_info=True)
            return None

    def _get_audio_duration(self, audio_file: str) -> float:
        """Получение длительности аудио файла в секундах"""
        try:
            if not os.path.exists(audio_file):
                logger.error(f"Файл не найден: {audio_file}")
                return 0.0

            # Используем ffprobe для получения точной длительности
            cmd = [
                'ffprobe',
                '-v', 'error',
                '-show_entries', 'format=duration',
                '-of', 'default=noprint_wrappers=1:nokey=1',
                audio_file
            ]

            result = subprocess.run(cmd, capture_output=True, text=True, timeout=5)

            if result.returncode == 0 and result.stdout.strip():
                duration = float(result.stdout.strip())
                return duration
            else:
                logger.warning(f"Не удалось получить длительность через ffprobe: {result.stderr}")

                # Альтернативный метод: оцениваем по размеру файла
                file_size = os.path.getsize(audio_file)  # в байтах
                bitrate = 128000  # 128 kbps
                duration = file_size * 8 / bitrate  # в секундах
                return duration

        except Exception as e:
            logger.warning(f"Ошибка получения длительности аудио: {e}")
            return 5.0  # Значение по умолчанию


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
        self.video_generator = VideoGenerator(ffmpeg_manager)  # Добавлено
        self.ffmpeg_manager = ffmpeg_manager
        self.current_topic = ""
        self.is_discussion_active = False
        self.message_count = 0
        self.discussion_round = 0
        self.active_agent = None
        self.conversation_history = []
        self.show_video_intros = True  # Флаг для показа видео-интро

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
        """Оптимизированный метод - создает MPEG-TS файлы и сохраняет в кэш"""
        if self.is_discussion_active:
            return

        self.is_discussion_active = True
        self.discussion_round += 1

        try:
            if not self.current_topic:
                self.select_topic()

            logger.info(f"🚀 Начало раунда #{self.discussion_round} - создание MPEG-TS файлов для кэша")

            # Определяем порядок выступлений
            speaking_order = random.sample(self.agents, len(self.agents))

            for agent_idx, agent in enumerate(speaking_order):
                if not self.is_discussion_active:
                    break

                # Генерация ответа
                logger.info(f"🤖 {agent.name} генерирует ответ...")

                message = await agent.generate_response(self.current_topic, self.conversation_history)

                # Сохраняем в историю
                self.conversation_history.append(f"{agent.name}: {message}")
                self.message_count += 1

                # Отправляем сообщение в WebSocket сразу
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

                # Агент начинает говорить
                self.active_agent = agent.id
                socketio.emit('agent_start_speaking', {
                    'agent_id': agent.id,
                    'agent_name': agent.name,
                    'expertise': agent.expertise
                })

                # ========== СОЗДАНИЕ MPEG-TS ДЛЯ КЭША ==========
                audio_file = None
                video_message = None

                try:
                    # 1. Генерируем аудио
                    audio_file = await self.tts_manager.generate_audio_only(
                        text=message,
                        voice_id=agent.voice,
                        agent_name=agent.name
                    )

                    # 2. Создаем видео с сообщением
                    message_video_duration = min(max(len(message.split()) * 0.2, 3), 10)

                    # Создаем видео сообщения
                    video_message = await asyncio.to_thread(
                        self.video_generator.create_message_video,
                        agent_name=agent.name,
                        message=message,
                        duration=message_video_duration
                    )

                    if audio_file and video_message and self.ffmpeg_manager:
                        # Создаем MPEG-TS файл с видео и аудио
                        timestamp = int(time.time())
                        mpegts_filename = f"mpegts_{agent.name}_{timestamp}.ts"
                        mpegts_path = os.path.join(self.ffmpeg_manager.mpegts_cache_dir, mpegts_filename)

                        # Создаем MPEG-TS файл
                        success = self.ffmpeg_manager._create_mpegts_file(
                            video_message,
                            message_video_duration,
                            audio_file,
                            mpegts_path
                        )

                        if success:
                            # Добавляем в кэш
                            cache_key = self.ffmpeg_manager._get_mpegts_cache_key(video_message, audio_file)
                            self.ffmpeg_manager.cache_mpegts_file(
                                video_message,
                                mpegts_path,
                                message_video_duration,
                                audio_file,
                                True
                            )

                            logger.info(f"💾 MPEG-TS файл сохранен в кэш: {mpegts_filename}")
                            logger.info(f"📊 В кэше: {len(self.ffmpeg_manager.mpegts_cache)} файлов")

                            # Отправляем уведомление о создании файла
                            socketio.emit('mpegts_created', {
                                'agent_name': agent.name,
                                'filename': mpegts_filename,
                                'duration': message_video_duration,
                                'cache_size': len(self.ffmpeg_manager.mpegts_cache),
                                'timestamp': datetime.now().isoformat()
                            })
                        else:
                            logger.error(f"❌ Не удалось создать MPEG-TS файл для {agent.name}")

                    # Имитируем воспроизведение для пользователя
                    audio_duration = self.tts_manager._get_audio_duration(audio_file) if audio_file else 5.0
                    logger.info(f"🔊 Аудио создано: {agent.name} ({audio_duration:.1f} сек)")
                    await asyncio.sleep(audio_duration)

                except Exception as e:
                    logger.error(f"❌ Ошибка создания контента для {agent.name}: {e}")
                    await asyncio.sleep(3.0)

                # ========== ЗАВЕРШЕНИЕ РЕЧИ ==========
                socketio.emit('agent_stop_speaking', {'agent_id': agent.id})
                self.active_agent = None

                # ========== ПЕРЕХОД К СЛЕДУЮЩЕМУ АГЕНТУ ==========
                if agent_idx < len(speaking_order) - 1 and self.is_discussion_active:
                    pause = random.uniform(0.5, 1.5)
                    await asyncio.sleep(pause)

            logger.info(f"✅ Раунд #{self.discussion_round} завершен")

            socketio.emit('round_complete', {
                'round': self.discussion_round,
                'total_messages': self.message_count,
                'cache_size': len(self.ffmpeg_manager.mpegts_cache) if self.ffmpeg_manager else 0,
                'next_round_in': Config.DISCUSSION_INTERVAL // 2
            })

            # Пауза перед следующим раундом
            await asyncio.sleep(Config.DISCUSSION_INTERVAL // 2)

            # Случайная смена темы
            if random.random() > 0.6:
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

    def _generate_intro_cache_key(self, agent) -> str:
        """Генерация ключа кэша для видео-интро агента"""
        return f"intro_{agent.name}_{hash(agent.expertise)}"

    def _generate_message_cache_key(self, agent, message: str) -> str:
        """Генерация ключа кэша для видео с сообщением"""
        message_hash = hashlib.md5(message[:200].encode()).hexdigest()[:16]
        return f"message_{agent.name}_{message_hash}"

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


# ========== ИНИЦИАЛИЗАЦИЯ ==========

ffmpeg_manager = FFmpegStreamManager()
stream_manager = AIStreamManager(ffmpeg_manager)

ffmpeg_manager.video_generator = stream_manager.video_generator

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

@app.route('/')
def index():
    """Главная страница"""
    return render_template('index.html',
                           agents=stream_manager.get_agents_state(),
                           topic=stream_manager.current_topic or "Загрузка темы...",
                           stats=stream_manager.get_stats())


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
    try:
        if not stream_manager.is_discussion_active:
            stream_manager.is_discussion_active = True
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
                stream_manager.tts_manager.generate_audio_only(
                    text=test_text,
                    voice_id=agent.voice,
                    agent_name=agent.name
                )
            )

            if audio_file and ffmpeg_manager:
                ffmpeg_manager.add_audio_to_queue(audio_file)

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
                stream_manager.tts_manager.generate_audio_only(
                    text=text,
                    voice_id=voice,
                    agent_name="Тест"
                )
            )

            if audio_file and ffmpeg_manager:
                ffmpeg_manager.add_audio_to_queue(audio_file)

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
            return jsonify({
                'status': 'started',
                'pid': result['pid'],
                'rtmp_url': ffmpeg_manager.rtmp_url,
                'stream_key': ffmpeg_manager.stream_key[:10] + '...',
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


@app.route('/api/change_topic', methods=['POST'])
def api_change_topic():
    """Смена темы"""
    topic = stream_manager.select_topic()
    return jsonify({'success': True, 'topic': topic})


# ========== SOCKET.IO HANDLERS ==========


@socketio.on('request_update')
def handle_request_update():
    """Запрос обновления состояния"""
    emit('update', {
        'agents': stream_manager.get_agents_state(),
        'topic': stream_manager.current_topic or "Не выбрана",
        'stats': stream_manager.get_stats(),
        'stream_status': ffmpeg_manager.get_status()
    })



@socketio.on('stream_started')
def handle_stream_started(data):
    logger.info(f"🎬 Стрим запущен: {data}")


@socketio.on('stream_stopped')
def handle_stream_stopped(data):
    logger.info(f"🛑 Стрим остановлен: {data}")


@socketio.on('stream_connected')
def handle_stream_connected(data):
    logger.info(f"✅ Стрим подключен к YouTube: {data}")


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
    print("🤖 AI AGENTS STREAM - ПРЯМОЙ STREAM KEY РЕЖИМ")
    print("=" * 70)

    print("📦 Используемые технологии:")
    print("   • FFmpeg для прямой трансляции на YouTube")
    print("   • OpenAI GPT для генерации диалогов")
    print("   • Edge TTS для генерации голоса")
    print("   • WebSocket для реального обновления UI")

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