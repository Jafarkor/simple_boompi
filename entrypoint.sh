#!/bin/bash
set -e

echo "🔄 Checking for updates from Git..."

# Проверяем, есть ли изменения
git fetch origin > /dev/null 2>&1

LOCAL=$(git rev-parse HEAD)
REMOTE=$(git rev-parse origin/main)

if [ "$LOCAL" != "$REMOTE" ]; then
    echo "📥 New changes detected, pulling..."

    # Сбрасываем все локальные изменения и принудительно обновляем
    git reset --hard origin/main

    echo "📦 Updating dependencies..."
    pip install --no-cache-dir -r requirements.txt > /dev/null 2>&1

    echo "✅ Updated successfully"
else
    echo "✅ Already up to date"
fi

echo "🚀 Starting application..."
exec python main.py