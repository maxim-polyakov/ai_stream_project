#!/usr/bin/env python3
"""
🤖 YouTube Live Stream с AI агентами на OpenAI GPT-4
Автономные агенты ведут научные дебаты в реальном времени
С ПОДДЕРЖКОЙ СТРИМА НА YOUTUBЕ
"""

import os
import sys
import json
import random
import asyncio
import threading
import logging
import signal
from datetime import datetime
from typing import List, Dict, Any, Optional

# Проверяем импорты
try:
    import openai
    from flask import Flask, render_template, send_from_directory
    from flask_socketio import SocketIO, emit
    import pygame
    import edge_tts
    from config import Config

    # Для YouTube стриминга
    try:
        import googleapiclient.discovery
        from google.oauth2.credentials import Credentials
        from google.auth.transport.requests import Request
        from google_auth_oauthlib.flow import InstalledAppFlow

        YOUTUBE_ENABLED = True
    except ImportError:
        YOUTUBE_ENABLED = False
        print("⚠️ YouTube API не установлен. Установите: pip install google-api-python-client google-auth-oauthlib")

    print("✅ Все зависимости установлены")
except ImportError as e:
    print(f"❌ Ошибка импорта: {e}")
    print("\n📦 Установите зависимости:")
    print("pip install openai flask flask-socketio eventlet edge-tts pygame python-dotenv")
    print("Для YouTube: pip install google-api-python-client google-auth-oauthlib google-auth-httplib2")
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
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='eventlet')

# Инициализация OpenAI
if Config.OPENAI_API_KEY:
    openai.api_key = Config.OPENAI_API_KEY
else:
    logger.warning("⚠️ OpenAI API ключ не найден. Будут использоваться демо-сообщения.")

# Инициализация аудио
pygame.mixer.init(frequency=44100, size=-16, channels=2, buffer=2048)


# ========== YOUTUBЕ STREAM MANAGER ==========

