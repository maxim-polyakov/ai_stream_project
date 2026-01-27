#!/usr/bin/env python3
"""
🤖 AI Stream с FFmpeg стримингом на YouTube
Версия с простым кэшем аудио и видео файлов
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


# ========== ПРОСТОЙ МЕНЕДЖЕР КЭША ==========

class SimpleCacheManager:
    """Простой менеджер кэша для аудио и видео файлов"""

    def __init__(self, audio_cache_dir='audio_cache', video_cache_dir='video_cache'):
        self.audio_cache_dir = audio_cache_dir
        self.video_cache_dir = video_cache_dir

        # Создаем директории кэша
        os.makedirs(audio_cache_dir, exist_ok=True)
        os.makedirs(video_cache_dir, exist_ok=True)

        # Статистика
        self.cache_hits = 0
        self.cache_misses = 0

        logger.info(f"📁 Кэш аудио: {audio_cache_dir}")
        logger.info(f"📁 Кэш видео: {video_cache_dir}")

    def _safe_filename(self, text: str, max_length: int = 50) -> str:
        """Создание безопасного имени файла"""
        # Заменяем небезопасные символы
        safe_text = "".join(c if c.isalnum() or c in (' ', '-', '_') else '_' for c in text)
        # Убираем лишние пробелы и подчеркивания
        safe_text = '_'.join(safe_text.split())
        # Обрезаем до максимальной длины
        return safe_text[:max_length]

    def get_audio_file(self, text: str, voice_id: str = 'male_ru', agent_name: str = "") -> Optional[str]:
        """Получить аудио файл из кэша или создать новый"""
        # Создаем хэш из текста и голоса
        text_hash = hashlib.md5(f"{text}_{voice_id}".encode('utf-8')).hexdigest()

        # Создаем безопасное имя файла
        safe_agent_name = self._safe_filename(agent_name) if agent_name else "agent"
        cache_filename = f"{safe_agent_name}_{text_hash}.mp3"
        cache_path = os.path.join(self.audio_cache_dir, cache_filename)

        # Проверяем кэш
        if os.path.exists(cache_path) and os.path.getsize(cache_path) > 0:
            self.cache_hits += 1
            logger.debug(f"♻️ Найден в кэше: {cache_filename}")
            return cache_path
        else:
            self.cache_misses += 1
            logger.debug(f"❌ Не найден в кэше: {cache_filename}")
            return None

    def save_audio_file(self, text: str, audio_file_path: str, voice_id: str = 'male_ru', agent_name: str = "") -> str:
        """Сохранить аудио файл в кэш"""
        # Создаем хэш из текста и голоса
        text_hash = hashlib.md5(f"{text}_{voice_id}".encode('utf-8')).hexdigest()

        # Создаем безопасное имя файла
        safe_agent_name = self._safe_filename(agent_name) if agent_name else "agent"
        cache_filename = f"{safe_agent_name}_{text_hash}.mp3"
        cache_path = os.path.join(self.audio_cache_dir, cache_filename)

        try:
            # Проверяем, существует ли исходный файл
            if not os.path.exists(audio_file_path):
                logger.error(f"❌ Исходный аудио файл не найден: {audio_file_path}")
                return audio_file_path

            # Копируем файл в кэш
            shutil.copy2(audio_file_path, cache_path)

            # Проверяем, что файл скопирован успешно
            if os.path.exists(cache_path) and os.path.getsize(cache_path) > 0:
                logger.info(f"💾 Аудио сохранено в кэш: {cache_filename}")
                return cache_path
            else:
                logger.error(f"❌ Не удалось сохранить аудио в кэш: {cache_filename}")
                return audio_file_path

        except Exception as e:
            logger.error(f"❌ Ошибка сохранения аудио в кэш: {e}")
            return audio_file_path

    def get_video_file(self, audio_file_path: str, agent_name: str = "", text_hash: str = None) -> Optional[str]:
        """Получить видео файл из кэша или создать новый"""
        # Используем переданный хэш текста или создаем из пути аудио файла
        if text_hash:
            video_hash = text_hash  # Используем хэш текста для видео
        else:
            # Если нет хэша текста, используем хэш пути аудио файла
            video_hash = hashlib.md5(audio_file_path.encode('utf-8')).hexdigest()

        # Создаем безопасное имя файла
        safe_agent_name = self._safe_filename(agent_name) if agent_name else "agent"
        cache_filename = f"{safe_agent_name}_{video_hash}.mp4"
        cache_path = os.path.join(self.video_cache_dir, cache_filename)

        # Проверяем кэш
        if os.path.exists(cache_path) and os.path.getsize(cache_path) > 0:
            self.cache_hits += 1
            logger.debug(f"♻️ Видео найден в кэше: {cache_filename}")
            return cache_path
        else:
            self.cache_misses += 1
            logger.debug(f"❌ Видео не найден в кэше: {cache_filename}")
            return None

    def save_video_file(self, audio_file_path: str, video_file_path: str, agent_name: str = "",
                        text_hash: str = None) -> str:
        """Сохранить видео файл в кэш"""
        # Используем переданный хэш текста или создаем из пути аудио файла
        if text_hash:
            video_hash = text_hash  # Используем хэш текста для видео
        else:
            # Если нет хэша текста, используем хэш пути аудио файла
            video_hash = hashlib.md5(audio_file_path.encode('utf-8')).hexdigest()

        # Создаем безопасное имя файла
        safe_agent_name = self._safe_filename(agent_name) if agent_name else "agent"
        cache_filename = f"{safe_agent_name}_{video_hash}.mp4"
        cache_path = os.path.join(self.video_cache_dir, cache_filename)

        try:
            # Проверяем, существует ли исходный файл
            if not os.path.exists(video_file_path):
                logger.error(f"❌ Исходный видео файл не найден: {video_file_path}")
                return video_file_path

            # Копируем файл в кэш
            shutil.copy2(video_file_path, cache_path)

            # Проверяем, что файл скопирован успешно
            if os.path.exists(cache_path) and os.path.getsize(cache_path) > 0:
                logger.info(f"💾 Видео сохранено в кэш: {cache_filename}")
                return cache_path
            else:
                logger.error(f"❌ Не удалось сохранить видео в кэш: {cache_filename}")
                return video_file_path

        except Exception as e:
            logger.error(f"❌ Ошибка сохранения видео в кэш: {e}")
            return video_file_path

    def clear_cache(self, days_old: int = 7):
        """Очистить старые файлы из кэша"""
        now = time.time()
        deleted_files = 0

        # Очищаем аудио кэш
        for filename in os.listdir(self.audio_cache_dir):
            filepath = os.path.join(self.audio_cache_dir, filename)
            if os.path.isfile(filepath):
                try:
                    file_age = now - os.path.getmtime(filepath)
                    if file_age > days_old * 24 * 3600:
                        os.remove(filepath)
                        deleted_files += 1
                        logger.debug(f"🗑️ Удален старый файл аудио: {filename}")
                except Exception as e:
                    logger.warning(f"Не удалось удалить {filename}: {e}")

        # Очищаем видео кэш
        for filename in os.listdir(self.video_cache_dir):
            filepath = os.path.join(self.video_cache_dir, filename)
            if os.path.isfile(filepath):
                try:
                    file_age = now - os.path.getmtime(filepath)
                    if file_age > days_old * 24 * 3600:
                        os.remove(filepath)
                        deleted_files += 1
                        logger.debug(f"🗑️ Удален старый файл видео: {filename}")
                except Exception as e:
                    logger.warning(f"Не удалось удалить {filename}: {e}")

        logger.info(f"🗑️ Очищен кэш: удалено {deleted_files} старых файлов")

    def get_cache_info(self) -> Dict[str, Any]:
        """Получить информацию о кэше"""
        try:
            audio_files = []
            video_files = []

            # Получаем файлы аудио кэша
            if os.path.exists(self.audio_cache_dir):
                audio_files = [f for f in os.listdir(self.audio_cache_dir)
                               if os.path.isfile(os.path.join(self.audio_cache_dir, f))]

            # Получаем файлы видео кэша
            if os.path.exists(self.video_cache_dir):
                video_files = [f for f in os.listdir(self.video_cache_dir)
                               if os.path.isfile(os.path.join(self.video_cache_dir, f))]

            # Рассчитываем размер кэша
            audio_size = 0
            for f in audio_files:
                try:
                    audio_size += os.path.getsize(os.path.join(self.audio_cache_dir, f))
                except:
                    pass

            video_size = 0
            for f in video_files:
                try:
                    video_size += os.path.getsize(os.path.join(self.video_cache_dir, f))
                except:
                    pass

            total_size = audio_size + video_size

            total_requests = self.cache_hits + self.cache_misses
            hit_ratio = self.cache_hits / total_requests if total_requests > 0 else 0

            return {
                'audio_files': len(audio_files),
                'video_files': len(video_files),
                'audio_cache_size_mb': audio_size / (1024 * 1024),
                'video_cache_size_mb': video_size / (1024 * 1024),
                'total_cache_size_mb': total_size / (1024 * 1024),
                'cache_hits': self.cache_hits,
                'cache_misses': self.cache_misses,
                'hit_ratio': round(hit_ratio * 100, 2)  # в процентах
            }

        except Exception as e:
            logger.error(f"❌ Ошибка получения информации о кэше: {e}")
            return {
                'audio_files': 0,
                'video_files': 0,
                'audio_cache_size_mb': 0,
                'video_cache_size_mb': 0,
                'total_cache_size_mb': 0,
                'cache_hits': self.cache_hits,
                'cache_misses': self.cache_misses,
                'hit_ratio': 0
            }


# ========== FFMPEG STREAM MANAGER с КЭШЕМ ==========

class FFmpegPipeStreamManager:
    """Управление FFmpeg стримом на YouTube с кэшем"""

    def __init__(self, cache_manager: SimpleCacheManager):
        self.stream_process = None
        self.is_streaming = False
        self.stream_key = None
        self.rtmp_url = None
        self.ffmpeg_pid = None
        self.last_error = None
        self.stream_start_time = None
        self.cache_manager = cache_manager

        # Создаем папки для временных файлов
        os.makedirs('temp_videos', exist_ok=True)

        logger.info("FFmpeg Stream Manager с кэшем инициализирован")

    def set_stream_key(self, stream_key: str):
        """Установка ключа стрима"""
        self.stream_key = stream_key
        self.rtmp_url = f"rtmp://a.rtmp.youtube.com/live2/{stream_key}"
        logger.info(f"🔑 Stream Key установлен: {stream_key[:10]}...")
        return True

    def start_stream(self) -> Dict[str, Any]:
        """Запуск FFmpeg стрима"""
        if not self.stream_key:
            logger.error("❌ Stream Key не установлен!")
            return {'success': False, 'error': 'Stream Key не установлен'}

        try:
            self.stream_start_time = time.time()

            # Команда FFmpeg
            ffmpeg_cmd = [
                'ffmpeg',
                '-re',
                '-f', 'lavfi',
                '-i',
                "color=c=black:s=1920x1080:r=30,drawtext=text='AI Live Stream':fontcolor=white:fontsize=48:x=(w-text_w)/2:y=(h-text_h)/2:box=1:boxcolor=black@0.5",
                '-f', 'lavfi',
                '-i', 'anullsrc=r=44100:cl=stereo',
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

            logger.info(f"🚀 Запуск FFmpeg стрима")
            logger.info(f"📍 RTMP URL: {self.rtmp_url}")

            # Запускаем FFmpeg
            self.stream_process = subprocess.Popen(
                ffmpeg_cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                bufsize=1,
                universal_newlines=False
            )

            self.is_streaming = True
            self.ffmpeg_pid = self.stream_process.pid

            # Запускаем мониторинг
            self._start_monitor_thread()

            logger.info(f"🎬 FFmpeg стрим запущен (PID: {self.ffmpeg_pid})")

            time.sleep(2)

            return {
                'success': True,
                'pid': self.ffmpeg_pid,
                'stream_key': self.stream_key,
                'rtmp_url': self.rtmp_url,
                'message': 'FFmpeg стрим запущен.'
            }

        except Exception as e:
            logger.error(f"❌ Ошибка запуска FFmpeg: {e}", exc_info=True)
            return {'success': False, 'error': str(e)}

    def _create_video_with_audio(self, audio_file: str, agent_name: str = "") -> Optional[str]:
        """Создание видео файла с аудио и текстом (с кэшем)"""
        try:
            # Сначала проверяем кэш видео
            cached_video = self.cache_manager.get_video_file(audio_file, agent_name)
            if cached_video:
                logger.info(f"🎬 Используем видео из кэша: {os.path.basename(cached_video)}")
                return cached_video

            # Если нет в кэше, создаем новое видео
            temp_dir = 'temp_videos'
            os.makedirs(temp_dir, exist_ok=True)
            temp_video = os.path.join(temp_dir, f'video_audio_{int(time.time())}.mp4')

            # Команда для создания видео
            cmd = [
                'ffmpeg',
                '-f', 'lavfi',
                '-i',
                f"color=c=black:s=1920x1080:r=30,drawtext=text='{agent_name} Speaking':fontcolor=white:fontsize=60:x=(w-text_w)/2:y=(h-text_h)/2",
                '-i', audio_file,
                '-c:v', 'libx264',
                '-preset', 'veryfast',
                '-tune', 'zerolatency',
                '-pix_fmt', 'yuv420p',
                '-c:a', 'aac',
                '-b:a', '128k',
                '-shortest',
                '-y',
                temp_video
            ]

            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=30
            )

            if result.returncode == 0 and os.path.exists(temp_video):
                # Сохраняем в кэш
                cached_path = self.cache_manager.save_video_file(audio_file, agent_name, temp_video)
                logger.info(f"✅ Видео создано и сохранено в кэш: {os.path.basename(cached_path)}")

                # Удаляем временный файл (он теперь в кэше)
                try:
                    os.remove(temp_video)
                except:
                    pass

                return cached_path
            else:
                logger.error(f"❌ Ошибка создания видео: {result.stderr[:200]}")
                return None

        except Exception as e:
            logger.error(f"❌ Исключение при создании видео: {e}")
            return None

    def play_audio(self, audio_file: str, agent_name: str = "") -> bool:
        """Воспроизведение аудио файла в стриме с использованием кэша видео"""
        if not os.path.exists(audio_file):
            logger.error(f"❌ Аудио файл не найден: {audio_file}")
            return False

        if not self.is_streaming:
            logger.error("❌ Стрим не запущен")
            return False

        try:
            # Получаем длительность аудио
            duration = self._get_audio_duration(audio_file)
            logger.info(f"🎵 Воспроизведение аудио: {os.path.basename(audio_file)} ({duration:.1f} сек)")

            # Получаем или создаем видео (с кэшем)
            video_file = self._create_video_with_audio(audio_file, agent_name)
            if not video_file:
                logger.error("❌ Не удалось получить/создать видео")
                return False

            # Команда для отправки видео в стрим
            cmd = [
                'ffmpeg',
                '-re',
                '-i', video_file,
                '-c:v', 'libx264',
                '-preset', 'veryfast',
                '-tune', 'zerolatency',
                '-pix_fmt', 'yuv420p',
                '-c:a', 'aac',
                '-b:a', '128k',
                '-f', 'flv',
                self.rtmp_url
            ]

            logger.info(f"📤 Отправка видео в стрим")

            # Запускаем процесс в отдельном потоке
            def send_video():
                try:
                    process = subprocess.Popen(
                        cmd,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL
                    )

                    # Ждем завершения
                    time.sleep(duration + 2)

                    # Останавливаем процесс
                    if process.poll() is None:
                        process.terminate()
                        time.sleep(0.5)
                        if process.poll() is None:
                            process.kill()

                    logger.info(f"✅ Видео отправлено в стрим")

                except Exception as e:
                    logger.error(f"❌ Ошибка отправки видео: {e}")

            # Запускаем в отдельном потоке
            video_thread = threading.Thread(target=send_video, daemon=True)
            video_thread.start()

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
            ], capture_output=True, text=True, timeout=5)

            duration_str = result.stdout.strip()
            if duration_str:
                return float(duration_str)
            else:
                return 5.0
        except:
            return 5.0

    def _start_monitor_thread(self):
        """Мониторинг процесса FFmpeg"""

        def monitor():
            logger.info(f"👀 Начало мониторинга FFmpeg процесса (PID: {self.ffmpeg_pid})")

            while self.is_streaming and self.stream_process:
                try:
                    line_bytes = self.stream_process.stderr.readline()
                    if line_bytes:
                        line = line_bytes.decode('utf-8', errors='ignore').strip()
                        if line:
                            if any(keyword in line.lower() for keyword in
                                   ['error', 'fail', 'invalid', 'unable', 'cannot']):
                                logger.error(f"FFmpeg ERROR: {line}")
                                self.last_error = line
                            elif 'rtmp://' in line and 'connected' in line.lower():
                                logger.info(f"✅ Подключение к YouTube: {line}")
                except:
                    pass

                if self.stream_process.poll() is not None:
                    return_code = self.stream_process.returncode
                    logger.warning(f"⚠️ FFmpeg процесс завершился с кодом: {return_code}")
                    self.is_streaming = False
                    break

                time.sleep(0.1)

            logger.info("👀 Мониторинг FFmpeg завершен")

        self.monitor_thread = threading.Thread(target=monitor, daemon=True)
        self.monitor_thread.start()

    def stop_stream(self):
        """Остановка стрима"""
        try:
            self.is_streaming = False

            if self.stream_process:
                logger.info("🛑 Остановка FFmpeg стрима...")
                self.stream_process.terminate()

                for _ in range(10):
                    if self.stream_process.poll() is not None:
                        break
                    time.sleep(0.5)

                if self.stream_process.poll() is None:
                    self.stream_process.kill()
                    self.stream_process.wait()

                logger.info("✅ FFmpeg стрим остановлен")

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
            'last_error': self.last_error,
            'uptime': time.time() - self.stream_start_time if self.stream_start_time else 0
        }


# ========== EDGE TTS MANAGER с КЭШЕМ ==========

class EdgeTTSManager:
    """Менеджер TTS с кэшем аудио"""

    def __init__(self, ffmpeg_manager: FFmpegPipeStreamManager = None, cache_manager: SimpleCacheManager = None):
        self.ffmpeg_manager = ffmpeg_manager
        self.cache_manager = cache_manager or SimpleCacheManager()

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

        logger.info("Edge TTS Manager с кэшем инициализирован")

    async def text_to_speech_and_stream(self, text: str, voice_id: str = 'male_ru', agent_name: str = "") -> Optional[
        str]:
        """Генерация аудио (с кэшем) и отправка в стрим"""
        try:
            if voice_id not in self.voice_map:
                voice_id = 'male_ru'

            # Сначала проверяем кэш
            cached_audio = self.cache_manager.get_audio_file(text, voice_id, agent_name)

            if cached_audio:
                logger.info(f"♻️ Используем аудио из кэша: {os.path.basename(cached_audio)}")
                audio_file = cached_audio
            else:
                # Генерируем новое аудио
                voice_name = self.voice_map[voice_id]

                # Настройки голоса
                rate = '+0%'
                pitch = '+0Hz'

                if voice_id == 'male_ru_deep':
                    rate = '-10%'
                    pitch = '-20Hz'
                elif voice_id == 'female_ru_soft':
                    rate = '-5%'
                    pitch = '+10Hz'

                logger.info(f"🔊 Генерация TTS для {agent_name}: {text[:50]}...")

                communicate = edge_tts.Communicate(
                    text=text,
                    voice=voice_name,
                    rate=rate,
                    pitch=pitch
                )

                # Создаем временный файл
                temp_audio = os.path.join('temp_videos', f'temp_audio_{int(time.time())}.mp3')
                await communicate.save(temp_audio)

                # Сохраняем в кэш
                audio_file = self.cache_manager.save_audio_file(text, voice_id, agent_name, temp_audio)

                # Удаляем временный файл
                try:
                    os.remove(temp_audio)
                except:
                    pass

                logger.info(f"💾 Аудио сохранено в кэш: {os.path.basename(audio_file)}")

            # Проверяем файл
            if not os.path.exists(audio_file) or os.path.getsize(audio_file) == 0:
                logger.error(f"❌ Аудио файл не создан или пустой: {audio_file}")
                return None

            # Воспроизводим локально для тестирования
            if self.pygame_available:
                try:
                    pygame.mixer.music.load(audio_file)
                    pygame.mixer.music.play()

                    duration = self._get_audio_duration(audio_file)
                    await asyncio.sleep(duration)

                    logger.info(f"🔊 Локальное воспроизведение завершено")
                except Exception as e:
                    logger.warning(f"Не удалось воспроизвести локально: {e}")

            # Отправляем в стрим через FFmpeg
            if self.ffmpeg_manager and self.ffmpeg_manager.is_streaming:
                logger.info(f"📤 Отправка аудио в стрим: {os.path.basename(audio_file)}")
                success = self.ffmpeg_manager.play_audio(audio_file, agent_name)

                if success:
                    logger.info(f"✅ Аудио отправлено в стрим")
                    return audio_file
                else:
                    logger.error(f"❌ Не удалось отправить аудио в стрим")
                    return None
            else:
                logger.warning("⚠️ FFmpeg стрим не активен, только локальное воспроизведение")
                return audio_file

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
            return 5.0


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

    def __init__(self, ffmpeg_manager: FFmpegPipeStreamManager = None, cache_manager: SimpleCacheManager = None):
        self.agents: List[AIAgent] = []
        self.cache_manager = cache_manager or SimpleCacheManager()
        self.tts_manager = EdgeTTSManager(ffmpeg_manager, self.cache_manager)
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
        """Запуск раунда дискуссии с кэшем"""
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

                # Генерация ответа
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

                # Генерация и отправка аудио (с кэшем)
                logger.info(f"🔊 Генерация/получение TTS для {agent.name}...")

                audio_file = await self.tts_manager.text_to_speech_and_stream(
                    text=message,
                    voice_id=agent.voice,
                    agent_name=agent.name
                )

                if audio_file:
                    logger.info(f"✅ Аудио обработано: {os.path.basename(audio_file)}")

                    # Ждем окончания аудио
                    audio_duration = self.tts_manager._get_audio_duration(audio_file)
                    logger.info(f"⏱️  Длительность аудио: {audio_duration:.1f} сек")

                    await asyncio.sleep(audio_duration + 1)
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
        cache_info = self.cache_manager.get_cache_info()

        return {
            'message_count': self.message_count,
            'discussion_round': self.discussion_round,
            'current_topic': self.current_topic,
            'is_active': self.is_discussion_active,
            'active_agent': self.active_agent,
            'agents_count': len(self.agents),
            'conversation_history': len(self.conversation_history),
            'ffmpeg_streaming': self.ffmpeg_manager.is_streaming if self.ffmpeg_manager else False,
            'cache_info': cache_info
        }


# ========== ГЛОБАЛЬНЫЕ ОБЪЕКТЫ ==========

# Создаем менеджер кэша
cache_manager = SimpleCacheManager()
# Запускаем очистку старых файлов (старше 7 дней) при старте
cache_manager.clear_cache(days_old=7)

# Создаем FFmpeg менеджер с кэшем
ffmpeg_manager = FFmpegPipeStreamManager(cache_manager)
# Создаем AI менеджер с кэшем
stream_manager = AIStreamManager(ffmpeg_manager, cache_manager)


# ========== FLASK РОУТЫ ==========

@app.route('/')
def index():
    """Главная страница"""
    return render_template('index.html',
                           agents=stream_manager.get_agents_state(),
                           topic=stream_manager.current_topic or "Загрузка темы...",
                           stats=stream_manager.get_stats())


@app.route('/api/cache/clear', methods=['POST'])
def clear_cache():
    """Очистка кэша"""
    try:
        cache_manager.clear_cache(days_old=0)  # 0 дней = очистить всё
        return jsonify({
            'success': True,
            'message': 'Кэш очищен'
        })
    except Exception as e:
        return jsonify({
            'success': False,
            'error': str(e)
        })


@app.route('/api/cache/info')
def get_cache_info():
    """Получить информацию о кэше"""
    try:
        cache_info = cache_manager.get_cache_info()
        return jsonify({
            'success': True,
            'cache_info': cache_info
        })
    except Exception as e:
        return jsonify({
            'success': False,
            'error': str(e)
        })


# ========== ЗАПУСК СЕРВЕРА ==========

def signal_handler(signum, frame):
    """Обработчик сигналов"""
    print(f"\n🛑 Получен сигнал {signum}. Завершение...")

    # Останавливаем стрим
    if ffmpeg_manager.is_streaming:
        ffmpeg_manager.stop_stream()

    sys.exit(0)


if __name__ == '__main__':
    # Регистрируем обработчики сигналов
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    print("=" * 70)
    print("🤖 AI AGENTS STREAM WITH FFMPEG (Simple Cache Version)")
    print("=" * 70)

    # Информация о кэше
    cache_info = cache_manager.get_cache_info()
    print(f"\n📊 Кэш аудио: {cache_info['audio_files']} файлов ({cache_info['audio_cache_size_mb']:.1f} MB)")
    print(f"📊 Кэш видео: {cache_info['video_files']} файлов ({cache_info['video_cache_size_mb']:.1f} MB)")
    print(f"📊 Всего: {cache_info['total_cache_size_mb']:.1f} MB")

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

    # Проверяем Edge TTS
    try:
        import edge_tts

        print("✅ Edge TTS установлен")
    except ImportError:
        print("❌ Edge TTS не установлен: pip install edge-tts")

    # Запускаем цикл дискуссии в отдельном потоке
    print("\n🔄 Запуск цикла дискуссии AI агентов...")


    async def discussion_loop():
        """Основной цикл дискуссии"""
        await asyncio.sleep(2)
        logger.info("🔄 Запуск цикла дискуссии AI агентов")

        if not stream_manager.current_topic:
            stream_manager.select_topic()

        print(f"📝 Начальная тема: {stream_manager.current_topic}")
        print("🤖 Агенты готовы к дискуссии")

        while True:
            try:
                if not stream_manager.is_discussion_active:
                    await asyncio.sleep(5)
                    continue

                await stream_manager.run_discussion_round()
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


    discussion_thread = threading.Thread(target=start_discussion_loop, daemon=True)
    discussion_thread.start()
    print("✅ Цикл дискуссии запущен")

    # Статистика агентов
    print(f"👥 Загружено {len(stream_manager.agents)} AI агентов:")
    for agent in stream_manager.agents:
        print(f"   • {agent.name} - {agent.expertise} ({agent.voice})")

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
        .controls { display: flex; flex-wrap: wrap; gap: 10px; margin: 20px 0; }
        button { padding: 10px 20px; background: #4a69ff; color: white; border: none; border-radius: 5px; cursor: pointer; }
        button:hover { background: #3a59ef; }
        .status { padding: 10px; border-radius: 5px; margin: 10px 0; }
        .status-streaming { background: #1a5a1a; }
        .status-stopped { background: #5a1a1a; }
        .cache-info { background: #2d2d2d; padding: 15px; border-radius: 10px; margin: 20px 0; }
        .cache-info h3 { margin-top: 0; }
        .cache-stats { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 10px; }
        .cache-stat { background: #3d3d3d; padding: 10px; border-radius: 5px; }
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h1>🤖 AI Agents Live Stream Control</h1>
            <p>Управление автономными ИИ агентами с кэшем аудио/видео</p>
        </div>

        <div id="status" class="status status-stopped">
            Статус: Загрузка...
        </div>

        <div id="cache-info" class="cache-info">
            <h3>📊 Информация о кэше:</h3>
            <div id="cache-stats" class="cache-stats">
                <div class="cache-stat">Аудио файлов: <span id="audio-files">0</span></div>
                <div class="cache-stat">Видео файлов: <span id="video-files">0</span></div>
                <div class="cache-stat">Общий размер: <span id="cache-size">0 MB</span></div>
                <div class="cache-stat">Попаданий в кэш: <span id="cache-hits">0</span></div>
            </div>
            <button onclick="clearCache()" style="margin-top: 10px; background: #ff4a4a;">🗑️ Очистить кэш</button>
        </div>

        <div id="topic-box" class="topic-box">
            <h3>Текущая тема дискуссии:</h3>
            <p id="current-topic">Загрузка...</p>
        </div>

        <div class="controls">
            <button onclick="startDiscussion()">▶️ Начать дискуссию</button>
            <button onclick="stopDiscussion()">⏹️ Остановить дискуссию</button>
            <button onclick="changeTopic()">🔄 Сменить тему</button>
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

        function clearCache() {
            if (confirm('Очистить весь кэш? Это удалит все сохраненные аудио и видео файлы.')) {
                fetch('/api/cache/clear', { method: 'POST' })
                    .then(response => response.json())
                    .then(data => {
                        if (data.success) {
                            alert('Кэш очищен');
                            updateCacheInfo();
                        } else {
                            alert('Ошибка: ' + data.error);
                        }
                    })
                    .catch(err => console.error('Error:', err));
            }
        }

        function updateCacheInfo() {
            fetch('/api/cache/info')
                .then(response => response.json())
                .then(data => {
                    if (data.success) {
                        const cache = data.cache_info;
                        document.getElementById('audio-files').textContent = cache.audio_files;
                        document.getElementById('video-files').textContent = cache.video_files;
                        document.getElementById('cache-size').textContent = cache.total_cache_size_mb.toFixed(1) + ' MB';
                        document.getElementById('cache-hits').textContent = cache.cache_hits;
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

                // Обновляем информацию о кэше
                if (stats.cache_info) {
                    document.getElementById('audio-files').textContent = stats.cache_info.audio_files;
                    document.getElementById('video-files').textContent = stats.cache_info.video_files;
                    document.getElementById('cache-size').textContent = stats.cache_info.total_cache_size_mb.toFixed(1) + ' MB';
                    document.getElementById('cache-hits').textContent = stats.cache_info.cache_hits;
                }
            })
            .catch(err => console.error('Error loading stats:', err));

        // Обновляем информацию о кэше каждые 30 секунд
        setInterval(updateCacheInfo, 30000);
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