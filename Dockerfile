FROM python:3.12-slim

# ffmpeg нужен для voice→mp3, lsof для отладки сокетов внутри контейнера
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
        ffmpeg \
        ca-certificates \
        tini \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Сначала только requirements, чтобы Docker мог переиспользовать слой
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip \
 && pip install --no-cache-dir -r requirements.txt

# Затем код
COPY . .

# Создаём временные директории
RUN mkdir -p documents code_files

# tini — pid 1, который правильно обрабатывает SIGTERM от Docker
ENTRYPOINT ["/usr/bin/tini", "--"]

# Без буфера, чтобы логи появлялись сразу
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1

CMD ["python", "main.py"]