class YouTubeStreamManager:
    """Менеджер для запуска стрима на YouTube"""

    SCOPES = ["https://www.googleapis.com/auth/youtube.force-ssl"]

    def __init__(self):
        self.youtube = None
        self.broadcast_id = None
        self.stream_id = None
        self.is_live = False
        self.stream_key = None
        self.rtmp_url = None

    def authenticate(self):
        """Аутентификация в YouTube API"""
        try:
            creds = None

            if os.path.exists('token.json'):
                creds = Credentials.from_authorized_user_file('token.json', self.SCOPES)

            if not creds or not creds.valid:
                if creds and creds.expired and creds.refresh_token:
                    creds.refresh(Request())
                else:
                    if not os.path.exists('client_secret.json'):
                        logger.error("❌ Файл client_secret.json не найден!")
                        print("\n📝 Создайте файл client_secret.json:")
                        print("1. Перейдите: https://console.cloud.google.com/")
                        print("2. Создайте проект и включите YouTube Data API v3")
                        print("3. Создайте OAuth 2.0 Client ID (Desktop app)")
                        print("4. Скачайте JSON и сохраните как client_secret.json")
                        return False

                    flow = InstalledAppFlow.from_client_secrets_file(
                        'client_secret.json', self.SCOPES)
                    creds = flow.run_local_server(port=8080)

                with open('token.json', 'w') as token:
                    token.write(creds.to_json())

            self.youtube = googleapiclient.discovery.build(
                'youtube', 'v3', credentials=creds)

            logger.info("✅ YouTube API аутентифицирован")
            return True

        except Exception as e:
            logger.error(f"❌ Ошибка аутентификации YouTube: {e}")
            return False

    def create_stream(self, title: str, description: str = ""):
        """Создание стрима на YouTube"""
        try:
            # Создаем трансляцию
            broadcast_request = self.youtube.liveBroadcasts().insert(
                part="snippet,status,contentDetails",
                body={
                    "snippet": {
                        "title": title,
                        "description": description,
                        "scheduledStartTime": datetime.now().isoformat()
                    },
                    "status": {
                        "privacyStatus": "unlisted",  # public, unlisted, private
                        "selfDeclaredMadeForKids": False
                    },
                    "contentDetails": {
                        "enableAutoStart": True,
                        "enableAutoStop": True,
                        "enableEmbed": True,
                        "recordFromStart": True,
                        "enableDvr": True,
                        "enableContentEncryption": False,
                        "enableLowLatency": True
                    }
                }
            )

            self.broadcast = broadcast_request.execute()
            self.broadcast_id = self.broadcast['id']

            logger.info(f"📡 YouTube трансляция создана: {self.broadcast_id}")

            # Создаем поток
            stream_request = self.youtube.liveStreams().insert(
                part="snippet,cdn",
                body={
                    "snippet": {
                        "title": f"Stream for {title}"
                    },
                    "cdn": {
                        "frameRate": "30fps",
                        "ingestionType": "rtmp",
                        "resolution": "1080p"
                    }
                }
            )

            self.stream = stream_request.execute()
            self.stream_id = self.stream['id']

            # Получаем ключ потока
            ingestion_info = self.stream['cdn']['ingestionInfo']
            self.stream_key = ingestion_info['streamName']
            self.rtmp_url = f"rtmp://a.rtmp.youtube.com/live2/{self.stream_key}"

            logger.info(f"🌊 YouTube поток создан: {self.stream_id}")
            logger.info(f"🔑 Stream Key: {self.stream_key}")
            logger.info(f"📍 RTMP URL: {self.rtmp_url}")

            # Связываем трансляцию с потоком
            bind_request = self.youtube.liveBroadcasts().bind(
                part="id,contentDetails",
                id=self.broadcast_id,
                streamId=self.stream_id
            )

            bind_request.execute()

            # Переводим в статус live
            transition_request = self.youtube.liveBroadcasts().transition(
                broadcastStatus="live",
                id=self.broadcast_id,
                part="id,status"
            )

            transition_request.execute()
            self.is_live = True

            logger.info("🎬 YouTube стрим запущен!")
            logger.info(f"📺 Ссылка: https://youtube.com/watch?v={self.broadcast_id}")

            return True

        except Exception as e:
            logger.error(f"❌ Ошибка создания стрима: {e}")
            return False

    def get_stream_info(self):
        """Получение информации о стриме"""
        return {
            'stream_key': self.stream_key,
            'rtmp_url': self.rtmp_url,
            'broadcast_id': self.broadcast_id,
            'watch_url': f"https://youtube.com/watch?v={self.broadcast_id}" if self.broadcast_id else None,
            'is_live': self.is_live
        }

    def stop_stream(self):
        """Остановка стрима"""
        try:
            if self.broadcast_id and self.is_live:
                request = self.youtube.liveBroadcasts().transition(
                    broadcastStatus="complete",
                    id=self.broadcast_id,
                    part="id,status"
                )

                request.execute()
                self.is_live = False
                logger.info("🛑 YouTube стрим остановлен")

        except Exception as e:
            logger.error(f"❌ Ошибка остановки стрима: {e}")


# ========== EDGE TTS MANAGER ==========

