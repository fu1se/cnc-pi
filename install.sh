#!/usr/bin/env bash
# Установка cncd на Raspberry Pi. Запускать из каталога проекта: bash install.sh
set -e
cd "$(dirname "$0")"
DIR="$(pwd)"
USER_NAME="$(id -un)"

echo "== 1. пакеты"
sudo apt-get update -qq
sudo apt-get install -y python3-venv python3-pip

echo "== 2. доступ к последовательному порту"
sudo usermod -aG dialout "$USER_NAME"

echo "== 3. виртуальное окружение"
python3 -m venv venv
./venv/bin/pip install -q --upgrade pip
./venv/bin/pip install -q -r requirements.txt

echo "== 4. служба systemd"
sed -e "s|/home/pi/cnc-pi|$DIR|g" -e "s|^User=pi$|User=$USER_NAME|" \
    cncd.service | sudo tee /etc/systemd/system/cncd.service >/dev/null
sudo systemctl daemon-reload
sudo systemctl enable --now cncd

echo
echo "Готово. Интерфейс: http://$(hostname -I | awk '{print $1}'):8080  (или http://$(hostname).local:8080)"
echo "Если порт не /dev/ttyUSB0 — поправьте CNC_PORT в /etc/systemd/system/cncd.service"
echo "Группа dialout применится после перелогина или reboot."
