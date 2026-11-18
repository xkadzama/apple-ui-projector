#!/usr/bin/env bash
# Сборка и установка домашнего экрана «Дом» на проектор.
#
# Что делает по порядку:
#   1. проверяет, что проектор отвечает и его версия Android не ниже minSdk лончера;
#   2. собирает APK (Gradle) или берёт уже собранный при SKIP_BUILD=1;
#   3. ставит APK на проектор;
#   4. назначает его домашним экраном;
#   5. проверяет, что Home действительно открывает его, и только потом
#      (по желанию) отключает стоковый лаунчер — и откатывается, если Home не переключился.
#
# Использование:
#   TV_HOST=192.168.1.50 ./install_launcher.sh
#   TV_HOST=192.168.1.50 SKIP_BUILD=1 ./install_launcher.sh      поставить уже собранный APK
#   TV_HOST=192.168.1.50 STOCK_LAUNCHER=none ./install_launcher.sh   не трогать стоковый лаунчер
#   TV_HOST=192.168.1.50 HOME_KEY_SERVICE=1 ./install_launcher.sh    вернуть кнопки пульта
set -euo pipefail
cd "$(dirname "$0")"

: "${TV_HOST:?задайте TV_HOST — IP проектора, например: TV_HOST=192.168.1.50 ./install_launcher.sh}"
PY=python3
TOOLS="$(cd ../tools && pwd)"
APK="app/build/outputs/apk/release/app-release.apk"
PKG="com.gathin.tvhome"
HOME_KEY_SERVICE="${HOME_KEY_SERVICE:-0}"
STOCK_LAUNCHER="${STOCK_LAUNCHER:-auto}"

tv() { TV_HOST="$TV_HOST" TV_PORT="${TV_PORT:-5555}" "$PY" -I "$TOOLS/adb.py" "$@"; }

echo "== 1. Проверяю проектор =="
tv --probe || { echo "Проектор не отвечает. Включена ли отладка по ADB? (python3 ../tools/adb.py --discover)"; exit 1; }

MIN_SDK=$(sed -n 's/^[[:space:]]*minSdk[[:space:]][[:space:]]*\([0-9]\{1,3\}\)[[:space:]]*$/\1/p' app/build.gradle | head -n 1)
TV_SDK=$(tv 'getprop ro.build.version.sdk' | tr -dc '0-9')
if [ -n "$MIN_SDK" ] && [ -n "$TV_SDK" ] && [ "$TV_SDK" -lt "$MIN_SDK" ]; then
  echo "ОТКАЗ: проектор на SDK $TV_SDK, а лончер собран с minSdk $MIN_SDK."
  echo "pm install ответит INSTALL_FAILED_OLDER_SDK. Ничего не установлено и не изменено."
  exit 1
fi
echo "SDK проектора: $TV_SDK — подходит (minSdk $MIN_SDK)."

echo
echo "== 2. Сборка =="
if [ "${SKIP_BUILD:-}" = "1" ]; then
  [ -f "$APK" ] || { echo "SKIP_BUILD=1, но $APK нет. Соберите: ./gradlew assembleRelease"; exit 1; }
  echo "Беру готовый APK: $APK"
else
  export JAVA_HOME="${JAVA_HOME:-$(/usr/libexec/java_home -v 17 2>/dev/null || echo /opt/homebrew/opt/openjdk@17)}"
  export ANDROID_HOME="${ANDROID_HOME:-/opt/homebrew/share/android-commandlinetools}"
  export PATH="$JAVA_HOME/bin:$PATH"
  [ -f local.properties ] || echo "sdk.dir=$(printf '%s' "$ANDROID_HOME" | tr '\\' '/')" > local.properties
  echo "JAVA_HOME=$JAVA_HOME"
  echo "ANDROID_HOME=$ANDROID_HOME"
  ./gradlew assembleRelease --console=plain
fi
ls -lh "$APK"

echo
echo "== 3. Установка =="
# APK небольшой (сотни КБ), но push надёжнее, чем `pm install` по стриму на медленном Wi-Fi проектора.
tv --push "$APK" /data/local/tmp/tvhome.apk
tv 'pm install -r -d /data/local/tmp/tvhome.apk'
tv 'rm -f /data/local/tmp/tvhome.apk'
if ! tv 'pm list packages' | grep -qx "package:$PKG"; then
  echo "ОТКАЗ: пакет $PKG не появился на устройстве. Установка не удалась."
  exit 1
fi
echo "Установлен: $PKG"

# Bluetooth-кнопка: без этого разрешения лончер не видит сопряжённые устройства
# и иконка остаётся серой, даже когда что-то подключено.
tv "pm grant $PKG android.permission.BLUETOOTH_CONNECT" || true

