#!/usr/bin/env python3
"""
🤖 AI Stream с FFmpeg стримингом на YouTube
Версия для Docker без звука хоста
Зависимости: flask==2.3.0, flask-socketio==5.3.0, openai>=1.3.0
"""

import os
import sys
import json
import random
import asyncio
import threading
import logging
from gevent import monkey
monkey.patch_all()
import signal
import subprocess
import tempfile
import hashlib
from datetime import datetime
from pathlib import Path
from typing import List, Dict, Any, Optional
from flask import Flask, render_template, request, jsonify
from flask_socketio import SocketIO, emit

# Проверяем импорты
try:
    import openai
    import edge_tts
    import pygame
    from config import Config

    print("✅ Все зависимости установлены")
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
                   async_mode='threading',  # ← ИЗМЕНИЛИ
                   logger=True,
                   engineio_logger=False,
                   ping_timeout=300,
                   ping_interval=60,
                   max_http_buffer_size=1e8)

# Инициализация OpenAI (версия >=1.3.0)
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
        self.audio_queue = []
        self.ffmpeg_pid = None
        self.video_source = "black"  # или "http", "x11grab"

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
        """Запуск FFmpeg стрима"""
        if not self.stream_key:
            logger.error("❌ Stream Key не установлен!")
            return False

        try:
            # Базовые параметры видео
            if self.video_source == "http":
                # Захват с HTTP потока (Flask сервера)
                video_input = [
                    '-f', 'image2pipe',
                    '-i', 'http://localhost:5000/video_feed',
                    '-framerate', '30'
                ]
            elif self.video_source == "x11grab":
                # Захват виртуального дисплея
                video_input = [
                    '-f', 'x11grab',
                    '-i', ':99',
                    '-video_size', '1920x1080',
                    '-framerate', '30'
                ]
            else:
                # Черный экран с текстом
                video_input = [
                    '-f', 'lavfi',
                    '-i',
                    f'color=c=black:s=1920x1080:r=30:drawtext=text="AI\\ Stream":fontcolor=white:fontsize=48:x=(w-text_w)/2:y=(h-text_h)/2'
                ]

            # Параметры аудио
            if use_audio:
                # Аудио из очереди файлов
                audio_input = [
                    '-f', 'concat',
                    '-safe', '0',
                    '-i', '/tmp/audio_list.txt',
                    '-c:a', 'aac',
                    '-b:a', '128k',
                    '-ar', '44100'
                ]
            else:
                # Тихий аудио
                audio_input = [
                    '-f', 'lavfi',
                    '-i', 'anullsrc=channel_layout=stereo:sample_rate=44100',
                    '-c:a', 'aac',
                    '-b:a', '128k',
                    '-ar', '44100'
                ]

            # Сборка команды FFmpeg
            ffmpeg_cmd = ['ffmpeg']
            ffmpeg_cmd.extend(['-re'])  # Реальное время
            ffmpeg_cmd.extend(video_input)
            ffmpeg_cmd.extend(audio_input)
            ffmpeg_cmd.extend([
                '-c:v', 'libx264',
                '-preset', 'veryfast',
                '-tune', 'zerolatency',
                '-pix_fmt', 'yuv420p',
                '-g', '60',
                '-b:v', '4500k',
                '-maxrate', '4500k',
                '-bufsize', '9000k',
                '-f', 'flv',
                self.rtmp_url
            ])

            logger.info(f"🚀 Запуск FFmpeg: {' '.join(ffmpeg_cmd[:10])}...")

            # Запускаем FFmpeg
            self.stream_process = subprocess.Popen(
                ffmpeg_cmd,
                stdin=subprocess.PIPE if self.video_source == 'http' else None,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=False
            )

            self.is_streaming = True
            self.ffmpeg_pid = self.stream_process.pid

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
            # Читаем stderr для логов
            for line in iter(self.stream_process.stderr.readline, b''):
                line = line.decode('utf-8', errors='ignore').strip()
                if 'frame=' in line and 'fps=' in line:
                    logger.debug(f"FFmpeg: {line}")
                elif 'error' in line.lower():
                    logger.error(f"FFmpeg error: {line}")

            self.stream_process.wait()

        except Exception as e:
            logger.error(f"Ошибка мониторинга FFmpeg: {e}")
        finally:
            self.is_streaming = False

    def add_audio_file(self, audio_file: str):
        """Добавление аудио файла в стрим"""
        if not os.path.exists(audio_file):
            logger.error(f"❌ Аудио файл не найден: {audio_file}")
            return False

        try:
            # Добавляем в список для конкатенации
            with open('/tmp/audio_list.txt', 'a') as f:
                f.write(f"file '{audio_file}'\n")
                f.write(f"duration {self._get_audio_duration(audio_file)}\n")

            logger.info(f"🎵 Аудио добавлено: {os.path.basename(audio_file)}")
            return True

        except Exception as e:
            logger.error(f"❌ Ошибка добавления аудио: {e}")
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
            return 3.0  # Примерная длительность по умолчанию

    def stop_stream(self):
        """Остановка стрима"""
        if self.stream_process:
            logger.info("🛑 Остановка FFmpeg стрима...")
            self.is_streaming = False

            try:
                # Отправляем SIGTERM
                self.stream_process.terminate()

                # Ждем завершения
                for _ in range(10):
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

        return True  # Уже остановлен

    def get_status(self):
        """Получение статуса"""
        return {
            'is_streaming': self.is_streaming,
            'stream_key': self.stream_key[:10] + '...' if self.stream_key else None,
            'rtmp_url': self.rtmp_url,
            'pid': self.ffmpeg_pid,
            'audio_queue': len(self.audio_queue),
            'video_source': self.video_source
        }


