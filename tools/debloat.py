#!/usr/bin/env python3
"""Применение списка деблоата к проектору — с защитой от «кирпича».

Главное правило: ничего не удаляется. Пакеты отключаются через `pm disable-user --user 0`,
что равносильно выключению в Настройки → Приложения, и любой из них возвращается одной командой.

Защиты (как в оригинальном репозитории, проверены на реальных устройствах там):
  1. CORE — отказ на пакеты ядра Android и middleware ТВ. С одной из них телевизор/проектор
     может больше не загрузиться. Обойти нельзя.
  2. HOME GUARD — если в списке есть владелец кнопки Home и на устройстве больше никто
     не отвечает на HOME, запись пропускается, пока новый лончер не станет домашним.
  3. SETUPWRAITH — сетап-мастер Google TV может владеть сохранённой Wi-Fi сетью: его отключение
     удаляет сеть и роняет ADB. Сначала спрашиваем `dumpsys wifi`, кто владелец.
  4. БЭКАП — состояние пишется до первого изменения (tools/backup.py).

Использование:
    TV_HOST=192.168.1.50 python3 tools/debloat.py lists/projector-generic.txt --dry-run
    TV_HOST=192.168.1.50 python3 tools/debloat.py lists/projector-generic.txt
    TV_HOST=192.168.1.50 python3 tools/debloat.py lists/projector-generic.txt --only com.x.bugreport

Флаги:
    --dry-run        показать план, ничего не менять
    --only PKG       применить только этот пакет (повторяемый)
    --force-home     отключить владельца Home, даже если больше некому его показать
    --force-setupwraith  отключить сетап-мастер, не проверяя владельца Wi-Fi

Коды выхода: 0 — план применён (или dry-run), 1 — были отказы/ошибки, 2 — неверный вызов.
"""
import json
import os
import re
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import adb  # noqa: E402
import backup as backup_mod  # noqa: E402

# Пакеты, без которых Android или ТВ-часть не поднимаются. Тот же набор, что в оригинале:
# с одним из них реальные устройства уходили в цикл перезагрузки.
CORE_RE = re.compile(
    r"^(android"
    r"|android\.ext\.(services|shared)"
    r"|com\.(google\.)?android\.sdksandbox"
    r"|com\.android\.location\.fused"
    r"|com\.android\.systemui"
    r"|com\.android\.shell"
    r"|com\.android\.phone"
    r"|com\.android\.settings"
    r"|com\.android\.tv\.settings"
    r"|com\.(google\.)?android\.(permissioncontroller|packageinstaller|ext\.services|ext\.shared|modulemetadata)"
    r"|com\.google\.android\.overlay\.modules.*"
    r"|com\.android\.providers\..*"
    r"|com\.cltv\.mal"
    r"|com\.mediatek\.(tis|tv\.service|tv\.agent)"
    r")$"
)
CORE_OK = re.compile(r"^com\.android\.providers\.(calendar|contacts|userdictionary|blockednumber)$")

SETUPWRAITH = "com.google.android.tungsten.setupwraith"

HOME_RESOLVE = (
    "cmd package resolve-activity --brief -a android.intent.action.MAIN "
    "-c android.intent.category.HOME"
)
HOME_QUERY = (
    "cmd package query-activities --brief -a android.intent.action.MAIN "
    "-c android.intent.category.HOME"
)

KNOWN_LAUNCHERS = {
    "com.google.android.tvlauncher",
    "com.google.android.apps.tv.launcherx",
    "com.google.android.leanbacklauncher",
    "com.mitv.tvhome",
}

# Пакеты, которые отвечают на HOME, но домашним экраном не являются:
# мастер настройки, настройки, вендорский FallbackHome, заводские тесты.
NOT_A_LAUNCHER = re.compile(
    r"(setupwraith|fallbackhome|\.settings$|factorytest|factory\.test|"
    r"com\.android\.settings|com\.android\.tv\.settings|launcher3\.quickstep)$",
    re.I,
)

PKG_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z0-9_]+)+$")


def read_list(path):
    """Читает список: одна строка — один пакет, # — комментарий, всё после # игнорируется."""
    if not os.path.exists(path):
        raise SystemExit(f"нет такого файла списка: {path}")
    pkgs, seen = [], set()
    with open(path, encoding="utf-8-sig") as f:
        for lineno, raw in enumerate(f, 1):
            line = raw.split("#", 1)[0].strip()
            if not line:
                continue
            pkg = line.split()[0]
            if not PKG_RE.match(pkg):
                raise SystemExit(
                    f"строка {lineno} в {path}: «{pkg}» не похоже на имя пакета.\n"
                    "Список испорчен (BOM? Windows-переносы?) — исправьте и запустите снова."
                )
            if pkg not in seen:
                seen.add(pkg)
                pkgs.append(pkg)
    return pkgs


