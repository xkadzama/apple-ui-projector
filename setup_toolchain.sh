#!/usr/bin/env bash
# Установка всего, что нужно для сборки лончера: JDK 17 + Android SDK (platform 34, build-tools 34).
# Запускать один раз. Ничего не меняет в системе, кроме установки этих двух пакетов.
#
#   ./setup_toolchain.sh
#
# После установки соберите лончер:  TV_HOST=<ip> ./launcher/install_launcher.sh
set -euo pipefail

need() { printf '  %-14s %s\n' "$1" "$2"; }

echo "Проверяю, что уже есть:"
JDK=""
for cand in /opt/homebrew/opt/openjdk@17 /usr/local/opt/openjdk@17; do
  [ -x "$cand/bin/javac" ] && JDK="$cand"
done
if [ -z "$JDK" ]; then
  JH=$(/usr/libexec/java_home -v 17 2>/dev/null || true)
  # /usr/libexec/java_home -v 17 означает «17 или новее»: проверяем, что это действительно 17
  if [ -n "$JH" ] && "$JH/bin/java" -version 2>&1 | grep -q 'version "17\.'; then JDK="$JH"; fi
fi
need "JDK 17" "${JDK:-нет}"

SDK=""
for cand in "$HOME/Library/Android/sdk" "$HOME/Android/Sdk" \
            /opt/homebrew/share/android-commandlinetools \
            /usr/local/share/android-commandlinetools; do
  [ -d "$cand" ] && SDK="$cand"
done
need "Android SDK" "${SDK:-нет}"

if [ -n "$JDK" ] && [ -n "$SDK" ] && [ -d "$SDK/platforms/android-34" ]; then
  echo "Всё на месте."
else
  if ! command -v brew >/dev/null; then
    echo
    echo "Homebrew не найден. Установите JDK 17 и Android command-line tools вручную:"
    echo "  JDK 17       https://adoptium.net/temurin/releases/?version=17"
    echo "  Android SDK  https://developer.android.com/studio#command-tools"
    exit 1
  fi
  echo
  echo "Ставлю через Homebrew (это займёт время: JDK ~190 МБ, SDK ~65 МБ)…"
  HOMEBREW_NO_AUTO_UPDATE=1 brew install openjdk@17 android-commandlinetools
  JDK=/opt/homebrew/opt/openjdk@17
  [ -x "$JDK/bin/javac" ] || JDK=/usr/local/opt/openjdk@17
  SDK=/opt/homebrew/share/android-commandlinetools
  [ -d "$SDK" ] || SDK=/usr/local/share/android-commandlinetools
fi

echo
echo "Ставлю platform 34 и build-tools 34 (их ждёт launcher/app/build.gradle)…"
export JAVA_HOME="$JDK"
SDKMANAGER="$SDK/cmdline-tools/latest/bin/sdkmanager"
[ -x "$SDKMANAGER" ] || SDKMANAGER="$(command -v sdkmanager || true)"
if [ -z "$SDKMANAGER" ] || [ ! -x "$SDKMANAGER" ]; then
  echo "Не нашёл sdkmanager в $SDK. Откройте Android Studio → SDK Manager и поставьте"
  echo "  Android 14 (API 34) и Build-Tools 34.0.0"
  exit 1
fi
yes | "$SDKMANAGER" --sdk_root="$SDK" --licenses >/dev/null 2>&1 || true
"$SDKMANAGER" --sdk_root="$SDK" "platforms;android-34" "build-tools;34.0.0"

echo
echo "Готово. Проверка:"
echo "  JAVA_HOME=$JAVA_HOME"
echo "  ANDROID_HOME=$SDK"
"$JAVA_HOME/bin/java" -version 2>&1 | head -2
echo
echo "Дальше:  TV_HOST=<ip проектора> ./launcher/install_launcher.sh"
