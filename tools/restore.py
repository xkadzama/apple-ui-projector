#!/usr/bin/env python3
"""Возврат проектора в исходное состояние — по бэкапу, а не «на глаз».

Что делает:
  * включает обратно только те пакеты, которые отключили МЫ (из ~/.projector/last_apply.json);
  * пакеты, отключённые вами раньше, остаются отключёнными;
  * возвращает домашний экран туда, где он был в бэкапе;
  * проверяет результат чтением и говорит, что не получилось.

Использование:
    TV_HOST=192.168.1.50 python3 tools/restore.py                 всё, что мы отключили
    TV_HOST=192.168.1.50 python3 tools/restore.py --list          только показать план
    TV_HOST=192.168.1.50 python3 tools/restore.py --home          только вернуть домашний экран
    TV_HOST=192.168.1.50 python3 tools/restore.py --only pkg      конкретный пакет
    TV_HOST=192.168.1.50 python3 tools/restore.py --backup DIR    из конкретного бэкапа

Коды выхода: 0 — всё вернулось, 1 — что-то не встало на место, 2 — неверный вызов.
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import adb  # noqa: E402
import backup as backup_mod  # noqa: E402

ROOT = os.path.expanduser("~/.projector")


def parse_packages(text):
    return {line.strip()[len("package:"):] for line in text.splitlines()
            if line.strip().startswith("package:")}


def load_applied():
    path = os.path.join(ROOT, "last_apply.json")
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def short(component):
    """pkg/pkg.Activity и pkg/.Activity — одна и та же компонента."""
    if "/" not in component:
        return component
    pkg, act = component.split("/", 1)
    if act.startswith(pkg + "."):
        act = "." + act[len(pkg) + 1:]
    return f"{pkg}/{act}"


def main(argv):
    if argv and argv[0] in ("-h", "--help"):
        print(__doc__.strip())
        return 0
    show_only = "--list" in argv
    only_home = "--home" in argv
    only = [argv[i + 1] for i, a in enumerate(argv[:-1]) if a == "--only"]
    backup_dir = ""
    for i, a in enumerate(argv[:-1]):
        if a == "--backup":
            backup_dir = argv[i + 1]

    host = os.environ.get("TV_HOST", "").strip()
    if not host:
        print("Не задан TV_HOST. Пример: TV_HOST=192.168.1.50 python3 tools/restore.py",
              file=sys.stderr)
        return 2
    port = int(os.environ.get("TV_PORT") or 5555)
    if ":" in host:
        host, p = host.rsplit(":", 1)
        port = int(p)

    applied = load_applied()
    bdir = backup_dir or (backup_mod.latest_backup() or "")
    if not bdir or not os.path.isdir(bdir):
        print("Не нашёл бэкап в ~/.projector/backups — восстановить «как было» не из чего.\n"
              "Можно вернуть конкретные пакеты: --only <package>", file=sys.stderr)
        if not applied:
            return 2

    before_disabled = backup_mod.read_packages(bdir, "packages_disabled.txt") if bdir else set()
    before_home = ""
    if bdir:
        hp = os.path.join(bdir, "home_activity.txt")
        if os.path.exists(hp):
            with open(hp, encoding="utf-8") as f:
                lines = [ln.strip() for ln in f if ln.strip()]
            if lines:
                before_home = lines[-1].split()[0]

    to_enable = []
    if applied:
        to_enable = list(applied.get("applied", []))
    elif only:
        to_enable = list(only)
    to_enable = [p for p in to_enable if p not in before_disabled]
    if only:
        to_enable = [p for p in to_enable if p in only]

    dev = adb.AdbConnection(host, port, timeout=float(os.environ.get("TV_TIMEOUT") or 30))
    try:
        dev.connect()
        now_disabled = parse_packages(
            dev.shell("pm list packages -d --user 0").decode("utf-8", "replace"))
        plan = [p for p in to_enable if p in now_disabled]

        print(f"Бэкап: {bdir or 'нет'}")
        if only_home:
            print(f"Вернуть домашний экран: {before_home or '(в бэкапе не записан)'}")
        elif plan:
            print(f"Будет включено обратно ({len(plan)}):")
            for p in plan:
                print(f"  + {p}")
        else:
            print("Включать нечего: всё, что мы отключали, уже включено "
                  "или было отключено до нас.")

        if show_only:
            return 0
        if not plan and not only_home:
            return 0

        failed = []
        if not only_home:
            for i, p in enumerate(plan, 1):
                out = dev.shell(f"pm enable --user 0 {p}").decode("utf-8", "replace").strip()
                bad = "Error" in out or "Exception" in out
                print(f"  [{i}/{len(plan)}] {p}: {'ОТКАЗ' if bad else (out or 'ok')}")
                if bad:
                    failed.append(p)

        if before_home and "/" in before_home:
            dev.shell(f"cmd package set-home-activity --user 0 {before_home}")
            got = dev.shell(
                "cmd package resolve-activity --brief -a android.intent.action.MAIN "
                "-c android.intent.category.HOME").decode("utf-8", "replace")
            got_lines = [ln.strip() for ln in got.splitlines() if "/" in ln]
            got_home = got_lines[-1].split()[0] if got_lines else ""
            if got_home and short(got_home) != short(before_home):
                print(f"  Home не вернулся: устройство отвечает {got_home}, в бэкапе {before_home}")
                print(f"  Попробуйте вручную: python3 tools/adb.py "
                      f"'cmd package set-home-activity --user 0 {before_home}'")
                failed.append("home")
            elif got_home:
                print(f"  Home вернулся: {got_home}")

        dev.shell("sync")
        still = parse_packages(
            dev.shell("pm list packages -d --user 0").decode("utf-8", "replace"))
        left = [p for p in plan if p in still]
        if left:
            print(f"\nОстались отключёнными ({len(left)}): {', '.join(left)}")
            print("Прошивка могла держать их выключенными сама; включите вручную в "
                  "Настройки → Приложения или повторите: --only <package>")
            failed.extend(left)
        elif plan:
            print(f"\nВсе {len(plan)} пакет(ов) включены обратно.")
        return 1 if failed else 0
    except adb.AdbError as e:
        print(f"ошибка: {e}", file=sys.stderr)
        return 1
    finally:
        dev.close()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