def parse_packages(text):
    return {line.strip()[len("package:"):] for line in text.splitlines()
            if line.strip().startswith("package:")}


class Plan:
    def __init__(self, pkgs, disabled_now, owners_home, home_pkg, wifi_owners, sdk):
        self.pkgs = pkgs
        self.disabled_now = disabled_now
        self.owners_home = owners_home
        self.home_pkg = home_pkg
        self.wifi_owners = wifi_owners
        self.sdk = sdk
        self.apply, self.skip, self.absent = [], [], []

    def report(self):
        if self.apply:
            print(f"Будет отключено ({len(self.apply)}):")
            for p in self.apply:
                print(f"  × {p}")
        for p, why in self.skip:
            print(f"пропуск  {p}\n          причина: {why}")
        if self.absent:
            print(f"Уже нет в списке устройства ({len(self.absent)}): {', '.join(self.absent)}")


def build_plan(pkgs, installed, disabled_now, owners_home, home_pkg, wifi_owners, sdk,
               force_home=False, force_setupwraith=False):
    plan = Plan(pkgs, disabled_now, owners_home, home_pkg, wifi_owners, sdk)

    # 1. Ядро Android / middleware — отказ целиком, до любых изменений.
    core = [p for p in pkgs if CORE_RE.match(p) and not CORE_OK.match(p)]
    if core:
        raise SystemExit(
            "ОТКАЗ: в списке есть пакеты ядра Android или ТВ-middleware. С ними проектор может\n"
            "не загрузиться, и починить это по ADB будет нельзя:\n"
            + "\n".join(f"  ! {p}" for p in core)
            + "\n\nЗакомментируйте эти строки и запустите снова. На проекторе ничего не изменено."
        )

    # 2. Прочие потенциально опасные: телефония/системные сервисы, если попали случайно.
    for p in pkgs:
        if p not in installed:
            plan.absent.append(p)
            continue
        if p in disabled_now:
            plan.skip.append((p, "уже отключён — не наше изменение, restore не должен его включать"))
            continue

        # Home guard
        other_home = [h for h in owners_home if h != p and not NOT_A_LAUNCHER.search(h)]
        if p in owners_home and not other_home and not force_home:
            plan.skip.append((
                p,
                "владелец кнопки Home, и на проекторе нет другого домашнего экрана. "
                "Сначала поставьте лончер и сделайте его домашним (tools/install_launcher.sh), "
                "потом повторите список. Принудительно: --force-home",
            ))
            continue

        # setupwraith guard
        if p == SETUPWRAITH and not force_setupwraith:
            if not wifi_owners:
                plan.skip.append((
                    p, "не удалось проверить владельца сохранённых Wi-Fi сетей (dumpsys wifi пуст). "
                       "Если проектор на кабеле — можно с --force-setupwraith"))
                continue
            if any("setupwraith" in w for w in wifi_owners):
                plan.skip.append((
                    p, "сохранённая Wi-Fi сеть принадлежит сетап-мастеру: его отключение удалит сеть "
                       "и оборвёт ADB. На проекторе забудьте сеть и подключитесь заново в Настройках, "
                       "потом повторите"))
                continue

        plan.apply.append(p)
    return plan


def owner_of_home(text):
    """Достаёт имя пакета из вывода resolve-activity (последняя строка вида pkg/Activity)."""
    for line in reversed(text.strip().splitlines()):
        line = line.strip()
        if "/" in line:
            return line.split("/", 1)[0]
    return ""


def home_packages(text):
    """Достаёт список пакетов из query-activities."""
    found = []
    for line in text.splitlines():
        line = line.strip()
        if "/" in line and not line.startswith("priority"):
            pkg = line.split("/", 1)[0].split()[0]
            if PKG_RE.match(pkg):
                found.append(pkg)
    return found


def wifi_owners(dev):
    try:
        out = dev.shell("dumpsys wifi", timeout=45).decode("utf-8", "replace")
    except adb.AdbError:
        return []
    return re.findall(r"cname=([^ \r\n]+)", out)


def verify_disabled(dev, expected):
    """Читает обратно `pm list packages -d --user 0` и возвращает те, что не отключились."""
    out = dev.shell("pm list packages -d --user 0").decode("utf-8", "replace")
    off = parse_packages(out)
    return [p for p in expected if p not in off]