# ========== EDGE TTS MANAGER ==========

class EdgeTTSManager:
    """Менеджер TTS для генерации аудио файлов"""

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

        logger.info("Edge TTS Manager инициализирован")

    async def text_to_speech(self, text: str, voice_id: str = 'male_ru', agent_name: str = "") -> Optional[str]:
        """Генерация аудио файла"""
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
                self._add_to_stream(cache_file)
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

            # Добавляем в FFmpeg стрим
            self._add_to_stream(cache_file)

            return cache_file

        except Exception as e:
            logger.error(f"❌ Ошибка Edge TTS: {e}", exc_info=True)
            return None

    def _add_to_stream(self, audio_file: str):
        """Добавление аудио в FFmpeg стрим"""
        if self.ffmpeg_manager and self.ffmpeg_manager.is_streaming:
            self.ffmpeg_manager.add_audio_file(audio_file)


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

            # Вызов OpenAI API (версия >=1.3.0)
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
        """Запуск раунда дискуссии"""
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

                # Генерация ответа
                logger.info(f"🤖 {agent.name} генерирует ответ...")
                message = await agent.generate_response(
                    self.current_topic,
                    self.conversation_history
                )

                # Сохраняем в историю
                self.conversation_history.append(f"{agent.name}: {message}")
                self.message_count += 1

                # Отправляем сообщение
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

                # Генерация и добавление аудио
                audio_task = asyncio.create_task(
                    self.tts_manager.text_to_speech(message, agent.voice, agent.name)
                )

                # Пауза на "произнесение" сообщения
                word_count = len(message.split())
                pause_duration = max(3, min(word_count * 0.3, 10))
                await asyncio.sleep(pause_duration)

                # Ждем завершения генерации аудио
                await audio_task

                # Агент заканчивает говорить
                socketio.emit('agent_stop_speaking', {'agent_id': agent.id})
                self.active_agent = None

                # Пауза между агентами
                if agent != speaking_order[-1]:
                    pause = random.uniform(1.5, 3.0)
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


# ========== ГЛОБАЛЬНЫЕ ОБЪЕКТЫ ==========

ffmpeg_manager = FFmpegStreamManager()
stream_manager = AIStreamManager(ffmpeg_manager)


# ========== АСИНХРОННЫЙ ЦИКЛ ==========

async def discussion_loop():
    """Основной цикл дискуссии"""
    await asyncio.sleep(2)  # Пауза для запуска сервера
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

    try:
        loop.run_until_complete(discussion_loop())
    finally:
        loop.close()


# ========== FLASK РОУТЫ ==========

@app.route('/')
def index():
    """Главная страница"""
    return render_template('index.html',
                           agents=stream_manager.get_agents_state(),
                           topic=stream_manager.current_topic or "Загрузка темы...",
                           stats=stream_manager.get_stats())


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


