#!/usr/bin/env python3
"""
🤖 AI Stream с FFmpeg стримингом на YouTube
Версия с интеграцией YouTube API
"""

import os
import sys
import json
import random
import asyncio
import threading
import logging
import time
import wave
import subprocess
import hashlib
from datetime import datetime
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

# Попробуем импортировать YouTube API
YOUTUBE_API_AVAILABLE = False
youtube_api_manager = None

try:
    from youtube_direct_api import YouTubeDirectStream

    # Проверяем наличие файла client_secrets.json
    if os.path.exists('client_secrets.json'):
        try:
            youtube_api_manager = YouTubeDirectStream()
            YOUTUBE_API_AVAILABLE = True
            print("✅ YouTube API доступен (client_secrets.json найден)")
        except Exception as e:
            print(f"⚠️ Ошибка инициализации YouTube API: {e}")
            print("Используйте ручной ввод Stream Key")
    else:
        print("⚠️ Файл client_secrets.json не найден.")
        print("Для автоматического создания трансляций через YouTube API:")
        print("1. Создайте проект в Google Cloud Console")
        print("2. Включите YouTube Data API v3")
        print("3. Создайте OAuth 2.0 Client ID")
        print("4. Сохраните как client_secrets.json в корне проекта")

except ImportError:
    print("⚠️ YouTube API модуль не найден.")
    print("Для автоматических трансляций установите:")
    print("pip install google-api-python-client google-auth-oauthlib google-auth-httplib2")
except Exception as e:
    print(f"⚠️ Неожиданная ошибка при импорте YouTube API: {e}")

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

            # Видео источник
            if self.video_source == "http":
                video_input = [
                    '-f', 'image2pipe',
                    '-i', 'http://localhost:5000/video_feed',
                    '-framerate', '30'
                ]
            elif self.video_source == "x11grab":
                video_input = [
                    '-f', 'x11grab',
                    '-i', ':99',
                    '-video_size', '1920x1080',
                    '-framerate', '30'
                ]
            else:
                video_input = [
                    '-f', 'lavfi',
                    '-i',
                    f'color=c=black:s=1920x1080:r=30:drawtext=text="AI\\\\ Stream\\\\ {datetime.now().strftime("%H:%M")}":fontcolor=white:fontsize=48:x=(w-text_w)/2:y=(h-text_h)/2'
                ]

            # Параметры аудио
            if use_audio and self.use_pyaudio:
                # Аудио вход из stdin (сырые данные)
                audio_input = [
                    '-f', 's16le',  # 16-bit little-endian PCM
                    '-ar', str(self.audio_sample_rate),
                    '-ac', str(self.audio_channels),
                    '-i', 'pipe:0',  # Читать из stdin
                ]
            else:
                # Тихий аудио
                audio_input = [
                    '-f', 'lavfi',
                    '-i', 'anullsrc=channel_layout=stereo:sample_rate=44100'
                ]

            # Команда FFmpeg
            ffmpeg_cmd = [
                'ffmpeg',

                # Видео источник (реальное время)
                '-re',
                '-f', 'lavfi',
                '-i', f'color=...:r=30',

                # Аудио источник (реальное время + синхронизация)
                '-re',
                '-f', 's16le',
                '-ar', '44100',
                '-ac', '2',
                '-i', 'pipe:0',

                # Кодеки
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

                # Важно: синхронизация аудио как главного потока
                '-async', '1',
                '-vsync', '1',
                '-flush_packets', '1',

                '-f', 'flv',
                self.rtmp_url
            ]

            logger.info(f"🚀 Запуск FFmpeg: {' '.join(ffmpeg_cmd[:10])}...")

            # Запускаем FFmpeg
            self.stream_process = subprocess.Popen(
                ffmpeg_cmd,
                stdin=subprocess.PIPE if (use_audio and self.use_pyaudio) else None,
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

            # Запуск обработчика аудио
            if use_audio and self.use_pyaudio:
                threading.Thread(target=self._audio_processor, daemon=True).start()

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

    def play_audio_file(self, audio_file: str):
        """Воспроизведение аудио файла (MP3) и отправка в стрим"""
        if not os.path.exists(audio_file):
            logger.error(f"❌ Аудио файл не найден: {audio_file}")
            return False

        try:
            # Используем ffmpeg для конвертации MP3 в сырое аудио
            ffmpeg_cmd = [
                'ffmpeg',
                '-i', audio_file,  # Входной MP3 файл
                '-f', 's16le',  # Формат выхода: 16-bit PCM
                '-ar', '44100',  # Частота дискретизации
                '-ac', '2',  # Стерео
                '-acodec', 'pcm_s16le',  # Кодек для выхода
                '-'  # Вывод в stdout
            ]

            logger.debug(f"Конвертируем аудио: {os.path.basename(audio_file)}")

            # Запускаем ffmpeg для конвертации
            process = subprocess.Popen(
                ffmpeg_cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=10 ** 8  # Большой буфер для плавного воспроизведения
            )

            # Читаем выходные данные и отправляем в стрим
            while True:
                audio_data = process.stdout.read(4096)  # Читаем порциями
                if not audio_data:
                    break

                # Отправляем в FFmpeg stdin
                if self.ffmpeg_stdin:
                    try:
                        self.ffmpeg_stdin.write(audio_data)
                        self.ffmpeg_stdin.flush()
                    except BrokenPipeError:
                        logger.error("❌ FFmpeg stdin закрыт")
                        break

            # Ждем завершения конвертации
            process.wait()

            # Проверяем на ошибки
            if process.returncode != 0:
                error_output = process.stderr.read().decode('utf-8', errors='ignore')
                logger.error(f"Ошибка конвертации аудио: {error_output}")
                return False

            logger.info(f"✅ Аудио отправлено в стрим: {os.path.basename(audio_file)}")
            return True

        except Exception as e:
            logger.error(f"❌ Ошибка воспроизведения аудио файла: {e}")
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
        """Проверка подключения к YouTube (ИСПРАВЛЕННАЯ ВЕРСИЯ)"""
        if not self.rtmp_url:
            return {'connected': False, 'error': 'No RTMP URL'}

        try:
            # Команда для ПРОВЕРКИ подключения (не стриминга!)
            # ffprobe читает метаданные, а не стримит
            cmd = [
                'ffprobe',
                '-v', 'error',
                '-rw_timeout', '5000000',  # 5 секунд таймаут на чтение
                '-timeout', '5000000',  # 5 секунд общий таймаут
                '-analyzeduration', '10000000',
                '-probesize', '10000000',
                '-show_entries', 'stream=codec_name',  # Минимальная информация
                self.rtmp_url
            ]

            logger.debug(f"Проверка подключения: {' '.join(cmd)}")

            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=10
            )

            logger.debug(f"FFprobe результат: {result.returncode}")
            logger.debug(f"FFprobe stdout: {result.stdout[:200]}")
            logger.debug(f"FFprobe stderr: {result.stderr[:200]}")

            # YouTube обычно возвращает 1 даже при успешной проверке
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
    return render_template('index.html',
                           agents=stream_manager.get_agents_state(),
                           topic=stream_manager.current_topic or "Загрузка темы...",
                           stats=stream_manager.get_stats(),
                           youtube_api_available=YOUTUBE_API_AVAILABLE)