class EdgeTTSManager:
    """Менеджер TTS с мужскими голосами через Edge TTS (Microsoft)"""

    def __init__(self):
        self.cache_dir = 'audio_cache'
        os.makedirs(self.cache_dir, exist_ok=True)

        self.voice_map = {
            'male_ru': 'ru-RU-DmitryNeural',
            'male_ru_deep': 'ru-RU-DmitryNeural',
            'female_ru': 'ru-RU-SvetlanaNeural',
            'female_ru_soft': 'ru-RU-DariyaNeural'
        }

        logger.info("Edge TTS Manager инициализирован")

    async def text_to_speech(self, text: str, voice_id: str = 'male_ru') -> Optional[str]:
        """Преобразование текста в речь через Edge TTS"""
        try:
            if voice_id not in self.voice_map:
                voice_id = 'male_ru'

            voice_name = self.voice_map[voice_id]

            import hashlib
            text_hash = hashlib.md5(f"{text}_{voice_id}".encode()).hexdigest()
            cache_file = os.path.join(self.cache_dir, f"{text_hash}.mp3")

            if os.path.exists(cache_file):
                return cache_file

            rate = '+0%'
            pitch = '+0Hz'

            if voice_id == 'male_ru_deep':
                rate = '-10%'
                pitch = '-20Hz'

            communicate = edge_tts.Communicate(
                text=text,
                voice=voice_name,
                rate=rate,
                pitch=pitch
            )

            await communicate.save(cache_file)
            return cache_file

        except Exception as e:
            logger.error(f"Ошибка Edge TTS: {e}")
            return None

    async def speak(self, text: str, voice_id: str = 'male_ru') -> bool:
        """Озвучивание текста"""
        try:
            audio_file = await self.text_to_speech(text, voice_id)

            if not audio_file:
                return False

            pygame.mixer.music.load(audio_file)
            pygame.mixer.music.play()

            while pygame.mixer.music.get_busy():
                await asyncio.sleep(0.1)

            return True

        except Exception as e:
            logger.error(f"Ошибка воспроизведения: {e}")
            return False


# ========== AI AGENT ==========

class AIAgent:
    """AI агент с уникальной личностью и экспертизой"""

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
        """Генерация уникального ответа через OpenAI GPT-4"""

        if not Config.OPENAI_API_KEY:
            demo_responses = [
                f"Как эксперт в {self.expertise.lower()}, я считаю, что {topic.lower()} - важная тема для обсуждения.",
                f"С точки зрения {self.expertise.lower()}, можно выделить несколько ключевых аспектов этой проблемы.",
                f"Мои исследования в области {self.expertise.lower()} показывают интересные перспективы по этой теме.",
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
                for msg in conversation_history[-2:]:
                    user_prompt += f"- {msg}\n"
                user_prompt += "\n"

            user_prompt += f"{self.name}, что ты думаешь по этой теме? (кратко, 2-3 предложения)"

            response = await asyncio.to_thread(
                openai.chat.completions.create,
                model=Config.OPENAI_MODEL,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt}
                ],
                temperature=0.8,
                max_tokens=200
            )

            message = response.choices[0].message.content.strip()

            if message.startswith(f"{self.name}:"):
                message = message[len(f"{self.name}:"):].strip()

            self.message_history.append(message)
            return message

        except Exception as e:
            logger.error(f"Ошибка генерации ответа для {self.name}: {e}")
            return f"Как эксперт в {self.expertise.lower()}, я считаю, что {topic.lower()} требует внимательного изучения."


# ========== AI STREAM MANAGER ==========