@app.route('/api/start_stream', methods=['POST'])
def start_stream():
    """Запуск FFmpeg стрима (принимает разные форматы)"""
    try:
        # Принимаем JSON разными способами
        if request.is_json:
            data = request.get_json()
        elif request.content_type == 'application/x-www-form-urlencoded':
            # Для form-data
            data = {
                'stream_key': request.form.get('stream_key'),
                'video_source': request.form.get('video_source', 'black'),
                'use_audio': request.form.get('use_audio', 'true').lower() == 'true'
            }
        elif request.content_type.startswith('multipart/form-data'):
            # Для multipart
            data = {
                'stream_key': request.form.get('stream_key'),
                'video_source': request.form.get('video_source', 'black'),
                'use_audio': request.form.get('use_audio', 'true').lower() == 'true'
            }
        else:
            # Пытаемся парсить raw данные
            try:
                data = json.loads(request.data.decode('utf-8'))
            except:
                return jsonify({
                    'status': 'error',
                    'message': 'Content-Type должен быть application/json или передайте JSON в теле запроса'
                }), 415

        # Проверяем обязательные поля
        stream_key = data.get('stream_key', '')
        if not stream_key:
            return jsonify({
                'status': 'error',
                'message': 'Stream Key обязателен'
            }), 400

        # Логируем полученные данные
        logger.info(f"📨 Получен запрос на запуск стрима: {data}")

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


@app.route('/api/stop_stream', methods=['POST'])
def stop_stream():
    """Остановка стрима"""
    try:
        if ffmpeg_manager.stop_stream():
            return jsonify({'status': 'stopped'})
        return jsonify({'status': 'error', 'message': 'Стрим не был запущен'})
    except Exception as e:
        logger.error(f"Ошибка остановки стрима: {e}")
        return jsonify({'status': 'error', 'message': str(e)})


@app.route('/api/stream_status')
def stream_status():
    """Статус стрима"""
    return jsonify(ffmpeg_manager.get_status())


@app.route('/api/stream_stats')
def stream_stats():
    """Статистика стрима"""
    stats = stream_manager.get_stats()
    stats.update(ffmpeg_manager.get_status())
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
        'server_time': datetime.now().isoformat()
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
        'stream_status': ffmpeg_manager.get_status()
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
    print("🤖 AI AGENTS STREAM WITH FFMPEG")
    print("=" * 70)

    # Информация о зависимостях
    print(f"📦 Версии зависимостей:")
    print(f"   Flask: 2.3.0")
    print(f"   Flask-SocketIO: 5.3.0")
    print(f"   OpenAI: >=1.3.0")
    print(f"   Edge TTS: >=6.1.9")
    print(f"   FFmpeg: системный")

    # Создаем директории
    os.makedirs("stream_ui", exist_ok=True)
    os.makedirs("audio_cache", exist_ok=True)

    # Очищаем старые аудио файлы
    import shutil

    if os.path.exists('audio_cache'):
        # Безопасное удаление только файлов внутри директории
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

    # Очищаем список аудио
    if os.path.exists('/tmp/audio_list.txt'):
        os.remove('/tmp/audio_list.txt')

    # Создаем простой UI если его нет
    ui_path = "stream_ui/index.html"
    if not os.path.exists(ui_path):
        print("📁 Создаю простой UI...")
        os.makedirs("stream_ui", exist_ok=True)
        with open(ui_path, 'w', encoding='utf-8') as f:
            f.write('''<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <title>AI Stream Control</title>
    <style>
        body { font-family: Arial; padding: 20px; }
        .status { padding: 10px; margin: 10px 0; border-radius: 5px; }
        .online { background: #d4edda; }
        .offline { background: #f8d7da; }
        button { margin: 5px; padding: 10px 20px; }
    </style>
</head>
<body>
    <h1>🤖 AI Stream Control Panel</h1>
    <div id="status" class="status offline">Status: Loading...</div>
    <button onclick="startStream()">Start YouTube Stream</button>
    <button onclick="stopStream()">Stop Stream</button>
    <script src="https://cdn.socket.io/4.5.4/socket.io.min.js"></script>
    <script>
        const socket = io();
        socket.on('connected', updateStatus);
        socket.on('update', updateStatus);

        function updateStatus(data) {
            document.getElementById('status').innerHTML = 
                `Agents: ${data.agents.length}, Topic: ${data.topic}`;
        }

        function startStream() {
            const key = prompt('Enter YouTube Stream Key:');
            if(key) fetch('/api/start_stream', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({stream_key: key})
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
    print("🌐 Веб-интерфейс: http://localhost:5000")
    print("🔧 API Endpoints:")
    print("   GET  /health                - Проверка здоровья")
    print("   POST /api/start_stream      - Запуск стрима")
    print("   GET  /api/stream_status     - Статус стрима")
    print("")
    print("📝 Для запуска стрима:")
    print("   1. Получите Stream Key в YouTube Studio")
    print("   2. Откройте http://localhost:5000")
    print("   3. Нажмите 'Start YouTube Stream'")
    print("   4. Введите Stream Key")
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