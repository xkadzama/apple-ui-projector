#!/usr/bin/env python3
"""Разведка: что реально установлено на ВАШЕМ проекторе и что из этого можно отключить.

Скрипт ничего не меняет. Он читает список пакетов с устройства, раскладывает их по полочкам
(реклама/телеметрия, предустановленный мусор, железо проектора, стриминги) и собирает
готовый список деблоата под эту модель — с пометками, что за пакет и почему он в списке.

Использование:
    TV_HOST=192.168.1.50 python3 tools/discover.py                 показать разбор
    TV_HOST=192.168.1.50 python3 tools/discover.py --write         записать список
    TV_HOST=192.168.1.50 python3 tools/discover.py --write --safe  только очевидные (безопасные)
    python3 tools/discover.py --explain com.xgimi.tof              что делает пакет (нужен TV_HOST)

Куда пишет: lists/<модель>-debloat.txt  (плюс рядом .meta.json с выводом разведки)
"""
import json
import os
import re
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import adb  # noqa: E402

LISTS_DIR = os.path.join(os.path.dirname(HERE), "lists")

# Категории. Порядок важен: первое совпадение выигрывает, поэтому «неприкосновенное» стоит выше.
# Сопоставление идёт по СЛОВАМ имени пакета (com.xgimi.autokst -> {com, xgimi, autokst}), а не по
# подстроке: иначе "disclaimerCUstomization" попадает в «пульт» из-за последовательности rcu.
KEEP = [
    # Железо проектора: трапеция, фокус, питание, оптика. Отключение = «умный экран без картинки».
    ({"keystone", "kst", "autokst", "autokeystone", "trapezoid", "trapez", "focus", "autofocus",
      "tof", "zoom", "motor", "fan", "cool", "thermal", "lamp", "laser", "dmd", "optical",
      "lens", "brightness", "picture", "display", "color", "colour", "hdr", "gamma"},
     "железо проектора"),
    ({"bluetooth", "bt", "rcu", "remote", "remotecontrol", "bluekey", "keyboard", "inputmethod", "ime",
      "misckey", "mouse", "netflixmouse", "cursor", "pointer"}, "пульт/ввод"),
    ({"wifi", "wlan", "network", "ethernet", "connectivity", "tether", "wpa"}, "сеть"),
    ({"vold", "storage", "usb", "mount", "externalstorage", "mediaprovider"}, "хранение"),
    ({"audio", "dolby", "dts", "sound", "speaker", "volume", "mixer"}, "звук"),
    ({"hdmi", "tvinput", "inputplayer", "hdcp", "cec", "displayport", "mhl", "tuner"},
     "HDMI-вход"),
    ({"cast", "mirror", "miracast", "screencast", "chromecast", "mediashell", "airscreen", "starmirror"},
     "трансляция с телефона"),
    ({"provision", "setup", "wizard", "initial"}, "первичная настройка"),
    ({"launcher", "home", "tvhome", "launcherx", "tvlauncher", "jmlauncher"}, "домашний экран"),
    # Служба доступности, которая двигает курсор с пульта (NetflixMouse на DroidLogic/«Smart Project»).
    # Без неё курсор и выбор на экране могут перестать работать, поэтому — в «не трогать».
    ({"accessibility", "a11y"}, "курсор/управление с пульта"),
]

# Явно опасное даже в категориях «мусора»: системные сервисы, которые лучше не трогать.
NEVER = [
    # Оверлеи (RRO) и «заглушки» фреймворка: отключение ломает оформление и системные экраны.
    ({"overlay", "resoverlay", "frameworkpackagestubs", "framework", "rro"}, "оверлей системы"),
    ({"systemui", "shell", "settings", "phone", "permissioncontroller", "packageinstaller",
      "modulemetadata", "providers", "ext", "sdksandbox", "droidlogic", "inputdevices",
      "intentresolver", "certinstaller", "keychain", "localtransport", "networkstack",
      "captiveportallogin", "pacprocessor", "proxyhandler", "statementservice", "webview",
      "remoteprovisioner", "dynsystem", "managedprovisioning", "backupconfirm", "sharedstoragebackup",
      "wallpaperbackup", "mtp", "se", "vpndialogs", "safetycenter", "uwb", "nearby"},
     "системный сервис Android"),
    # Пакеты, которые на этой прошивке в system/priv-app и удаляются только с рутом.
    ({"superuser", "supersu", "magisk", "kinguser", "koushikdutta", "shellservice", "root"},
     "системный/рут-сервис (не трогать без понимания)"),
    ({"gms", "gsf", "vending"}, "Google Play"),
    ({"fallbackhome", "factorytest", "factorymenu", "hkcfac", "fac"}, "заводское/аварийное"),
]

