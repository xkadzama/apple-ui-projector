#!/usr/bin/env bash
# Один вход: найти проектор, снять бэкап, провести разведку, показать план.
# Ничего не меняет на устройстве — только читает. Дальше решение за вами.
#
#   ./start.sh                 всё по порядку
#   ./start.sh 192.168.1.50    если IP уже известен
set -uo pipefail
cd "$(dirname "$0")"
PY=python3

IP="${1:-}"
if [ -z "$IP" ]; then
  echo "== Ищу проектор в локальной сети =="
  "$PY" tools/adb.py --discover || true
  echo
  read -r -p "Введите IP проектора: " IP
fi
[ -n "$IP" ] || { echo "Без IP дальше нельзя."; exit 1; }

export TV_HOST="$IP"
export TV_PORT="${TV_PORT:-5555}"

echo
echo "== Проверяю подключение =="
if ! "$PY" tools/adb.py --probe; then
  echo
  echo "Не получилось. Проверьте, что на экране проектора нажато «Разрешить»."
  echo "Если устройства нет в сети: Настройки → Для разработчиков → Отладка по ADB."
  exit 1
fi

echo
echo "== Бэкап (до любых изменений) =="
"$PY" tools/backup.py || exit 1

echo
echo "== Разведка: что можно отключить =="
"$PY" tools/discover.py || exit 1

echo
echo "== План (ничего не меняется) =="
"$PY" tools/debloat.py lists/projector-generic.txt --dry-run || true

cat <<'EOF'

──────────────────────────────────────────────────────────────
Дальше выберите один из вариантов:

  1) Список под вашу модель (рекомендуется):
       python3 tools/discover.py --write
       python3 tools/debloat.py lists/<модель>-debloat.txt

  2) Базовый список:
       python3 tools/debloat.py lists/projector-generic.txt

  3) Новый домашний экран в стиле Apple TV:
       ./setup_toolchain.sh
       TV_HOST=<ip> ./launcher/install_launcher.sh

  Вернуть всё: python3 tools/restore.py
──────────────────────────────────────────────────────────────
EOF
