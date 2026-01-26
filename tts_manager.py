#!/usr/bin/env python3
"""
Edge TTS Manager - РАБОЧИЕ мужские голоса Microsoft
"""

import os
import asyncio
import tempfile
import hashlib
import logging
from typing import Optional
import edge_tts
import pygame

logger = logging.getLogger(__name__)


class EdgeTTSManager:
    """Менеджер TTS с Edge TTS от Microsoft (есть мужские голоса!)"""

    def __init__(self):
        # Инициализация pygame для воспроизведения
        pygame.mixer.init(frequency=44100, size=-16, channels=2, buffer=2048)

        # Настройки голосов Edge TTS
        self.voices_config = {
            # РУССКИЕ МУЖСКИЕ ГОЛОСА (работают!)
            'male_ru': {
                'voice': 'ru-RU-DmitryNeural',
                'rate': '+0%',
                'pitch': '+0Hz',
                'volume': '+0%'
            },
            'male_ru_deep': {
                'voice': 'ru-RU-DmitryNeural',
                'rate': '-10%',
                'pitch': '-20Hz',
                'volume': '+0%'
            },
            # РУССКИЕ ЖЕНСКИЕ ГОЛОСА
            'female_ru': {
                'voice': 'ru-RU-SvetlanaNeural',
                'rate': '+0%',
                'pitch': '+0Hz',
                'volume': '+0%'
            },
            'female_ru_soft': {
                'voice': 'ru-RU-DariyaNeural',
                'rate': '-5%',
                'pitch': '+10Hz',
                'volume': '-5%'
            }
        }

        # Создаем директорию для кэша
        self.cache_dir = 'audio_cache'
        os.makedirs(self.cache_dir, exist_ok=True)

        logger.info("Edge TTS Manager инициализирован")
        logger.info(f"Доступные голоса: {list(self.voices_config.keys())}")

    def _get_cache_path(self, text: str, voice_id: str) -> str:
        """Получение пути к кэшированному файлу"""
        text_hash = hashlib.md5(f"{text}_{voice_id}".encode('utf-8')).hexdigest()
        return os.path.join(self.cache_dir, f"{text_hash}.mp3")

    async def text_to_speech(self, text: str, voice_id: str = 'male_ru') -> Optional[str]:
        """
        Преобразование текста в речь через Edge TTS

        Args:
            text: Текст для озвучки
            voice_id: ID голоса из конфига

        Returns:
            Путь к аудио файлу
        """
        try:
            # Проверяем голос
            if voice_id not in self.voices_config:
                logger.warning(f"Голос {voice_id} не найден, использую male_ru")
                voice_id = 'male_ru'

            voice_config = self.voices_config[voice_id]

            # Проверяем кэш
            cache_path = self._get_cache_path(text, voice_id)

            if os.path.exists(cache_path):
                logger.debug(f"Используем кэш: {cache_path}")
                return cache_path

            # Формируем параметры для Edge TTS
            communicate = edge_tts.Communicate(
                text=text,
                voice=voice_config['voice'],
                rate=voice_config['rate'],
                pitch=voice_config['pitch'],
                volume=voice_config['volume']
            )

            logger.info(f"Генерируем Edge TTS: голос={voice_config['voice']}")

            # Сохраняем аудио во временный файл
            with tempfile.NamedTemporaryFile(delete=False, suffix='.mp3') as tmp_file:
                temp_path = tmp_file.name

            # Сохраняем аудио
            await communicate.save(temp_path)

            # Переносим в кэш
            import shutil
            shutil.move(temp_path, cache_path)

            logger.info(f"Аудио сохранено: {cache_path} ({os.path.getsize(cache_path)} bytes)")
            return cache_path

        except Exception as e:
            logger.error(f"Ошибка Edge TTS: {e}")
            return None

    async def speak(self, text: str, voice_id: str = 'male_ru') -> bool:
        """
        Озвучивание текста

        Args:
            text: Текст для озвучки
            voice_id: ID голоса

        Returns:
            True если успешно
        """
        try:
            logger.info(f"Озвучиваем: {text[:50]}... голос={voice_id}")

            audio_file = await self.text_to_speech(text, voice_id)

            if not audio_file:
                logger.error("Не удалось получить аудио файл")
                return False

            # Загружаем и воспроизводим
            pygame.mixer.music.load(audio_file)
            pygame.mixer.music.play()

            # Ждем окончания воспроизведения
            while pygame.mixer.music.get_busy():
                await asyncio.sleep(0.1)

            return True

        except Exception as e:
            logger.error(f"Ошибка воспроизведения: {e}")
            return False

    async def test_all_voices(self):
        """Тестирование всех голосов"""
        test_text = "Здравствуйте! Это тест мужского и женского голосов."

        print("\n🔊 ТЕСТ ГОЛОСОВ EDGE TTS")
        print("=" * 50)

        for voice_id, config in self.voices_config.items():
            print(f"\n🎤 Тест голоса: {voice_id}")
            print(f"⚙️  Конфиг: {config}")

            try:
                success = await self.speak(test_text, voice_id)
                if success:
                    print("✅ УСПЕХ!")
                else:
                    print("❌ ОШИБКА")
            except Exception as e:
                print(f"❌ КРИТИЧЕСКАЯ ОШИБКА: {e}")

            await asyncio.sleep(1)  # Пауза между тестами

    def stop(self):
        """Остановка воспроизведения"""
        pygame.mixer.music.stop()

    def cleanup(self):
        """Очистка ресурсов"""
        pygame.mixer.quit()