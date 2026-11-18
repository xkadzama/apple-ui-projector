#!/usr/bin/env python3
"""Бэкап и восстановление проектора: снимок состояния ДО любых изменений.

Зачем: `pm disable-user` не удаляет пакеты, но список «что было включено» — единственное,
что позволяет вернуть устройство ровно в исходное состояние. Пишем его до первого изменения.

Использование:
    TV_HOST=192.168.1.50 python3 tools/backup.py            создать бэкап в ~/.projector/backups/<дата>/
    TV_HOST=192.168.1.50 python3 tools/backup.py --diff     сравнить с последним бэкапом
    python3 tools/backup.py --list                          показать сохранённые бэкапы

Что сохраняется:
    packages_all.txt       все пакеты (pm list packages)
    packages_disabled.txt  уже отключённые до нас (их трогать нельзя)
    props.txt              getprop — модель, прошивка, версия Android
    settings_global.txt    settings list global
    settings_secure.txt    settings list secure
    home_activity.txt      кто сейчас владеет кнопкой Home
    launchers.txt          все, кто претендует на HOME
    meta.json              сводка для человека и для скриптов
"""
import json
import os
import shutil
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import adb  # noqa: E402

ROOT = os.path.expanduser("~/.projector")
BACKUPS = os.path.join(ROOT, "backups")

COMMANDS = {
    "packages_all.txt": "pm list packages",
    "packages_disabled.txt": "pm list packages -d --user 0",
    "props.txt": "getprop",
    "settings_global.txt": "settings list global",
    "settings_secure.txt": "settings list secure",
    "home_activity.txt": (
        "cmd package resolve-activity --brief -a android.intent.action.MAIN "
        "-c android.intent.category.HOME"
    ),
    "launchers.txt": (
        "cmd package query-activities --brief -a android.intent.action.MAIN "
        "-c android.intent.category.HOME"
    ),
}


def parse_packages(text):
    return sorted(
        line.strip()[len("package:"):]
        for line in text.splitlines()
        if line.strip().startswith("package:")
    )