def main(argv):
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__.strip())
        return 0 if argv else 2
    list_path = argv[0]
    dry = "--dry-run" in argv
    force_home = "--force-home" in argv
    force_setupwraith = "--force-setupwraith" in argv
    only = []
    for i, a in enumerate(argv):
        if a == "--only" and i + 1 < len(argv):
            only.append(argv[i + 1])

    host = os.environ.get("TV_HOST", "").strip()
    if not host:
        print("Не задан TV_HOST. Пример: TV_HOST=192.168.1.50 python3 tools/debloat.py <список>",
              file=sys.stderr)
        return 2
    port = int(os.environ.get("TV_PORT") or 5555)
    if ":" in host:
        host, p = host.rsplit(":", 1)
        port = int(p)

    pkgs = read_list(list_path)
    if only:
        pkgs = [p for p in pkgs if p in only]
        missing = [p for p in only if p not in pkgs]
        if missing:
            raise SystemExit(f"--only: этих пакетов нет в списке: {', '.join(missing)}")
    if not pkgs:
        raise SystemExit("в списке нет ни одного пакета (всё закомментировано?) — ничего не делаю")

    dev = adb.AdbConnection(host, port, timeout=float(os.environ.get("TV_TIMEOUT") or 30))
    try:
        dev.connect()
        info = adb.probe_device(host, port)
        if "error" in info:
            print(f"ошибка: {info['error']}", file=sys.stderr)
            return 1
        sdk = int(info["sdk"] or 0)
        print(f"Устройство: {info['brand']} {info['model']} — Android {info['android']} (SDK {sdk})")
        print(f"Прошивка  : {info['firmware']}\n")

        installed = parse_packages(dev.shell("pm list packages").decode("utf-8", "replace"))
        disabled_now = parse_packages(
            dev.shell("pm list packages -d --user 0").decode("utf-8", "replace"))
        home_text = dev.shell(HOME_RESOLVE).decode("utf-8", "replace")
        query_text = dev.shell(HOME_QUERY).decode("utf-8", "replace")
        home_pkg = owner_of_home(home_text)
        owners = home_packages(query_text) or ([home_pkg] if home_pkg else [])
        owners = list(dict.fromkeys(owners))
        wowners = wifi_owners(dev) if SETUPWRAITH in pkgs else []

        print(f"Домашний экран сейчас: {home_pkg or 'не определился'}")
        if len(owners) > 1:
            print(f"На HOME претендуют: {', '.join(owners)}")
        print()

        plan = build_plan(pkgs, installed, disabled_now, owners, home_pkg, wowners, sdk,
                          force_home=force_home, force_setupwraith=force_setupwraith)
        plan.report()
        if not plan.apply:
            print("\nНечего применять.")
            return 0

        if dry:
            print(f"\n--dry-run: на проекторе ничего не изменено. "
                  f"Отключил бы {len(plan.apply)} пакет(ов).")
            return 0

        bpath, _meta = backup_mod.make_backup(dev, note=f"перед {os.path.basename(list_path)}")
        print(f"\nБэкап до изменений: {bpath}")

        print(f"\nОтключаю по одному, с проверкой результата…")
        failed = []
        for i, p in enumerate(plan.apply, 1):
            out = dev.shell(f"pm disable-user --user 0 {p}").decode("utf-8", "replace").strip()
            bad = "Error" in out or "Exception" in out or "not allowed" in out
            print(f"  [{i}/{len(plan.apply)}] {p}: {'ОТКАЗ' if bad else (out or 'ok')}")
            if bad:
                failed.append(p)
            time.sleep(0.25)

        # Android пишет состояние на диск примерно через 10 секунд после изменения:
        # без sync и паузы перезагрузка сразу после списка поднимет старое состояние.
        dev.shell("sync")
        time.sleep(2)

        print("\nПроверяю чтением обратно (pm list packages -d):")
        # verify_disabled уже отвечает «что НЕ отключилось», второй раз инвертировать не нужно.
        not_off = verify_disabled(dev, plan.apply)
        if not_off:
            print(f"  не отключились ({len(not_off)}): {', '.join(not_off)}")
            print("  Такие пакеты прошивка может возвращать сама (например, стоковый лончер).")
        else:
            print(f"  все {len(plan.apply)} пакет(ов) отключены")

        result = {
            "applied": plan.apply,
            "skipped": [p for p, _ in plan.skip],
            "failed": failed,
            "not_disabled": not_off,
            "backup": bpath,
            "list": os.path.abspath(list_path),
        }
        out_path = os.path.join(os.path.expanduser("~/.projector"), "last_apply.json")
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
        print(f"\nИтог записан: {out_path}")
        print("Вернуть всё обратно: python3 tools/restore.py")
        return 0 if not failed else 1
    except adb.AdbError as e:
        print(f"ошибка: {e}", file=sys.stderr)
        return 1
    finally:
        dev.close()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