class AIStreamManager:
    """Менеджер стрима с AI агентами"""

    def __init__(self, youtube_manager: Optional[YouTubeStreamManager] = None):
        self.agents: List[AIAgent] = []
        self.tts_manager = EdgeTTSManager()
        self.youtube_manager = youtube_manager
        self.current_topic = ""
        self.is_discussion_active = False
        self.message_count = 0
        self.discussion_round = 0
        self.active_agent = None
        self.conversation_history = []

        self._init_agents()
        logger.info(f"AI Stream Manager инициализирован с {len(self.agents)} агентами")

    def _init_agents(self):
        """Инициализация AI агентов"""
        for agent_config in Config.AGENTS:
            agent = AIAgent(agent_config)
            self.agents.append(agent)

    def select_topic(self) -> str:
        """Выбор случайной темы для дискуссии"""
        self.current_topic = random.choice(Config.TOPICS)
        logger.info(f"Выбрана тема: {self.current_topic}")
        return self.current_topic

    async def run_discussion_round(self):
        """Запуск одного раунда дискуссии"""
        if self.is_discussion_active:
            return

        self.is_discussion_active = True
        self.discussion_round += 1

        try:
            if not self.current_topic:
                self.select_topic()

            socketio.emit('topic_update', {
                'topic': self.current_topic,
                'round': self.discussion_round
            })

            speaking_order = random.sample(self.agents, len(self.agents))

            for agent in speaking_order:
                if not self.is_discussion_active:
                    break

                self.active_agent = agent.id
                socketio.emit('agent_start_speaking', {
                    'agent_id': agent.id,
                    'agent_name': agent.name
                })

                message = await agent.generate_response(
                    self.current_topic,
                    self.conversation_history
                )

                self.conversation_history.append(f"{agent.name}: {message}")
                self.message_count += 1

                socketio.emit('new_message', {
                    'agent_id': agent.id,
                    'agent_name': agent.name,
                    'message': message,
                    'expertise': agent.expertise,
                    'avatar': agent.avatar,
                    'color': agent.color,
                    'message_count': self.message_count
                })

                logger.info(f"💬 {agent.name}: {message[:80]}...")

                await self.tts_manager.speak(message, agent.voice)
                await asyncio.sleep(1)

                socketio.emit('agent_stop_speaking', {'agent_id': agent.id})
                self.active_agent = None

                if agent != speaking_order[-1]:
                    await asyncio.sleep(random.uniform(2, 3))

            socketio.emit('round_complete', {
                'round': self.discussion_round,
                'total_messages': self.message_count,
                'next_round_in': 10
            })

            await asyncio.sleep(10)

            if random.random() > 0.6:
                self.select_topic()
                socketio.emit('topic_update', {'topic': self.current_topic})

        except Exception as e:
            logger.error(f"❌ Ошибка в раунде дискуссии: {e}", exc_info=True)

        finally:
            self.is_discussion_active = False
            self.active_agent = None

    def get_agents_state(self) -> List[Dict[str, Any]]:
        """Получение состояния всех агентов"""
        return [
            {
                'id': agent.id,
                'name': agent.name,
                'expertise': agent.expertise,
                'avatar': agent.avatar,
                'color': agent.color,
                'is_speaking': agent.id == self.active_agent
            }
            for agent in self.agents
        ]

    def get_stats(self) -> Dict[str, Any]:
        """Получение статистики стрима"""
        return {
            'message_count': self.message_count,
            'discussion_round': self.discussion_round,
            'current_topic': self.current_topic,
            'is_active': self.is_discussion_active,
            'active_agent': self.active_agent,
            'agents_count': len(self.agents),
            'youtube_live': self.youtube_manager.is_live if self.youtube_manager else False
        }


# ========== ГЛОБАЛЬНЫЕ ОБЪЕКТЫ ==========

youtube_manager = YouTubeStreamManager() if YOUTUBE_ENABLED else None
stream_manager = AIStreamManager(youtube_manager)


# ========== АСИНХРОННЫЙ ЦИКЛ ДИСКУССИИ ==========

async def discussion_loop():
    """Основной цикл дискуссии"""
    await asyncio.sleep(3)
    stream_manager.select_topic()

    while True:
        try:
            if not stream_manager.is_discussion_active:
                await stream_manager.run_discussion_round()
            await asyncio.sleep(1)
        except Exception as e:
            logger.error(f"Ошибка в основном цикле: {e}")
            await asyncio.sleep(5)


def start_discussion_loop():
    """Запуск цикла дискуссии в отдельном потоке"""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(discussion_loop())


# ========== FLASK РОУТЫ ==========

@app.route('/')
def index():
    """Главная страница стрима"""
    return render_template('index.html',
                           agents=stream_manager.get_agents_state(),
                           topic=stream_manager.current_topic or "Загрузка темы...",
                           stats=stream_manager.get_stats(),
                           youtube_enabled=YOUTUBE_ENABLED)


@app.route('/health')
def health():
    return {'status': 'ok', 'time': datetime.now().isoformat()}


@app.route('/start')
def start_stream():
    """Запуск стрима"""
    stream_manager.is_discussion_active = True
    return {'status': 'started', 'topic': stream_manager.current_topic}


