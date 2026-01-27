FROM python:3.10-slim

# Установка зависимостей
RUN apt-get update && apt-get install -y \
    ffmpeg \
    curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Копирование requirements
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Копирование кода
COPY . /app

# Создаем пользователя ПОСЛЕ установки всех зависимостей
RUN useradd -m -u 1000 appuser

# Даем права на запись в audio_cache
RUN mkdir -p audio_cache && \
    chown -R appuser:appuser /app && \
    chmod 755 /app && \
    chmod 777 /app/audio_cache

USER appuser

EXPOSE 5000
CMD ["python", "main_ffmpeg.py"]