# Что стоит отключить: реклама, телеметрия, мусор. Ключ — набор слов, значение — (категория, заметка).
DISABLE = [
    ({"adservices", "ondevicepersonalization", "federatedcompute", "partnersetup"},
     "реклама", "реклама Google: атрибуция и подбор"),
    ({"feedback", "bugreport", "bugreportsender", "logkit", "analytics", "statistic", "telemetry",
      "stats", "bi", "biometrics", "analyticscollector", "datalayer"},
     "телеметрия", "сбор статистики и отправка отчётов"),
    ({"tvrecommendations", "recommendations", "backdrop", "dreamx", "esticker", "waterfall"},
     "реклама", "рекламные ряды на главном экране"),
    ({"dreams", "screensaver"}, "заставка", "заставка экрана (на проекторе полезна против выгорания)"),
    ({"autoinstall", "autoinstalls", "onetimeinitializer", "partnercustomizer", "webcontent",
      "customizer", "customization"}, "мусор", "доливка партнёрских приложений"),
    ({"demo", "retail", "showroom", "promo", "advert", "ads"},
     "мусор", "демонстрационный режим магазина"),
    ({"games", "tvmusic", "videos", "films", "playgames"},
     "предустановлено", "приложение, которым на проекторе не пользуются"),
    ({"syncadapters", "printspooler", "healthconnect", "backuprestore", "calendar"},
     "предустановлено", "фоновая синхронизация и печать"),
    ({"talkback"}, "доступность", "экранный диктор (нужен только незрячим)"),
    ({"katniss", "assistant"}, "голос", "голосовой ассистент и пульт в телефоне"),
    ({"setupwraith", "tungsten"}, "настройка", "мастер первой настройки (осторожно: может владеть Wi-Fi)"),
    ({"factory", "factorymenu"}, "заводское", "заводское меню (не запускать — переключает режим)"),
    ({"disclaimer", "disclaimercustomization"}, "мусор", "экран отказа от ответственности"),
    ({"instructions", "guide", "manual", "help", "support", "customersupport", "smarthelp",
      "sonyselect", "messagebox", "dashboard"}, "мусор", "справка и реклама вендора"),
    ({"ota", "updater", "update"}, "обновления", "обновление прошивки (осторожно: без него нет новых версий)"),
    ({"aptoide", "aptoidetv", "softonic", "market", "store", "appstore", "appmarket"},
     "магазин", "сторонний магазин приложений"),
    ({"netflix", "amazonvideo", "disney", "ivi", "kinopoisk", "more", "okko", "wink",
      "appletv", "hbo", "paramount", "peacock", "primevideo", "spotify", "tidal", "youtube",
      "rutube", "hdrezkaapp", "hdrezka", "mxtech", "vlc", "megogo", "tvzavr", "amedia", "premier",
      "vk", "rutube"},
     "стриминг", "стриминговое приложение (отключать, только если не смотрите)"),
]

# Пакеты, которые часто встречаются у конкретных брендов и точно не нужны дома.
BRAND_SPECIFIC = {
    "xgimi": ({"doubanfm", "minitvfactory", "atvwifiverify"}, "мусор XGIMI"),
    "xiaomi": ({"analytics", "webcontent", "statistic"}, "мусор Xiaomi"),
    "formovie": ({"ad", "promo"}, "мусор Formovie"),
    "jmgo": ({"ad", "promo", "shop"}, "мусор JMGO"),
    "tcl": ({"bi", "guard", "messagebox", "dashboard", "ocean", "channelplus"}, "мусор TCL"),
}

# Слова, которые сами по себе не решают: «tv» есть в половине системных пакетов Android TV,
# «ad» — в com.android.*, «ext» — в android.ext.services. Совпадение ТОЛЬКО по ним не считается.
TOO_GENERIC = {"tv", "start", "more", "ads", "ad", "bi", "fac", "ext", "help", "guide", "update",
               "setup", "framework", "android", "com", "google"}


def tokens(pkg):
    return set(pkg.lower().split("."))


def _match(pkg, patterns):
    """Совпадает ли имя пакета с набором слов.

    Имя пакета сравнивается по сегментам, и совпадение должно быть хотя бы по ОДНОМУ
    осмысленному слову. Иначе com.android.tv.settings совпал бы с «tv» из набора стримингов,
    а com.android.providers.media — с «ext» из android.ext.services.
    """
    hit = tokens(pkg) & patterns
    return bool(hit - TOO_GENERIC)


def classify(pkg, brand):
    for patterns, why in NEVER:
        if _match(pkg, patterns):
            return ("never", why, "")
    for patterns, why in KEEP:
        if _match(pkg, patterns):
            return ("keep", why, "")
    brand_patterns = BRAND_SPECIFIC.get(brand)
    if brand_patterns and _match(pkg, brand_patterns[0]):
        return ("disable", brand_patterns[1], "мусор бренда")
    for patterns, cat, why in DISABLE:
        if _match(pkg, patterns):
            # «По желанию» — то, что зависит от вкуса владельца, а не от того, мусор это или нет.
            level = "optional" if cat in ("обновления", "голос", "стриминг", "настройка", "заставка") else "disable"
            return (level, f"{cat}: {why}", cat)
    return ("unknown", "", "")