@app.route('/stop')
def stop_stream():
    """Остановка стрима"""
    stream_manager.is_discussion_active = False
    return {'status': 'stopped'}


@app.route('/start_youtube')
def start_youtube_stream():
    """Запуск YouTube стрима"""
    if not YOUTUBE_ENABLED:
        return {'status': 'error', 'message': 'YouTube API не установлен'}

    success = youtube_manager.authenticate() and youtube_manager.create_stream(
        title="🤖 AI Agents Live: Научные дебаты ИИ",
        description="""Автономные ИИ-агенты обсуждают науку в реальном времени.

Участники:
• Доктор Алексей Волков - Квантовая физика
• Профессор Мария Соколова - Нейробиология
• Доктор Иван Петров - Климатология
• Исследователь София Ковалева - ИИ и робототехника

Стрим создан автоматически с помощью Python и OpenAI GPT-4."""
    )

    if success:
        stream_info = youtube_manager.get_stream_info()
        return {
            'status': 'started',
            'watch_url': stream_info['watch_url'],
            'stream_key': stream_info['stream_key'],
            'rtmp_url': stream_info['rtmp_url']
        }
    else:
        return {'status': 'error', 'message': 'Не удалось запустить YouTube стрим'}


@app.route('/stop_youtube')
def stop_youtube_stream():
    """Остановка YouTube стрима"""
    if YOUTUBE_ENABLED and youtube_manager:
        youtube_manager.stop_stream()
        return {'status': 'stopped'}
    return {'status': 'error', 'message': 'YouTube стрим не запущен'}


@app.route('/youtube_info')
def get_youtube_info():
    """Получение информации о YouTube стриме"""
    if YOUTUBE_ENABLED and youtube_manager:
        return youtube_manager.get_stream_info()
    return {'status': 'not_available'}


# ========== WEBSOCKET ==========

@socketio.on('connect')
def handle_connect():
    logger.info(f"📱 Клиент подключился")
    socketio.emit('connected', {
        'status': 'connected',
        'topic': stream_manager.current_topic or stream_manager.select_topic(),
        'agents': stream_manager.get_agents_state(),
        'stats': stream_manager.get_stats(),
        'server_time': datetime.now().isoformat()
    })


@socketio.on('disconnect')
def handle_disconnect():
    logger.info("📱 Клиент отключился")


# ========== ЗАПУСК СЕРВЕРА ==========

def signal_handler(signum, frame):
    """Обработчик сигналов для корректного завершения"""
    print("\n\n🛑 Получен сигнал завершения...")

    if YOUTUBE_ENABLED and youtube_manager and youtube_manager.is_live:
        print("⏳ Останавливаем YouTube стрим...")
        youtube_manager.stop_stream()

    print("👋 Завершение работы...")
    sys.exit(0)


