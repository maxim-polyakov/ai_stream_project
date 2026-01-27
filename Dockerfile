FROM python:3.10-slim

# Установка только необходимых зависимостей
RUN apt-get update && apt-get install -y \
    ffmpeg \
    curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . /app

# Создание директорий
RUN mkdir -p audio_cache stream_ui

# Не привилегированный пользователь
USER 1000:1000

EXPOSE 5000
CMD ["python", "main_ffmpeg.py"]