def main(argv):
    host = os.environ.get("TV_HOST", "").strip()
    if not host:
        print("Не задан TV_HOST. Пример: TV_HOST=192.168.1.50 python3 tools/discover.py --write",
              file=sys.stderr)
        return 2
    port = int(os.environ.get("TV_PORT") or 5555)
    if ":" in host:
        host, p = host.rsplit(":", 1)
        port = int(p)
    write = "--write" in argv
    safe = "--safe" in argv
    explain = None
    if "--explain" in argv:
        i = argv.index("--explain")
        explain = argv[i + 1] if i + 1 < len(argv) else None

    dev = adb.AdbConnection(host, port, timeout=float(os.environ.get("TV_TIMEOUT") or 30))
    try:
        dev.connect()
        info = adb.probe_device(host, port)
        if "error" in info:
            print(f"ошибка: {info['error']}", file=sys.stderr)
            return 1
        brand = (info["brand"] or "").lower()

        if explain:
            out = dev.shell(f"dumpsys package {explain}").decode("utf-8", "replace")
            keep = [ln for ln in out.splitlines()
                    if re.search(r"(versionName|firstInstallTime|pkg=|codePath|"
                                 r"Activity|Service|Receiver|Provider|permission|enabled=)", ln)]
            print(f"=== {explain} ===")
            print("\n".join(keep[:80]) or "пакет не найден или dumpsys ничего не рассказал")
            return 0

        installed = sorted(
            ln.strip()[len("package:"):]
            for ln in dev.shell("pm list packages").decode("utf-8", "replace").splitlines()
            if ln.strip().startswith("package:")
        )
        disabled = {
            ln.strip()[len("package:"):]
            for ln in dev.shell("pm list packages -d --user 0").decode("utf-8", "replace").splitlines()
            if ln.strip().startswith("package:")
        }

        buckets = {"disable": [], "optional": [], "keep": [], "never": [], "unknown": []}
        for pkg in installed:
            level, why, cat = classify(pkg, brand)
            buckets[level].append((pkg, why))

        print(f"Проектор : {info['brand']} {info['model']} ({info['device']})")
        print(f"Android  : {info['android']} (SDK {info['sdk']})")
        print(f"Прошивка : {info['firmware']}")
        print(f"Пакетов  : {len(installed)}, уже отключено: {len(disabled)}\n")

        def show(title, items, hint=""):
            if not items:
                return
            print(f"── {title} ({len(items)}) " + "─" * max(0, 50 - len(title)))
            if hint:
                print(f"   {hint}")
            for pkg, why in items:
                print(f"   {pkg}" + (f"\n       {why}" if why else ""))

        show("Безопасно отключить", buckets["disable"])
        show("Отключить по желанию", buckets["optional"],
             "проверьте, что этим пользуетесь/не пользуетесь")
        show("НЕ трогать — железо и системы проектора", buckets["keep"])
        show("НЕ трогать — ядро", buckets["never"])
        show("Неизвестные (решать вам)", buckets["unknown"],
             "посмотрите: python3 tools/discover.py --explain <package>")

        if not write:
            n = len(buckets["disable"]) + (0 if safe else len(buckets["optional"]))
            print(f"\nЗаписать список ({n} пакетов): "
                  f"TV_HOST={host} python3 tools/discover.py --write{' --safe' if safe else ''}")
            return 0

        chosen = buckets["disable"] + ([] if safe else buckets["optional"])
        safe_model = re.sub(r"[^A-Za-z0-9]+", "-", f"{info['brand']}-{info['model']}").strip("-")
        os.makedirs(LISTS_DIR, exist_ok=True)
        path = os.path.join(LISTS_DIR, f"{safe_model}-debloat.txt")
        with open(path, "w", encoding="utf-8") as f:
            f.write(f"# Список деблоата для {info['brand']} {info['model']} ({info['device']})\n")
            f.write(f"# Android {info['android']} (SDK {info['sdk']}), прошивка {info['firmware']}\n")
            f.write(f"# Собран tools/discover.py {time.strftime('%Y-%m-%d %H:%M')} по факту установленного.\n")
            f.write("# Применение: python3 tools/debloat.py " + os.path.relpath(path) + "\n")
            f.write("# Возврат:    python3 tools/restore.py\n#\n")
            f.write("# Не отключено (проверено разведкой):\n")
            for pkg, why in buckets["keep"]:
                f.write(f"#   {pkg}  — {why}\n")
            f.write("\n")
            for pkg, why in chosen:
                f.write(f"{pkg}  # {why}\n")
        meta = {
            "device": info, "brand": brand, "categories": {
                k: [p for p, _ in v] for k, v in buckets.items()},
            "written": path,
        }
        with open(path.replace(".txt", ".meta.json"), "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False, indent=2)
        print(f"\nЗаписан список: {path}")
        if any("setupwraith" in pkg for pkg, _ in chosen):
            print("ВНИМАНИЕ: в списке есть мастер первой настройки — он может владеть Wi-Fi.")
            print("tools/debloat.py проверит это и пропустит его, если есть риск.")
        print(f"Дальше:\n  python3 tools/debloat.py {os.path.relpath(path)} --dry-run")
        return 0
    except adb.AdbError as e:
        print(f"ошибка: {e}", file=sys.stderr)
        return 1
    finally:
        dev.close()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
