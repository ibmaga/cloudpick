#!/usr/bin/env bash
set -e

INSTALL_PATH="/usr/local/bin/cloudpick"
RAW="https://raw.githubusercontent.com/ibmaga/cloudpick/main/cloudpick.py"

echo "╔══════════════════════════════════════════╗"
echo "║      cloudpick installer — cloud.ru      ║"
echo "╚══════════════════════════════════════════╝"

# Python 3.6+
if ! command -v python3 &>/dev/null; then
    echo "❌ python3 не найден. Установи Python 3.6+."
    exit 1
fi

PYVER=$(python3 -c "import sys; print(sys.version_info.minor)")
if [ "$PYVER" -lt 6 ]; then
    echo "❌ Нужен Python 3.6+."
    exit 1
fi

# requests
if ! python3 -c "import requests" &>/dev/null; then
    echo "📦 Устанавливаю requests…"
    pip3 install requests --break-system-packages -q 2>/dev/null || \
    pip3 install requests -q
fi

echo "⬇️  Загружаю cloudpick…"
curl -fsSL "$RAW" -o /tmp/cloudpick.py

# Нужны права sudo для /usr/local/bin
if [ "$(id -u)" -ne 0 ]; then
    sudo mv /tmp/cloudpick.py "$INSTALL_PATH"
    sudo chmod +x "$INSTALL_PATH"
else
    mv /tmp/cloudpick.py "$INSTALL_PATH"
    chmod +x "$INSTALL_PATH"
fi

echo ""
echo "✅ Установлено: $INSTALL_PATH"
echo ""
echo "Запуск:"
echo "  cloudpick"
echo ""
echo "Или с переменными окружения (чтобы не вводить каждый раз):"
echo "  export CLOUDRU_KEY_ID=..."
echo "  export CLOUDRU_KEY_SECRET=..."
echo "  export CLOUDRU_PROJECT_ID=..."
echo "  cloudpick"
