FROM python:3.11
WORKDIR /app

# Устанавливаем git
RUN apt update && apt install -y ffmpeg lsof git

# Копируем файлы
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .

# Настраиваем Git (чтобы избежать ошибок)
RUN git config --global --add safe.directory /app

# Копируем и настраиваем entrypoint
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENTRYPOINT ["/entrypoint.sh"]