def make_backup(dev, note=""):
    stamp = time.strftime("%Y-%m-%d_%H%M%S")
    path = os.path.join(BACKUPS, stamp)
    os.makedirs(path, exist_ok=True)
    raw = {}
    for name, command in COMMANDS.items():
        out = dev.shell(command).decode("utf-8", "replace")
        raw[name] = out
        with open(os.path.join(path, name), "w", encoding="utf-8") as f:
            f.write(out)
    props = {}
    for line in raw["props.txt"].splitlines():
        import re
        m = re.match(r"\[([^\]]+)\]: \[(.*)\]", line)
        if m:
            props[m.group(1)] = m.group(2)
    meta = {
        "created": stamp,
        "note": note,
        "host": dev.host,
        "port": dev.port,
        "brand": props.get("ro.product.brand", ""),
        "model": props.get("ro.product.model", ""),
        "device": props.get("ro.product.device", ""),
        "android": props.get("ro.build.version.release", ""),
        "sdk": props.get("ro.build.version.sdk", ""),
        "firmware": props.get("ro.build.display.id", ""),
        "adb_secure": props.get("ro.adb.secure", ""),
        "home": raw["home_activity.txt"].strip().splitlines()[-1] if raw["home_activity.txt"].strip() else "",
        "packages_total": len(parse_packages(raw["packages_all.txt"])),
        "packages_disabled": len(parse_packages(raw["packages_disabled.txt"])),
    }
    with open(os.path.join(path, "meta.json"), "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)
    latest = os.path.join(ROOT, "latest")
    if os.path.islink(latest) or os.path.exists(latest):
        if os.path.islink(latest):
            os.unlink(latest)
        else:
            shutil.rmtree(latest)
    os.symlink(path, latest)
    return path, meta


def latest_backup():
    if not os.path.isdir(BACKUPS):
        return None
    dirs = sorted(d for d in os.listdir(BACKUPS) if os.path.isdir(os.path.join(BACKUPS, d)))
    return os.path.join(BACKUPS, dirs[-1]) if dirs else None


def read_packages(path, name="packages_all.txt"):
    p = os.path.join(path, name)
    if not os.path.exists(p):
        return set()
    with open(p, encoding="utf-8") as f:
        return set(parse_packages(f.read()))


def diff_with_latest(dev):
    path = latest_backup()
    if not path:
        print("Бэкапов ещё нет — сначала запустите: python3 tools/backup.py")
        return 1
    with open(os.path.join(path, "meta.json"), encoding="utf-8") as f:
        meta = json.load(f)
    print(f"Сравниваю с бэкапом {os.path.basename(path)} ({meta['created']})\n")
    before = read_packages(path)
    now = set(parse_packages(dev.shell("pm list packages").decode("utf-8", "replace")))
    disabled_now = set(parse_packages(
        dev.shell("pm list packages -d --user 0").decode("utf-8", "replace")))
    disabled_before = read_packages(path, "packages_disabled.txt")

    added = sorted(now - before)
    removed = sorted(before - now)
    disabled = sorted(disabled_now - disabled_before)

    if not (added or removed or disabled):
        print("Изменений нет: набор и состояние пакетов совпадают с бэкапом.")
        return 0
    if added:
        print(f"Появились новые пакеты ({len(added)}):")
        for p in added:
            print(f"  + {p}")
    if removed:
        print(f"\nПропали пакеты ({len(removed)}):")
        for p in removed:
            print(f"  - {p}")
    if disabled:
        print(f"\nОтключены после бэкапа ({len(disabled)}):")
        for p in disabled:
            print(f"  × {p}")
    print("\nВернуть всё: python3 tools/restore.py")
    return 0


def main(argv):
    if argv and argv[0] == "--list":
        if not os.path.isdir(BACKUPS):
            print("Бэкапов нет.")
            return 0
        for d in sorted(os.listdir(BACKUPS)):
            full = os.path.join(BACKUPS, d)
            if not os.path.isdir(full):
                continue
            meta = {}
            try:
                with open(os.path.join(full, "meta.json"), encoding="utf-8") as f:
                    meta = json.load(f)
            except OSError:
                pass
            print(f"{d}  {meta.get('brand', '')} {meta.get('model', '')}  "
                  f"Android {meta.get('android', '?')}  "
                  f"пакетов: {meta.get('packages_total', '?')}")
        return 0

    host = os.environ.get("TV_HOST", "").strip()
    if not host:
        print("Не задан TV_HOST. Пример: TV_HOST=192.168.1.50 python3 tools/backup.py", file=sys.stderr)
        return 2
    port = int(os.environ.get("TV_PORT") or 5555)
    if ":" in host:
        host, p = host.rsplit(":", 1)
        port = int(p)

    dev = adb.AdbConnection(host, port, timeout=float(os.environ.get("TV_TIMEOUT") or 30))
    try:
        dev.connect()
        if argv and argv[0] == "--diff":
            return diff_with_latest(dev)
        print("Снимаю состояние проектора (это займёт несколько секунд)…")
        note = argv[0] if argv else ""
        path, meta = make_backup(dev, note)
        print(f"\nБэкап сохранён: {path}")
        print(f"  {meta['brand']} {meta['model']}  Android {meta['android']} (SDK {meta['sdk']})")
        print(f"  прошивка : {meta['firmware']}")
        print(f"  Home     : {meta['home']}")
        print(f"  пакетов  : {meta['packages_total']} (из них уже отключено: {meta['packages_disabled']})")
        if meta["adb_secure"] in ("", "0"):
            print("\n  ВНИМАНИЕ: ro.adb.secure пуст или 0 — порт 5555 открыт любому в этой сети.")
            print("  После работы выключите отладку по ADB на проекторе.")
        return 0
    except adb.AdbError as e:
        print(f"ошибка: {e}", file=sys.stderr)
        return 1
    finally:
        dev.close()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