@app.route('/health')
def health():
    """Проверка здоровья"""
    return jsonify({
        'status': 'ok',
        'time': datetime.now().isoformat(),
        'agents': len(stream_manager.agents),
        'streaming': ffmpeg_manager.is_streaming,
        'discussion_active': stream_manager.is_discussion_active,
        'youtube_api_available': YOUTUBE_API_AVAILABLE
    })


@app.route('/api/start_stream', methods=['POST'])
def start_stream():
    """Запуск FFmpeg стрима (принимает разные форматы)"""
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
    """Запуск стрима через YouTube API (автоматическое создание)"""
    try:
        if not YOUTUBE_API_AVAILABLE or not youtube_api_manager:
            return jsonify({
                'status': 'error',
                'message': 'YouTube API не доступен. Установите зависимости и client_secrets.json'
            }), 501

        # Получаем параметры
        if request.is_json:
            data = request.get_json()
        else:
            data = request.form

        title = data.get('title', "🤖 AI Agents Live: Научные дебаты ИИ")
        description = data.get('description', Config.STREAM_DESCRIPTION)

        logger.info(f"🎬 Запуск YouTube стрима через API: {title}")

        # Запускаем стрим через YouTube API
        try:
            success = youtube_api_manager.start_stream(title, description)
        except Exception as e:
            logger.error(f"Ошибка YouTube API: {e}")
            return jsonify({
                'status': 'error',
                'message': f'Ошибка YouTube API: {str(e)}'
            }), 500

        if not success:
            return jsonify({
                'status': 'error',
                'message': 'Не удалось создать YouTube трансляцию'
            }), 500

        # Получаем информацию о стриме
        try:
            stream_info = youtube_api_manager.get_stream_info()
        except Exception as e:
            logger.error(f"Ошибка получения stream_info: {e}")
            return jsonify({
                'status': 'error',
                'message': 'Не удалось получить информацию о стриме'
            }), 500

        if not stream_info or 'stream_key' not in stream_info:
            return jsonify({
                'status': 'error',
                'message': 'Не удалось получить Stream Key от YouTube API'
            }), 500

        # Устанавливаем stream key в FFmpeg manager
        ffmpeg_manager.set_stream_key(stream_info['stream_key'])

        # Запускаем FFmpeg стрим
        if ffmpeg_manager.start_stream():
            return jsonify({
                'status': 'started',
                'broadcast_id': youtube_api_manager.broadcast_id,
                'stream_id': youtube_api_manager.stream_id,
                'watch_url': f"https://youtube.com/watch?v={youtube_api_manager.broadcast_id}",
                'stream_key': stream_info['stream_key'],
                'rtmp_url': stream_info['rtmp_url'],
                'pid': ffmpeg_manager.ffmpeg_pid,
                'message': 'YouTube трансляция создана и стрим запущен'
            })
        else:
            # Если FFmpeg не запустился, останавливаем YouTube трансляцию
            try:
                youtube_api_manager.end_stream()
            except:
                pass
            return jsonify({
                'status': 'error',
                'message': 'Не удалось запустить FFmpeg стрим'
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
        if not YOUTUBE_API_AVAILABLE or not youtube_api_manager:
            return jsonify({
                'status': 'error',
                'message': 'YouTube API не доступен'
            }), 501

        if request.is_json:
            data = request.get_json()
        else:
            data = request.form

        action = data.get('action', '')

        if action == 'get_info':
            try:
                info = youtube_api_manager.get_stream_info()
                return jsonify({
                    'status': 'success',
                    'broadcast_id': youtube_api_manager.broadcast_id,
                    'stream_id': youtube_api_manager.stream_id,
                    'is_live': youtube_api_manager.is_live,
                    'stream_info': info
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
                result = youtube_api_manager.update_broadcast_info(title, description)
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
                result = youtube_api_manager.end_stream()
                if result:
                    return jsonify({'status': 'ended'})
                return jsonify({'status': 'error', 'message': 'Не удалось завершить'})
            except Exception as e:
                return jsonify({'status': 'error', 'message': str(e)})

        elif action == 'get_chat_id':
            try:
                chat_id = youtube_api_manager.get_chat_id()
                if chat_id:
                    return jsonify({'status': 'success', 'chat_id': chat_id})
                return jsonify({'status': 'error', 'message': 'Чат не найден'})
            except Exception as e:
                return jsonify({'status': 'error', 'message': str(e)})

        elif action == 'create_test_stream':
            try:
                title = data.get('title', 'Тестовая трансляция')
                description = data.get('description', 'Тестовая трансляция создана через API')

                # Создаем тестовую трансляцию
                broadcast = youtube_api_manager.create_live_broadcast(title, description)
                stream = youtube_api_manager.create_stream()

                if broadcast and stream:
                    youtube_api_manager.bind_broadcast_to_stream()
                    return jsonify({
                        'status': 'created',
                        'broadcast_id': youtube_api_manager.broadcast_id,
                        'stream_id': youtube_api_manager.stream_id
                    })
                return jsonify({'status': 'error', 'message': 'Не удалось создать'})
            except Exception as e:
                return jsonify({'status': 'error', 'message': str(e)})

        else:
            return jsonify({
                'status': 'error',
                'message': 'Неизвестное действие',
                'available_actions': ['get_info', 'update_info', 'end_stream', 'get_chat_id', 'create_test_stream']
            })

    except Exception as e:
        logger.error(f"Ошибка управления YouTube: {e}")
        return jsonify({'status': 'error', 'message': str(e)})


@app.route('/api/youtube_status')
def youtube_status():
    """Статус YouTube трансляции"""
    try:
        if not YOUTUBE_API_AVAILABLE or not youtube_api_manager:
            return jsonify({
                'available': False,
                'message': 'YouTube API не доступен'
            })

        return jsonify({
            'available': True,
            'has_broadcast': youtube_api_manager.broadcast_id is not None,
            'has_stream': youtube_api_manager.stream_id is not None,
            'is_live': youtube_api_manager.is_live,
            'broadcast_id': youtube_api_manager.broadcast_id,
            'stream_id': youtube_api_manager.stream_id
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
        if YOUTUBE_API_AVAILABLE and youtube_api_manager and youtube_api_manager.is_live:
            try:
                youtube_api_manager.end_stream()
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

    # Добавляем информацию о YouTube API
    if YOUTUBE_API_AVAILABLE and youtube_api_manager:
        status['youtube'] = {
            'available': True,
            'has_broadcast': youtube_api_manager.broadcast_id is not None,
            'is_live': youtube_api_manager.is_live,
            'broadcast_id': youtube_api_manager.broadcast_id
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
    if YOUTUBE_API_AVAILABLE and youtube_api_manager:
        stats['youtube'] = {
            'broadcast_id': youtube_api_manager.broadcast_id,
            'is_live': youtube_api_manager.is_live,
            'stream_id': youtube_api_manager.stream_id
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
    return render_template('youtube_control.html',
                           youtube_api_available=YOUTUBE_API_AVAILABLE)


# ========== WEBSOCKET HANDLERS ==========

@socketio.on('connect')
def handle_connect():
    """Подключение клиента"""
    client_id = request.sid
    logger.info(f"📱 Клиент подключился: {client_id}")

    # Отправляем начальное состояние
    socketio.emit('connected', {
        'status': 'connected',
        'client_id': client_id,
        'agents': stream_manager.get_agents_state(),
        'topic': stream_manager.current_topic or stream_manager.select_topic(),
        'stats': stream_manager.get_stats(),
        'stream_status': ffmpeg_manager.get_status(),
        'server_time': datetime.now().isoformat(),
        'youtube_api_available': YOUTUBE_API_AVAILABLE,
        'youtube_broadcast_id': youtube_api_manager.broadcast_id if youtube_api_manager else None,
        'youtube_is_live': youtube_api_manager.is_live if youtube_api_manager else False
    })


@socketio.on('disconnect')
def handle_disconnect():
    """Отключение клиента"""
    logger.info(f"📱 Клиент отключился: {request.sid}")


@socketio.on('request_update')
def handle_update_request():
    """Запрос обновления"""
    socketio.emit('update', {
        'agents': stream_manager.get_agents_state(),
        'topic': stream_manager.current_topic,
        'stats': stream_manager.get_stats(),
        'stream_status': ffmpeg_manager.get_status(),
        'youtube_broadcast_id': youtube_api_manager.broadcast_id if youtube_api_manager else None,
        'youtube_is_live': youtube_api_manager.is_live if youtube_api_manager else False
    })


# ========== ЗАПУСК СЕРВЕРА ==========

def signal_handler(signum, frame):
    """Обработчик сигналов"""
    print(f"\n🛑 Получен сигнал {signum}. Завершение...")

    # Останавливаем стрим
    if ffmpeg_manager.is_streaming:
        ffmpeg_manager.stop_stream()

    # Останавливаем YouTube трансляцию если активна
    if YOUTUBE_API_AVAILABLE and youtube_api_manager and youtube_api_manager.is_live:
        try:
            youtube_api_manager.end_stream()
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

    # Информация о зависимостях
    print(f"📦 Версии зависимостей:")
    print(f"   Flask: 2.3.0")
    print(f"   Flask-SocketIO: 5.3.0")
    print(f"   OpenAI: >=1.3.0")
    print(f"   Edge TTS: >=6.1.9")
    print(f"   FFmpeg: системный")

    if YOUTUBE_API_AVAILABLE:
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
    discussion_thread = threading.Thread(target=start_discussion_loop, daemon=True)
    discussion_thread.start()

    print("🚀 Запуск веб-сервера...")
    print("🌐 Основной интерфейс: http://localhost:5000")
    print("🎬 YouTube API интерфейс: http://localhost:5000/youtube-control")
    print("🔧 API Endpoints:")
    print("   GET  /health                     - Проверка здоровья")
    print("   POST /api/start_stream           - Ручной запуск стрима")
    print("   POST /api/start_youtube_stream   - Автоматический запуск через YouTube API")
    print("   POST /api/youtube_control        - Управление YouTube трансляцией")
    print("   GET  /api/stream_status          - Статус стрима")
    print("   POST /api/test_audio             - Тест звука")
    print("")
    print("📝 Доступные методы запуска стрима:")
    print("   1. Ручной: Ввести Stream Key в основном интерфейсе")
    print("   2. Автоматический: Использовать YouTube API (требуется client_secrets.json)")
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