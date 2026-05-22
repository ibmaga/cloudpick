#!/usr/bin/env bash
set -e

INSTALL_PATH="/usr/local/bin/cloudpick"
CONFIG_DIR="$HOME/.cloudpick"
RAW="https://raw.githubusercontent.com/ibmaga/cloudpick/main"

echo "╔══════════════════════════════════════════╗"
echo "║      cloudpick installer — cloud.ru      ║"
echo "╚══════════════════════════════════════════╝"

# Python 3.6+
if ! command -v python3 &>/dev/null; then
    echo "❌ python3 не найден."
    exit 1
fi

# requests
if ! python3 -c "import requests" &>/dev/null; then
    echo "📦 Устанавливаю requests…"
    pip3 install requests --break-system-packages -q 2>/dev/null || pip3 install requests -q
fi

echo "⬇️  Загружаю cloudpick…"
curl -fsSL "$RAW/cloudpick.py" -o /tmp/cloudpick.py

if [ "$(id -u)" -ne 0 ]; then
    sudo mv /tmp/cloudpick.py "$INSTALL_PATH"
    sudo chmod +x "$INSTALL_PATH"
else
    mv /tmp/cloudpick.py "$INSTALL_PATH"
    chmod +x "$INSTALL_PATH"
fi

# Создаём папку конфига и кладём пример .env
mkdir -p "$CONFIG_DIR"
if [ ! -f "$CONFIG_DIR/.env" ]; then
    curl -fsSL "$RAW/.env.example" -o "$CONFIG_DIR/.env.example"
    echo ""
    echo "📄 Шаблон конфига: $CONFIG_DIR/.env.example"
    echo "   Скопируй и заполни:"
    echo "   cp $CONFIG_DIR/.env.example $CONFIG_DIR/.env"
    echo "   nano $CONFIG_DIR/.env"
else
    echo "📄 Конфиг уже есть: $CONFIG_DIR/.env"
fi

echo ""
echo "✅ Установлено: $INSTALL_PATH"
echo ""
echo "Запуск:  cloudpick"
echo "Логи:    tail -f /var/log/cloudpick.log"
echo "Статус:  pgrep -a python3 | grep cloudpick"
echo "Стоп:    pkill -f cloudpick"
