#!/usr/bin/env bash
# Все тесты разом: python3 tools/run_tests.sh
set -uo pipefail
cd "$(dirname "$0")"
fail=0
for t in test_adb test_debloat test_integration; do
  printf '\n=== %s ===\n' "$t"
  if ! python3 -W ignore "$t.py" 2>&1 | grep -vE "ResourceWarning|tracemalloc|self\.sock\.accept|del self\._target|^\s*$|^\s+conn, _"; then
    :
  fi
  # статус берём у самого python, а не у grep
  python3 -W ignore "$t.py" >/dev/null 2>&1 || { echo "ПРОВАЛ: $t"; fail=1; }
done
echo
if [ "$fail" = "0" ]; then echo "Все тесты пройдены."; else echo "Есть провалы."; fi
exit "$fail"
