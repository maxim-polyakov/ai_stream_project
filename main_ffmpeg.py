#!/usr/bin/env python3
"""
🤖 AI Stream с FFmpeg стримингом на YouTube
Версия БЕЗ YouTube API - только прямой стрим через stream key
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
    """Управление FFmpeg стримом на YouTube через stream key"""

    def __init__(self):
        self.stream_process = None
        self.is_streaming = False
        self.stream_key = None
        self.rtmp_url = None
        self.ffmpeg_pid = None
        self.video_source = "black"
        self.ffmpeg_stdin = None
        self.start_time = None
        self.audio_queue = []  # Очередь аудио файлов для воспроизведения
        self.is_playing_audio = False

        # Настройки аудио
        self.audio_sample_rate = 44100
        self.audio_channels = 2

        # Флаг для определения, используем ли мы PyAudio
        self.use_pyaudio = PYTHON_AUDIO_AVAILABLE

        logger.info("FFmpeg Stream Manager инициализирован")

    def set_stream_key(self, stream_key: str) -> bool:
        """Установка ключа стрима"""
        self.stream_key = stream_key
        self.rtmp_url = f"rtmp://a.rtmp.youtube.com/live2/{stream_key}"
        logger.info(f"🔑 Stream Key установлен: {stream_key[:10]}...")
        logger.info(f"📍 RTMP URL: {self.rtmp_url}")
        return True

    def stream_audio_pipe(self, audio_file: str) -> bool:
        """Более надежный метод отправки аудио через конвертацию в WAV"""
        if not self.is_streaming or not self.ffmpeg_stdin:
            logger.error("❌ Стрим не активен или stdin недоступен")
            return False

        temp_wav = None

        try:
            # 1. Сначала конвертируем MP3 в WAV файл
            temp_wav = tempfile.NamedTemporaryFile(suffix='.wav', delete=False)
            temp_wav.close()

            # Конвертация MP3 -> WAV
            convert_cmd = [
                'ffmpeg',
                '-i', audio_file,
                '-acodec', 'pcm_s16le',
                '-ar', '44100',
                '-ac', '2',
                '-y',
                temp_wav.name
            ]

            logger.info(f"🔄 Конвертация MP3 в WAV: {os.path.basename(audio_file)}")

            result = subprocess.run(convert_cmd, capture_output=True, text=True, timeout=30)

            if result.returncode != 0:
                logger.error(f"❌ Ошибка конвертации в WAV: {result.stderr[:500]}")
                if os.path.exists(temp_wav.name):
                    os.unlink(temp_wav.name)
                return False

            # 2. Проверяем созданный WAV файл
            if not os.path.exists(temp_wav.name) or os.path.getsize(temp_wav.name) < 100:
                logger.error(f"❌ WAV файл не создан или слишком маленький")
                if os.path.exists(temp_wav.name):
                    os.unlink(temp_wav.name)
                return False

            # 3. Подготавливаем PCM данные для отправки
            wav_file_size = os.path.getsize(temp_wav.name)

            # Определяем реальный размер заголовка WAV
            with open(temp_wav.name, 'rb') as f:
                # Читаем первые 44 байта для анализа
                header = f.read(44)
                if len(header) < 44:
                    logger.error("❌ WAV файл поврежден, заголовок слишком короткий")
                    return False

                # Проверяем сигнатуру WAV файла
                if header[0:4] != b'RIFF' or header[8:12] != b'WAVE':
                    logger.error("❌ Неверный формат WAV файла")
                    return False

            # 4. Отправляем данные с правильным форматом
            # ВАЖНО: FFmpeg должен быть запущен с правильными параметрами для приема PCM
            logger.info(f"📤 Отправка PCM аудио: {os.path.basename(audio_file)} ({wav_file_size} байт)")

            # Отправляем WAV файл целиком (включая заголовок)
            # FFmpeg сам разберет формат, если он правильно настроен
            chunk_size = 65536  # 64KB чанки для лучшей производительности
            total_sent = 0
            bytes_per_second = 44100 * 2 * 2  # 44100 Hz * 2 channels * 2 bytes per sample
            chunk_duration_ms = (chunk_size / bytes_per_second) * 1000

            logger.debug(f"Настройки: {chunk_size} байт/чанк, ~{chunk_duration_ms:.0f} мс")

            with open(temp_wav.name, 'rb') as wav_file:
                while True:
                    # Читаем данные
                    audio_data = wav_file.read(chunk_size)
                    if not audio_data:
                        break

                    # Проверяем статус основного процесса
                    if not self.is_streaming:
                        logger.warning("⚠️ Стрим остановлен во время отправки аудио")
                        break

                    # Отправляем данные
                    try:
                        self.ffmpeg_stdin.write(audio_data)
                        self.ffmpeg_stdin.flush()
                        total_sent += len(audio_data)

                        # Небольшая задержка для синхронизации с реальным временем
                        # Это предотвращает переполнение буфера FFmpeg
                        if len(audio_data) >= chunk_size:
                            time.sleep(chunk_duration_ms / 1000 * 0.9)  # 90% от реального времени

                    except BrokenPipeError:
                        logger.error("❌ Broken pipe: основной FFmpeg процесс завершился")
                        self.is_streaming = False
                        break
                    except OSError as e:
                        logger.error(f"❌ Ошибка записи аудио (OSError): {e}")
                        self.is_streaming = False
                        break
                    except Exception as e:
                        logger.error(f"❌ Ошибка записи аудио: {e}")
                        break

            logger.info(f"✅ Аудио отправлено ({total_sent} байт из {wav_file_size})")

            # 5. После отправки всех данных, отправляем сигнал завершения если нужно
            if self.is_streaming and total_sent > 0:
                try:
                    # Немного ждем перед закрытием
                    time.sleep(0.5)

                    # Проверяем, жив ли еще процесс
                    if hasattr(self, 'ffmpeg_process') and self.ffmpeg_process:
                        if self.ffmpeg_process.poll() is None:
                            # Процесс еще работает, можно отправить flush
                            self.ffmpeg_stdin.flush()
                except:
                    pass

            return total_sent > 0

        except subprocess.TimeoutExpired:
            logger.error("❌ Таймаут конвертации аудио")
            return False

        except Exception as e:
            logger.error(f"❌ Ошибка stream_audio_pipe: {e}", exc_info=True)
            return False

        finally:
            # Всегда очищаем временный файл
            if temp_wav and os.path.exists(temp_wav.name):
                try:
                    os.unlink(temp_wav.name)
                    logger.debug("🧹 Временный WAV файл удален")
                except Exception as e:
                    logger.warning(f"⚠️ Не удалось удалить временный файл: {e}")

    def _audio_queue_processor(self):
        """Обработчик очереди аудио файлов"""
        while self.is_streaming:
            try:
                if self.audio_queue:
                    # Берем следующий файл из очереди
                    audio_file = self.audio_queue.pop(0)
                    logger.info(f"🎵 Воспроизведение аудио из очереди: {os.path.basename(audio_file)}")

                    # Отправляем аудио
                    success = self.stream_audio_pipe(audio_file)

                    if not success:
                        logger.warning(f"⚠️ Не удалось воспроизвести аудио, пропускаем")

                    # Удаляем временный файл если он был создан TTS
                    if audio_file.startswith(tempfile.gettempdir()):
                        try:
                            os.unlink(audio_file)
                        except:
                            pass
                else:
                    # Если очередь пуста, ждем
                    time.sleep(0.1)

            except Exception as e:
                logger.error(f"❌ Ошибка в обработчике аудио: {e}")
                time.sleep(0.1)

    def add_audio_to_queue(self, audio_file: str) -> bool:
        """Добавление аудио файла в очередь на воспроизведение"""
        if not os.path.exists(audio_file):
            logger.error(f"❌ Аудио файл не найден: {audio_file}")
            return False

        self.audio_queue.append(audio_file)
        logger.info(f"📥 Аудио добавлено в очередь: {os.path.basename(audio_file)}")
        logger.info(f"📊 Размер очереди: {len(self.audio_queue)} файлов")
        return True

    def start_stream(self, use_audio: bool = True):
        """Запуск FFmpeg стрима с поддержкой аудио через stdin"""
        if not self.stream_key:
            logger.error("❌ Stream Key не установлен!")
            return {'success': False, 'error': 'Stream Key не установлен'}

        try:
            self.start_time = time.time()

            # Инициализируем переменные для аудио
            self.audio_queue = []  # Очередь аудио файлов
            self.current_audio = None
            self.ffmpeg_stdin = None

            # Команда FFmpeg с поддержкой аудио через stdin
            ffmpeg_cmd = [
                'ffmpeg',
                '-re',

                # Видео источник
                '-f', 'lavfi',
                '-i', 'color=size=1920x1080:rate=30:color=black',

                # Аудио источник через stdin (сырые PCM данные)
                '-f', 's16le',  # Формат: 16-bit signed little-endian PCM
                '-ar', '44100',  # Частота дискретизации: 44.1 kHz
                '-ac', '2',  # Стерео
                '-channel_layout', 'stereo',
                '-i', 'pipe:0',  # Читать из stdin

                # Видео кодек
                '-c:v', 'libx264',
                '-preset', 'veryfast',
                '-tune', 'zerolatency',
                '-pix_fmt', 'yuv420p',
                '-g', '60',
                '-b:v', '4500k',
                '-maxrate', '4500k',
                '-bufsize', '9000k',
                '-r', '30',  # Частота кадров

                # Аудио кодек
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

            logger.info(f"🚀 Запуск FFmpeg стрима на YouTube")
            logger.info(f"🔑 Stream Key: {self.stream_key[:10]}...")
            logger.info(f"📍 RTMP URL: {self.rtmp_url}")

            # Запускаем FFmpeg
            self.stream_process = subprocess.Popen(
                ffmpeg_cmd,
                stdin=subprocess.PIPE,  # КРИТИЧЕСКИ ВАЖНО!
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                bufsize=0,  # Небуферизованный ввод/вывод
                text=False  # Бинарный режим
            )

            self.is_streaming = True
            self.ffmpeg_pid = self.stream_process.pid
            self.ffmpeg_stdin = self.stream_process.stdin  # ВАЖНО: сохраняем stdin

            logger.info(f"✅ FFmpeg запущен (PID: {self.ffmpeg_pid})")

            # Запускаем мониторинг
            threading.Thread(target=self._monitor_ffmpeg, daemon=True).start()

            # Запускаем обработчик аудио очереди
            threading.Thread(target=self._audio_queue_processor, daemon=True).start()

            logger.info(f"🎬 FFmpeg стрим запущен (PID: {self.ffmpeg_pid})")
            socketio.emit('stream_started', {
                'pid': self.ffmpeg_pid,
                'rtmp_url': self.rtmp_url,
                'stream_key': self.stream_key[:10] + '...'
            })

            return {'success': True, 'pid': self.ffmpeg_pid, 'message': 'Стрим запущен'}

        except Exception as e:
            logger.error(f"❌ Ошибка запуска FFmpeg: {e}", exc_info=True)
            return {'success': False, 'error': str(e)}

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
            socketio.emit('stream_stopped', {'time': datetime.now().isoformat()})

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


# ========== EDGE TTS MANAGER ==========

class EdgeTTSManager:
    """Менеджер TTS для генерации аудио и передачи в стрим"""

    def __init__(self, ffmpeg_manager: FFmpegStreamManager = None):
        self.cache_dir = 'audio_cache'
        os.makedirs(self.cache_dir, exist_ok=True)
        self.ffmpeg_manager = ffmpeg_manager

        self.voice_map = {
            'male_ru': 'ru-RU-DmitryNeural',
            'male_ru_deep': 'ru-RU-EkaterinaRUS',
            'female_ru': 'ru-RU-SvetlanaNeural',
            'female_ru_soft': 'ru-RU-MishaNeural'
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