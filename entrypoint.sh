#!/bin/bash
set -e

echo "🔄 Checking for updates from Git..."

# Проверяем, есть ли изменения
git fetch origin

LOCAL=$(git rev-parse HEAD)
REMOTE=$(git rev-parse origin/main)

if [ "$LOCAL" != "$REMOTE" ]; then
    echo "📥 New changes detected, pulling..."
    git pull origin main

    echo "📦 Updating dependencies..."
    pip install --no-cache-dir -r requirements.txt > /dev/null 2>&1
else
    echo "✅ Already up to date"
fi

echo "🚀 Starting application..."
exec python main.py