if [ "$HOME_KEY_SERVICE" = "1" ]; then
  echo "Разрешаю лончеру перехватывать кнопки пульта (HomeKeyService)…"
  tv "pm grant $PKG android.permission.WRITE_SECURE_SETTINGS" || true
fi

echo
echo "== 4. Назначаю домашним экраном =="
# Кто сейчас владеет Home — спрашиваем у устройства, а не угадываем.
CUR_HOME=$(tv 'cmd package resolve-activity --brief -a android.intent.action.MAIN -c android.intent.category.HOME' | tail -n 1 | tr -d '\r' | cut -d/ -f1 || true)
echo "Сейчас Home принадлежит: ${CUR_HOME:-не определился}"
tv "cmd package set-home-activity --user 0 $PKG/.MainActivity" || true
tv "cmd package set-home-activity --user 0 $PKG/com.gathin.tvhome.MainActivity" || true
# Часть прошивок (DroidLogic/Amlogic, как на Smart Project) держит выбор лаунчера в своём prop
# и при загрузке запускает пакет оттуда, игнорируя set-home-activity. Если такой prop есть —
# переписываем его, иначе после перезагрузки вернётся стоковый экран.
VENDOR_HOME_PROP="persist.sys.home.selectpkg"
if [ -n "$(tv "getprop $VENDOR_HOME_PROP" | tr -d '\r')" ]; then
  echo "Прошивка хранит выбор лаунчера в $VENDOR_HOME_PROP — переписываю на $PKG."
  tv "setprop $VENDOR_HOME_PROP $PKG" || true
fi
# Некоторые прошивки запускают свой лаунчер по собственной категории (например HOME.JM),
# поэтому одного set-home-activity мало: гасим чужой домашний процесс, чтобы Home взял новый.
for OTHER in $STOCK_LAUNCHER com.jm.jmlauncher; do
  [ -n "$OTHER" ] && [ "$OTHER" != "$PKG" ] && tv "am force-stop $OTHER" >/dev/null 2>&1 || true
done
tv 'input keyevent KEYCODE_HOME' >/dev/null || true
sleep 3

echo
echo "== 5. Проверяю, что Home открывает новый экран =="
FOCUS=$(tv 'dumpsys activity activities' | grep -E 'mFocusedApp|topResumedActivity|mResumedActivity' | head -3 || true)
echo "$FOCUS"
if printf '%s' "$FOCUS" | grep -q "$PKG"; then
  echo "Готово: Home открывает $PKG."
else
  echo "ВНИМАНИЕ: Home пока не переключился на $PKG."
  echo "Нажмите Home на пульте проектора. Если появится выбор домашнего экрана — выберите «Дом»."
  echo "Стоковый лаунчер НЕ отключён: пока Home не подтверждён, это единственный способ вернуться."
fi

if [ "$STOCK_LAUNCHER" != "none" ] && printf '%s' "$FOCUS" | grep -q "$PKG"; then
  echo
  echo "== 6. Отключаю стоковый лаунчер (обратимо) =="
  KNOWN="com.google.android.tvlauncher com.google.android.apps.tv.launcherx com.google.android.leanbacklauncher com.mitv.tvhome"
  TARGET="${STOCK_LAUNCHER}"
  if [ "$TARGET" = "auto" ]; then
    if printf '%s' " $KNOWN " | grep -q " ${CUR_HOME} "; then TARGET="$CUR_HOME"; else TARGET=""; fi
  fi
  if [ -z "$TARGET" ]; then
    echo "Стоковый лаунчер не опознан ($CUR_HOME) — оставляю включённым, это безопаснее."
  else
    tv "pm disable-user --user 0 $TARGET" || true
    sleep 2
    tv 'input keyevent KEYCODE_HOME' >/dev/null || true
    sleep 2
    FOCUS2=$(tv 'dumpsys activity activities' | grep -E 'mFocusedApp|topResumedActivity|mResumedActivity' | head -3 || true)
    if printf '%s' "$FOCUS2" | grep -q "$PKG"; then
      echo "Стоковый лаунчер $TARGET отключён, Home по-прежнему на «Дом»."
    else
      echo "Home ушёл с «Дом» после отключения $TARGET — возвращаю всё назад."
      tv "pm enable --user 0 $TARGET" || true
      tv "cmd package set-home-activity --user 0 $PKG/.MainActivity" || true
      echo "Стоковый лаунчер снова включён. Ничего не потеряно."
    fi
  fi
fi

echo
echo "== Готово =="
echo "Настройки вида: удерживайте OK на плитке «Настройки» (или откройте приложение «Дом»)."
echo "Поменять набор приложений в доке: launcher/app/src/main/res/values/config.xml,"
echo "затем пересобрать и запустить этот скрипт снова."
echo "Вернуть стоковый экран: python3 ../tools/restore.py --home"
