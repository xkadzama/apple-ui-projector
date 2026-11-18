#!/usr/bin/env python3
"""Проверка, что отладка по ADB действительно выключена и порт закрыт.

Зачем отдельный скрипт: переключатель на проекторе может выглядеть выключенным, а порт —
оставаться открытым (так бывает, если включён только «USB debugging», а «Network debugging»
остался). Единственный надёжный признак — отказ в соединении.

Использование:
    python3 tools/adb-closed.py 192.168.1.50
    python3 tools/adb-closed.py 192.168.1.50 5877     проверить конкретный порт
    python3 tools/adb-closed.py 192.168.1.50 --all    проверить известные порты ADB

Коды выхода:
    0  порт закрыт (connection refused) — отладка выключена
    1  порт ещё отвечает — отладка включена
    2  проверить не удалось (таймаут, «no route to host»): это не доказательство ни того, ни другого
"""
import socket
import sys

PORTS = (5555, 5877, 5037)


def check(host, port, timeout=4.0):
    s = socket.socket()
    s.settimeout(timeout)
    try:
        s.connect((host, port))
        return "open"
    except ConnectionRefusedError:
        return "refused"
    except socket.timeout:
        return "timeout"
    except OSError as e:
        return f"error:{e.errno}"


def main(argv):
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__.strip())
        return 0 if argv else 2
    host = argv[0]
    check_all = "--all" in argv
    ports = PORTS if check_all else [int(argv[1])] if len(argv) > 1 and argv[1].isdigit() else [5555]

    worst = 0
    for port in ports:
        state = check(host, port)
        if state == "refused":
            print(f"  {host}:{port} — закрыт (отладка выключена) ✓")
        elif state == "open":
            print(f"  {host}:{port} — ОТКРЫТ: отладка всё ещё включена ✗")
            worst = max(worst, 1)
        elif state == "timeout":
            print(f"  {host}:{port} — нет ответа (устройство выключено или не в сети)")
            worst = max(worst, 2)
        else:
            print(f"  {host}:{port} — проверить не удалось ({state}).")
            print("     На macOS это часто значит, что терминалу запрещён доступ к локальной сети:")
            print("     Системные настройки → Конфиденциальность и безопасность → Локальная сеть.")
            worst = max(worst, 2)
    return worst


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
