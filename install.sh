#!/usr/bin/env bash
# Установка easy-switcher-ng вместо старого easy-switcher.
set -euo pipefail
if [ "$(id -u)" -eq 0 ]; then
    echo "Запускайте без sudo: пароль будет запрошен сам, а программа ставится для вашего пользователя." >&2
    exit 1
fi
cd "$(dirname "$0")"
DEST="$HOME/.local/share/easy-switcher-ng"

echo "== Останавливаю старый easy-switcher (нужен sudo)"
sudo systemctl disable --now easy-switcher.service || true

echo "== Права на устройства ввода для текущего сеанса"
sudo install -m 644 70-easy-switcher-ng.rules /etc/udev/rules.d/
sudo udevadm control --reload
sudo udevadm trigger --subsystem-match=input --subsystem-match=misc

echo "== Файлы программы"
mkdir -p "$DEST" "$HOME/.config/easy-switcher-ng" "$HOME/.config/systemd/user"
install -m 755 easy_switcher_ng.py "$DEST/"
[ -d "$DEST/venv" ] || python3 -m venv "$DEST/venv"
"$DEST/venv/bin/pip" install -q evdev
[ -f "$HOME/.config/easy-switcher-ng/config.toml" ] || install -m 644 config.toml "$HOME/.config/easy-switcher-ng/"
install -m 644 easy-switcher-ng.service "$HOME/.config/systemd/user/"

echo "== Запуск"
systemctl --user daemon-reload
systemctl --user enable --now easy-switcher-ng.service
sleep 1
systemctl --user --no-pager status easy-switcher-ng.service | head -12