if __name__ == '__main__':
    # Регистрируем обработчики сигналов
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    print("=" * 70)
    print("🤖 AI AGENTS YOUTUBE LIVE STREAM")
    print("=" * 70)
    print(f"🔑 OpenAI API: {'✅ Доступен' if Config.OPENAI_API_KEY else '⚠️ Демо-режим'}")
    print(f"🎬 YouTube стрим: {'✅ Доступен' if YOUTUBE_ENABLED else '❌ Требуется установка google-api-python-client'}")
    print(f"👥 Агентов: {len(stream_manager.agents)}")
    print(f"📚 Тем для обсуждения: {len(Config.TOPICS)}")
    print("=" * 70)

    # Создаем необходимые директории
    os.makedirs("stream_ui", exist_ok=True)
    os.makedirs("audio_cache", exist_ok=True)

    # Создаем простой UI если его нет
    if not os.path.exists("stream_ui/index.html"):
        simple_html = '''<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <title>AI Stream</title>
    <style>
        body { margin: 0; padding: 0; width: 1920px; height: 1080px; background: #0c2461; color: white; font-family: Arial; }
        .container { padding: 50px; }
        h1 { text-align: center; font-size: 48px; color: #4a69ff; }
        .topic { text-align: center; font-size: 32px; margin: 30px 0; color: #a5b4ff; }
        .agents { display: grid; grid-template-columns: 1fr 1fr; grid-template-rows: 1fr 1fr; gap: 30px; height: 700px; padding: 0 50px; }
        .agent { background: rgba(255,255,255,0.1); padding: 30px; border-radius: 20px; text-align: center; border: 2px solid rgba(255,255,255,0.2); }
        .agent.active { border-color: #4a69ff; background: rgba(74, 105, 255, 0.2); }
        .avatar { font-size: 80px; margin-bottom: 20px; }
        .name { font-size: 32px; margin-bottom: 10px; }
        .expertise { color: #a5b4ff; font-size: 24px; margin-bottom: 20px; }
        .message { font-size: 20px; color: #e0e0ff; min-height: 100px; padding: 15px; background: rgba(0,0,0,0.3); border-radius: 10px; }
    </style>
</head>
<body>
    <div class="container">
        <h1>🤖 AI Agents Live Stream</h1>
        <div class="topic" id="topic">Загрузка темы...</div>
        <div class="agents" id="agents"></div>
    </div>
    <script src="https://cdn.socket.io/4.5.4/socket.io.min.js"></script>
    <script>
        const socket = io('http://localhost:5000');
        socket.on('connected', (data) => updateUI(data));
        socket.on('topic_update', (data) => document.getElementById('topic').textContent = data.topic);
        socket.on('agent_start_speaking', (data) => {
            document.querySelectorAll('.agent').forEach(a => a.classList.remove('active'));
            const el = document.getElementById('agent-' + data.agent_id);
            if (el) el.classList.add('active');
        });
        socket.on('new_message', (data) => {
            const el = document.getElementById('agent-' + data.agent_id);
            if (el) el.querySelector('.message').textContent = data.message;
        });
        socket.on('agent_stop_speaking', (data) => {
            const el = document.getElementById('agent-' + data.agent_id);
            if (el) el.classList.remove('active');
        });
        function updateUI(data) {
            const grid = document.getElementById('agents');
            grid.innerHTML = '';
            data.agents.forEach(agent => {
                const el = document.createElement('div');
                el.className = 'agent'; el.id = 'agent-' + agent.id;
                el.innerHTML = `<div class="avatar">${agent.avatar}</div><div class="name">${agent.name}</div><div class="expertise">${agent.expertise}</div><div class="message">Ожидание...</div>`;
                grid.appendChild(el);
            });
            document.getElementById('topic').textContent = data.topic;
        }
    </script>
</body>
</html>'''
        with open("stream_ui/index.html", "w", encoding="utf-8") as f:
            f.write(simple_html)
        logger.info("Создан простой UI")

    # Запускаем поток с дискуссией
    discussion_thread = threading.Thread(target=start_discussion_loop, daemon=True)
    discussion_thread.start()

    print("🚀 Запуск сервера...")
    print("=" * 70)
    print("🌐 Веб-интерфейс: http://localhost:5000")
    print("📊 Статистика: http://localhost:5000/stats")
    print("🔧 Управление YouTube стримом:")
    print("   http://localhost:5000/start_youtube  - запустить стрим")
    print("   http://localhost:5000/stop_youtube   - остановить стрим")
    print("   http://localhost:5000/youtube_info   - информация о стриме")
    print("=" * 70)
    print("💡 Совет: Чтобы вывести стрим в OBS, используйте браузерный источник с URL выше")
    print("=" * 70)
    print("🎬 Дискуссия начнется автоматически через 3 секунды...")

    try:
        socketio.run(app,
                     host='0.0.0.0',
                     port=5000,
                     debug=False,
                     use_reloader=False,
                     allow_unsafe_werkzeug=True)
    except Exception as e:
        logger.error(f"Ошибка запуска сервера: {e}")
        print(f"\n❌ Ошибка: {e}")
        print("Попробуйте другой порт: socketio.run(..., port=8080)")