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

        # Очередь и управление аудио
        self.audio_queue = []
        self.current_audio = None
        self.is_playing_audio = False
        self.audio_processor_thread = None

        # Очередь и управление видео
        self.video_queue = []
        self.current_video = None
        self.is_playing_video = False
        self.video_processor_thread = None

        self.current_video_file = None
        self.video_position = 0
        self.video_duration = 0
        self.video_start_time = 0

        # Конфигурация аудио
        self.audio_sample_rate = 44100
        self.audio_channels = 2
        self.audio_format = 's16le'
        self.bytes_per_sample = 2

        # Конфигурация видео
        self.video_width = 1920
        self.video_height = 1080
        self.video_fps = 30
        self.video_bitrate = '4500k'

        # Для генерации тишины
        self.silence_chunk_duration = 0.1  # 100ms
        self.silence_chunk_size = int(self.audio_sample_rate * self.audio_channels *
                                      self.bytes_per_sample * self.silence_chunk_duration)

        # Текущий источник видео
        self.current_video_source = "color=size=1920x1080:rate=30:color=black"

        logger.info("FFmpeg Stream Manager с поддержкой видео инициализирован")

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

    def add_video_to_queue(self, video_file: str, duration: float = None) -> bool:
        """Добавление видео файла в очередь на воспроизведение"""
        if not os.path.exists(video_file):
            logger.error(f"❌ Видео файл не найден: {video_file}")
            return False

        # Получаем информацию о видео
        video_info = self._get_video_info(video_file)
        if not video_info:
            logger.error(f"❌ Не удалось получить информацию о видео: {video_file}")
            return False

        # Определяем длительность
        actual_duration = duration or video_info.get('duration', 5.0)

        self.video_queue.append({
            'path': video_file,
            'duration': actual_duration,
            'info': video_info
        })

        logger.info(f"🎬 Видео добавлено в очередь: {os.path.basename(video_file)} ({actual_duration:.1f} сек)")
        logger.info(f"📊 Размер очереди видео: {len(self.video_queue)} файлов")
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

    def _prepare_video_file(self, video_file: str) -> Optional[str]:
        """Подготовка видео файла (конвертация если нужно)"""
        if not os.path.exists(video_file):
            return None

        # Если видео уже в нужном формате, возвращаем как есть
        video_info = self._get_video_info(video_file)
        if not video_info:
            return None

        # Проверяем, нужно ли конвертировать
        needs_conversion = (
                video_info.get('codec') != 'h264' or
                video_info.get('fps') != self.video_fps or
                video_info.get('width') != self.video_width or
                video_info.get('height') != self.video_height
        )

        if not needs_conversion:
            return video_file

        # Конвертируем видео в нужный формат
        try:
            temp_video = tempfile.NamedTemporaryFile(suffix='.mp4', delete=False)
            temp_video.close()

            convert_cmd = [
                'ffmpeg',
                '-i', video_file,
                '-c:v', 'libx264',
                '-preset', 'ultrafast',
                '-tune', 'zerolatency',
                '-pix_fmt', 'yuv420p',
                '-s', f'{self.video_width}x{self.video_height}',
                '-r', str(self.video_fps),
                '-b:v', self.video_bitrate,
                '-maxrate', self.video_bitrate,
                '-bufsize', f'{int(int(self.video_bitrate[:-1]) * 2)}k',
                '-g', '60',
                '-c:a', 'aac',
                '-b:a', '128k',
                '-ar', '44100',
                '-ac', '2',
                '-f', 'mp4',
                '-y',
                temp_video.name
            ]

            logger.info(f"🔄 Конвертация видео: {os.path.basename(video_file)}")

            result = subprocess.run(
                convert_cmd,
                capture_output=True,
                text=True,
                timeout=60  # Даем больше времени для конвертации видео
            )

            if result.returncode != 0:
                logger.error(f"❌ Ошибка конвертации видео: {result.stderr[:500]}")
                os.unlink(temp_video.name)
                return None

            # Проверяем размер файла
            if os.path.getsize(temp_video.name) < 1024:  # Минимум 1KB
                logger.error("❌ Видео файл слишком маленький")
                os.unlink(temp_video.name)
                return None

            logger.info(f"✅ Видео сконвертировано: {os.path.getsize(temp_video.name) / 1024 / 1024:.1f} MB")
            return temp_video.name

        except Exception as e:
            logger.error(f"❌ Ошибка подготовки видео: {e}", exc_info=True)
            if os.path.exists(temp_video.name):
                os.unlink(temp_video.name)
            return None

    def _generate_silence_chunk(self) -> bytes:
        """Генерация чанка тишины (нулевые байты)"""
        return b'\x00' * self.silence_chunk_size

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
                '-f', 's16le',  # Формат вывода
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

    def _continuous_audio_processor(self):
        """Непрерывный процессор аудио"""
        logger.info("🚀 Запуск непрерывного аудио процессора")

        while self.is_streaming and self.ffmpeg_stdin:
            try:
                # Если есть аудио в очереди - воспроизводим его
                if self.audio_queue:
                    self.is_playing_audio = True
                    audio_file = self.audio_queue.pop(0)
                    logger.info(f"🎵 Начинаем воспроизведение аудио: {os.path.basename(audio_file)}")

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

    def _video_stream_processor(self):
        """Процессор для отправки видео файлов в стрим"""
        logger.info("🎬 Запуск видео процессора")

        while self.is_streaming:
            try:
                if self.video_queue and not self.is_playing_video:
                    video_item = self.video_queue.pop(0)
                    video_path = video_item['path']
                    duration = video_item['duration']

                    self.is_playing_video = True
                    logger.info(
                        f"🎬 Начинаем воспроизведение видео: {os.path.basename(video_path)} ({duration:.1f} сек)")

                    # Подготавливаем видео файл
                    prepared_video = self._prepare_video_file(video_path)

                    if prepared_video:
                        # Создаем временный файл с командой FFmpeg для видео
                        temp_script = tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False)
                        temp_script.write(f"file '{prepared_video}'\n")
                        temp_script.close()

                        # Запускаем FFmpeg для отправки видео
                        video_cmd = [
                            'ffmpeg',
                            '-re',  # Реальное время
                            '-f', 'concat',
                            '-safe', '0',
                            '-i', temp_script.name,
                            '-c', 'copy',
                            '-f', 'flv',
                            '-flvflags', 'no_duration_filesize',
                            self.rtmp_url
                        ]

                        logger.info(f"📤 Отправка видео в стрим: {os.path.basename(video_path)}")

                        # Запускаем процесс отправки видео
                        video_process = subprocess.Popen(
                            video_cmd,
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.PIPE,
                            text=True
                        )

                        # Ждем завершения видео
                        time.sleep(duration + 1)  # Даем дополнительную секунду

                        # Завершаем процесс
                        video_process.terminate()
                        video_process.wait(timeout=5)

                        # Очищаем временные файлы
                        os.unlink(temp_script.name)
                        if prepared_video != video_path:
                            os.unlink(prepared_video)

                        logger.info(f"✅ Видео отправлено в стрим")

                    self.is_playing_video = False

                    # Пауза между видео
                    time.sleep(1)

                else:
                    # Если нет видео в очереди, ждем
                    time.sleep(0.1)

            except Exception as e:
                logger.error(f"❌ Ошибка в видео процессоре: {e}", exc_info=True)
                self.is_playing_video = False
                time.sleep(1)

        logger.info("🛑 Видео процессор остановлен")

    def start_stream(self, use_audio: bool = True):
        """Запуск FFmpeg стрима с поддержкой видео файлов"""
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
            self.current_video_file = None

            # Определяем основной источник видео
            # Если есть видео в очереди - используем его, иначе черный экран
            video_source = "color=size=1920x1080:rate=30:color=black"

            if self.video_queue:
                video_item = self.video_queue[0]
                video_path = video_item['path']

                # Используем видео как основной источник
                video_source = f"movie='{video_path}':loop=0:setpts=N/FRAME_RATE/TB"
                self.current_video_file = video_path
                self.video_duration = video_item['duration']
                self.video_start_time = time.time()
                logger.info(f"🎬 Используем видео как источник: {os.path.basename(video_path)}")

            # Команда FFmpeg с динамическим видео источником
            ffmpeg_cmd = [
                'ffmpeg',
                '-re',  # Реальное время
                '-fflags', '+genpts',

                # ДИНАМИЧЕСКИЙ ВИДЕО ИСТОЧНИК
                '-f', 'lavfi',
                '-i', video_source,

                # Аудио источник через stdin
                '-f', 's16le',
                '-ar', str(self.audio_sample_rate),
                '-ac', str(self.audio_channels),
                '-channel_layout', 'stereo',
                '-i', 'pipe:0',

                # Видео настройки
                '-c:v', 'libx264',
                '-preset', 'veryfast',
                '-tune', 'zerolatency',
                '-pix_fmt', 'yuv420p',
                '-g', '60',
                '-b:v', '4500k',
                '-maxrate', '4500k',
                '-bufsize', '9000k',
                '-r', '30',
                '-x264-params', 'keyint=60:min-keyint=60:scenecut=0',

                # Аудио настройки
                '-c:a', 'aac',
                '-b:a', '128k',
                '-ar', '44100',
                '-ac', '2',
                '-acodec', 'aac',

                # Вывод
                '-f', 'flv',
                '-flvflags', 'no_duration_filesize',
                self.rtmp_url
            ]

            logger.info(f"🚀 Запуск FFmpeg стрима")
            logger.debug(f"Видео источник: {video_source}")

            # Запускаем FFmpeg
            self.stream_process = subprocess.Popen(
                ffmpeg_cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                bufsize=0,
                text=False
            )

            self.is_streaming = True
            self.ffmpeg_pid = self.stream_process.pid
            self.ffmpeg_stdin = self.stream_process.stdin

            logger.info(f"✅ FFmpeg запущен (PID: {self.ffmpeg_pid})")

            # Запускаем мониторинг
            threading.Thread(target=self._monitor_ffmpeg, daemon=True).start()

            # Запускаем непрерывный аудио процессор
            self.audio_processor_thread = threading.Thread(
                target=self._continuous_audio_processor,
                daemon=True
            )
            self.audio_processor_thread.start()

            # Запускаем видео процессор
            self.video_processor_thread = threading.Thread(
                target=self._video_stream_processor,
                daemon=True
            )
            self.video_processor_thread.start()

            socketio.emit('stream_started', {
                'pid': self.ffmpeg_pid,
                'rtmp_url': self.rtmp_url,
                'has_video': bool(self.current_video_file)
            })

            return {'success': True, 'pid': self.ffmpeg_pid}

        except Exception as e:
            logger.error(f"❌ Ошибка запуска FFmpeg: {e}", exc_info=True)
            self.is_streaming = False
            return {'success': False, 'error': str(e)}

    def _monitor_ffmpeg(self):
        """Мониторинг процесса FFmpeg"""
        try:
            stream_connected = False

            for line in iter(self.stream_process.stderr.readline, b''):
                line = line.decode('utf-8', errors='ignore').strip()

                # Отладочная информация
                if 'frame=' in line and 'fps=' in line:
                    current_time = time.time()
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

                # Ошибки
                elif any(x in line.lower() for x in ['error', 'failed', 'invalid']):
                    logger.error(f"⚠️ FFmpeg error: {line}")
                    socketio.emit('stream_warning', {'message': line})

            # Процесс завершен
            return_code = self.stream_process.wait()
            logger.info(f"FFmpeg завершился с кодом: {return_code}")

        except Exception as e:
            logger.error(f"Ошибка мониторинга FFmpeg: {e}")
        finally:
            self.is_streaming = False
            socketio.emit('stream_stopped', {'time': datetime.now().isoformat()})

    def stop_stream(self):
        """Корректная остановка стрима"""
        logger.info("🛑 Остановка FFmpeg стрима...")

        self.is_streaming = False

        # Очищаем очереди
        self.audio_queue.clear()
        self.video_queue.clear()

        # Даем время процессорам завершиться
        if self.audio_processor_thread and self.audio_processor_thread.is_alive():
            self.audio_processor_thread.join(timeout=2.0)

        if self.video_processor_thread and self.video_processor_thread.is_alive():
            self.video_processor_thread.join(timeout=2.0)

        try:
            # Закрываем stdin
            if self.ffmpeg_stdin:
                self.ffmpeg_stdin.close()

            # Graceful shutdown
            if self.stream_process and self.stream_process.poll() is None:
                self.stream_process.terminate()

                # Ждем корректного завершения
                for i in range(10):
                    if self.stream_process.poll() is not None:
                        break
                    time.sleep(0.5)

                # Принудительное завершение если нужно
                if self.stream_process.poll() is None:
                    self.stream_process.kill()
                    self.stream_process.wait()

        except Exception as e:
            logger.error(f"Ошибка при остановке: {e}")

        logger.info("✅ FFmpeg стрим остановлен")
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
    """Генератор видео для стрима"""

    def __init__(self, ffmpeg_manager: FFmpegStreamManager = None):
        self.ffmpeg_manager = ffmpeg_manager
        self.video_queue = []
        self.is_playing_video = False
        self.video_width = 1920
        self.video_height = 1080
        self.fps = 30
        self.video_cache_dir = 'video_cache'
        os.makedirs(self.video_cache_dir, exist_ok=True)

        # Шрифты для текста (если доступны)
        self.fonts = self._load_fonts()

        logger.info("Video Generator инициализирован")

    def _load_fonts(self):
        """Загрузка шрифтов"""
        fonts = {}
        font_paths = [
            '/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf',
            '/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf',
            '/System/Library/Fonts/Supplemental/Arial Bold.ttf',
            'C:/Windows/Fonts/arialbd.ttf',
            './fonts/arial.ttf'
        ]

        for path in font_paths:
            if os.path.exists(path):
                try:
                    fonts['bold'] = ImageFont.truetype(path, 40)
                    fonts['regular'] = ImageFont.truetype(path, 32)
                    fonts['small'] = ImageFont.truetype(path, 24)
                    logger.info(f"✅ Загружен шрифт: {path}")
                    return fonts
                except Exception as e:
                    continue

        # Если шрифты не найдены, используем стандартный
        logger.warning("⚠️ Шрифты не найдены, используем стандартный")
        fonts['bold'] = ImageFont.load_default()
        fonts['regular'] = ImageFont.load_default()
        fonts['small'] = ImageFont.load_default()
        return fonts

    def create_agent_intro_video(self, agent_name: str, expertise: str,
                                 avatar_color: str, message: str, duration: float = 7.0) -> str:
        """Создание видео-интро для агента"""
        try:
            # Создаем уникальное имя файла
            timestamp = int(time.time())
            video_filename = f"intro_{agent_name}_{timestamp}.mp4"
            video_path = os.path.join(self.video_cache_dir, video_filename)

            # Параметры видео
            fps = self.fps
            total_frames = int(duration * fps)

            # Создаем видео с помощью OpenCV
            fourcc = cv2.VideoWriter_fourcc(*'mp4v')
            video_writer = cv2.VideoWriter(video_path, fourcc, fps,
                                           (self.video_width, self.video_height))

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
                for r in range(radius, 0, -1):
                    alpha = int(255 * (r / radius) * progress)
                    color = (*rgb, alpha)
                    draw.ellipse([center_x - r, center_y - r,
                                  center_x + r, center_y + r],
                                 fill=rgb, outline=(255, 255, 255, 100))

                # Имя агента
                if frame_num > fps * 0.5:  # Появляется через 0.5 секунды
                    name_progress = min(1.0, (frame_num - fps * 0.5) / (fps * 0.5))
                    name_alpha = int(255 * name_progress)
                    try:
                        draw.text((center_x, center_y + 180), agent_name,
                                  font=self.fonts['bold'], fill=(255, 255, 255, name_alpha),
                                  anchor="mm")
                    except:
                        draw.text((center_x, center_y + 180), agent_name,
                                  fill=(255, 255, 255, name_alpha), anchor="mm")

                # Экспертиза
                if frame_num > fps * 0.8:
                    exp_progress = min(1.0, (frame_num - fps * 0.8) / (fps * 0.5))
                    exp_alpha = int(200 * exp_progress)
                    try:
                        draw.text((center_x, center_y + 230), expertise,
                                  font=self.fonts['small'], fill=(200, 200, 255, exp_alpha),
                                  anchor="mm")
                    except:
                        draw.text((center_x, center_y + 230), expertise,
                                  fill=(200, 200, 255, exp_alpha), anchor="mm")

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
                    for i, line in enumerate(lines):
                        text_y = bg_top + 20 + i * 40
                        text_alpha = int(255 * msg_progress)
                        try:
                            draw.text((center_x, text_y), line,
                                      font=self.fonts['regular'],
                                      fill=(255, 255, 255, text_alpha),
                                      anchor="mm")
                        except:
                            draw.text((center_x, text_y), line,
                                      fill=(255, 255, 255, text_alpha), anchor="mm")

                # Конвертируем PIL в OpenCV
                cv_img = cv2.cvtColor(numpy.array(img), cv2.COLOR_RGB2BGR)
                video_writer.write(cv_img)

            video_writer.release()

            # Проверяем что файл создан
            if os.path.exists(video_path) and os.path.getsize(video_path) > 0:
                logger.info(f"✅ Видео создано: {video_path} ({duration} сек)")
                return video_path

            return None

        except Exception as e:
            logger.error(f"❌ Ошибка создания видео: {e}", exc_info=True)
            return None

    def create_transition_video(self, from_topic: str, to_topic: str,
                                duration: float = 5.0) -> str:
        """Создание переходного видео между темами"""
        try:
            timestamp = int(time.time())
            video_filename = f"transition_{timestamp}.mp4"
            video_path = os.path.join(self.video_cache_dir, video_filename)

            fps = self.fps
            total_frames = int(duration * fps)

            fourcc = cv2.VideoWriter_fourcc(*'mp4v')
            video_writer = cv2.VideoWriter(video_path, fourcc, fps,
                                           (self.video_width, self.video_height))

            for frame_num in range(total_frames):
                progress = frame_num / total_frames

                # Создаем плавный переход
                img = Image.new('RGB', (self.video_width, self.video_height),
                                (10, 10, 20))
                draw = ImageDraw.Draw(img)

                # Анимация смены темы
                if progress < 0.5:
                    # Исчезает старая тема
                    alpha = int(255 * (1 - progress * 2))
                    try:
                        draw.text((self.video_width // 2, self.video_height // 2 - 50),
                                  "Тема обсуждения:",
                                  font=self.fonts['bold'],
                                  fill=(200, 200, 255, alpha),
                                  anchor="mm")
                        draw.text((self.video_width // 2, self.video_height // 2 + 10),
                                  from_topic,
                                  font=self.fonts['regular'],
                                  fill=(255, 255, 255, alpha),
                                  anchor="mm")
                    except:
                        draw.text((self.video_width // 2, self.video_height // 2 - 50),
                                  "Тема обсуждения:",
                                  fill=(200, 200, 255, alpha), anchor="mm")
                        draw.text((self.video_width // 2, self.video_height // 2 + 10),
                                  from_topic,
                                  fill=(255, 255, 255, alpha), anchor="mm")
                else:
                    # Появляется новая тема
                    alpha = int(255 * ((progress - 0.5) * 2))
                    try:
                        draw.text((self.video_width // 2, self.video_height // 2 - 50),
                                  "Новая тема:",
                                  font=self.fonts['bold'],
                                  fill=(200, 255, 200, alpha),
                                  anchor="mm")
                        draw.text((self.video_width // 2, self.video_height // 2 + 10),
                                  to_topic,
                                  font=self.fonts['regular'],
                                  fill=(255, 255, 255, alpha),
                                  anchor="mm")
                    except:
                        draw.text((self.video_width // 2, self.video_height // 2 - 50),
                                  "Новая тема:",
                                  fill=(200, 255, 200, alpha), anchor="mm")
                        draw.text((self.video_width // 2, self.video_height // 2 + 10),
                                  to_topic,
                                  fill=(255, 255, 255, alpha), anchor="mm")

                # Анимационные элементы
                for i in range(20):
                    x = int((self.video_width * progress + i * 100) % self.video_width)
                    y = int(self.video_height * 0.8 +
                            numpy.sin(progress * 10 + i * 0.5) * 20)
                    radius = int(5 + numpy.sin(progress * 5 + i) * 3)
                    draw.ellipse([x - radius, y - radius, x + radius, y + radius],
                                 fill=(100, 100, 255, 100))

                cv_img = cv2.cvtColor(numpy.array(img), cv2.COLOR_RGB2BGR)
                video_writer.write(cv_img)

            video_writer.release()

            if os.path.exists(video_path):
                logger.info(f"✅ Переходное видео создано: {video_path}")
                return video_path

            return None

        except Exception as e:
            logger.error(f"❌ Ошибка создания переходного видео: {e}")
            return None

    def add_video_to_stream(self, video_path: str) -> bool:
        """Добавление видео в стрим через FFmpeg"""
        if not self.ffmpeg_manager or not self.ffmpeg_manager.is_streaming:
            logger.error("❌ FFmpeg стрим не активен")
            return False

        if not os.path.exists(video_path):
            logger.error(f"❌ Видео файл не найден: {video_path}")
            return False

        try:
            # Получаем информацию о видео
            video_info = self._get_video_info(video_path)
            if not video_info:
                return False

            duration = video_info.get('duration', 5.0)

            # Создаем команду для вставки видео в стрим
            # Используем сложный фильтр FFmpeg для наложения видео
            temp_output = tempfile.NamedTemporaryFile(suffix='.ts', delete=False)
            temp_output.close()

            # Конвертируем видео в формат для стрима
            convert_cmd = [
                'ffmpeg',
                '-i', video_path,
                '-c:v', 'libx264',
                '-preset', 'ultrafast',
                '-tune', 'zerolatency',
                '-pix_fmt', 'yuv420p',
                '-b:v', '3000k',
                '-maxrate', '3000k',
                '-bufsize', '6000k',
                '-g', '30',
                '-c:a', 'aac',
                '-b:a', '128k',
                '-f', 'mpegts',
                '-y',
                temp_output.name
            ]

            logger.info(f"🎬 Конвертация видео для стрима: {os.path.basename(video_path)}")

            result = subprocess.run(
                convert_cmd,
                capture_output=True,
                text=True,
                timeout=30
            )

            if result.returncode != 0:
                logger.error(f"❌ Ошибка конвертации видео: {result.stderr[:500]}")
                os.unlink(temp_output.name)
                return False

            # Теперь нам нужно отправить видео в FFmpeg
            # В данном случае мы будем использовать альтернативный подход:
            # Создаем временный файл и отправляем его как источник

            # Добавляем в очередь на обработку
            self.video_queue.append(temp_output.name)
            logger.info(f"📥 Видео добавлено в очередь: {os.path.basename(video_path)}")

            return True

        except Exception as e:
            logger.error(f"❌ Ошибка добавления видео в стрим: {e}", exc_info=True)
            return False

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

            result = subprocess.run(cmd, capture_output=True, text=True)

            if result.returncode == 0:
                info = json.loads(result.stdout)

                # Извлекаем информацию
                duration = 0.0
                if 'format' in info and 'duration' in info['format']:
                    duration = float(info['format']['duration'])
                elif 'streams' in info and len(info['streams']) > 0:
                    if 'duration' in info['streams'][0]:
                        duration = float(info['streams'][0]['duration'])

                return {
                    'duration': duration,
                    'width': info.get('streams', [{}])[0].get('width', 1920),
                    'height': info.get('streams', [{}])[0].get('height', 1080),
                    'fps': 30  # По умолчанию
                }

            return None

        except Exception as e:
            logger.error(f"❌ Ошибка получения информации о видео: {e}")
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
        """Обновленный метод с поддержкой видео"""
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

                # ПОКАЗ ВИДЕО-ИНТРО (если включено)
                if self.show_video_intros and self.video_generator:
                    # Создаем видео-интро для агента
                    intro_message = f"Сейчас выступает: {agent.name}"

                    # Генерируем видео-интро
                    intro_video = self.video_generator.create_agent_intro_video(
                        agent_name=agent.name,
                        expertise=agent.expertise,
                        avatar_color=agent.color,
                        message=intro_message,
                        duration=5.0  # 5 секунд
                    )

                    if intro_video:
                        # Показываем видео перед началом речи агента
                        socketio.emit('video_start', {
                            'agent_id': agent.id,
                            'video_type': 'intro',
                            'duration': 5.0
                        })

                        # Отправляем видео в стрим
                        success = self.video_generator.add_video_to_stream(intro_video)

                        if success:
                            await asyncio.sleep(5.0)  # Ждем завершения видео

                    socketio.emit('video_end', {'agent_id': agent.id})

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

                # Генерация аудио файла
                logger.info(f"🔊 Генерация TTS для {agent.name}...")

                audio_file = await self.tts_manager.generate_audio_only(
                    text=message,
                    voice_id=agent.voice,
                    agent_name=agent.name
                )

                if audio_file and self.ffmpeg_manager:
                    # Добавляем аудио в очередь стрима
                    success = self.ffmpeg_manager.add_audio_to_queue(audio_file)

                    if success:
                        # Получаем длительность аудио
                        audio_duration = self.tts_manager._get_audio_duration(audio_file)
                        logger.info(f"⏱️  Ожидание завершения аудио: {audio_duration:.1f} сек")

                        # Ждем пока аудио должно воспроизвестись
                        await asyncio.sleep(audio_duration + 0.5)  # Небольшой буфер
                    else:
                        logger.warning(f"⚠️ Не удалось добавить аудио в очередь")
                        # Ждем по количеству слов
                        word_count = len(message.split())
                        pause_duration = max(3, min(word_count * 0.3, 10))
                        await asyncio.sleep(pause_duration)
                else:
                    # Если аудио не сгенерировалось, ждем по количеству слов
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

                    # Создаем короткое переходное видео между агентами
                    if self.show_video_intros and self.video_generator:
                        next_agent = speaking_order[speaking_order.index(agent) + 1]
                        transition_message = f"Далее: {next_agent.name}"

                        # Можно добавить короткое переходное видео
                        # transition_video = self.video_generator.create_transition_video(...)

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
                old_topic = self.current_topic
                self.select_topic()

                # Показываем видео-переход при смене темы
                if self.show_video_intros and self.video_generator:
                    transition_video = self.video_generator.create_transition_video(
                        from_topic=old_topic,
                        to_topic=self.current_topic,
                        duration=5.0
                    )

                    if transition_video:
                        socketio.emit('topic_change_video', {
                            'old_topic': old_topic,
                            'new_topic': self.current_topic,
                            'duration': 5.0
                        })

                        success = self.video_generator.add_video_to_stream(transition_video)

                        if success:
                            await asyncio.sleep(5.0)

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


@app.route('/api/stream_health')
def get_stream_health():
    """Получение здоровья стрима"""
    return jsonify(ffmpeg_manager.get_stream_health())


@app.route('/api/change_topic', methods=['POST'])
def api_change_topic():
    """Смена темы"""
    topic = stream_manager.select_topic()
    return jsonify({'success': True, 'topic': topic})


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