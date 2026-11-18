#!/usr/bin/env python3
"""Минимальный ADB-клиент по TCP для Android TV / проектора — без бинарника adb и без сторонних пакетов.

Зачем свой клиент:
  * на macOS 26 стоковый `adb` получает "No route to host", даже когда порт открыт (Local Network privacy);
  * проекторы часто слушают ADB не на 5555, а на другом порту (например, 5877);
  * нужен один вход, который умеет найти устройство в сети, снять бэкап и проверить результат.

Использование:
    python3 tools/adb.py --discover                     найти проектор в локальной сети
    TV_HOST=192.168.1.50 python3 tools/adb.py --probe    модель, версия Android, прошивка
    TV_HOST=192.168.1.50 python3 tools/adb.py 'pm list packages'
    TV_HOST=192.168.1.50 python3 tools/adb.py --batch < commands.txt
    TV_HOST=192.168.1.50 python3 tools/adb.py --screenshot shot.png [DPAD_DOWN ...]
    TV_HOST=192.168.1.50 python3 tools/adb.py --push local.txt /sdcard/local.txt
    TV_HOST=192.168.1.50 python3 tools/adb.py --pull /sdcard/x.png x.png
    TV_HOST=192.168.1.50 python3 tools/adb.py --reboot

Переменные окружения:
    TV_HOST      IP или IP:порт проектора (обязательно для всех команд, кроме --discover)
    TV_PORT      порт ADB, если он не указан в TV_HOST и не 5555 (по умолчанию 5555)
    TV_KEY       путь к приватному ключу ADB (по умолчанию ~/.projector/adbkey)
    TV_TIMEOUT   таймаут сокета в секундах (по умолчанию 30)

Коды выхода:
    0   команда выполнена (ответ устройства напечатан как есть)
    1   команда выполнена, но результат не получен: пустой скриншот, оборванная передача
    2   отказ до обращения к устройству: нет TV_HOST, неизвестный флаг, неверный путь
"""
import io
import os
import re
import shutil
import socket
import struct
import subprocess
import sys
import time
import zlib

ADB_CNXN = 0x4E584E43
ADB_AUTH = 0x48545541
ADB_OPEN = 0x4E45504F
ADB_OKAY = 0x59414B4F
ADB_CLSE = 0x45534C43
ADB_WRTE = 0x45545257
ADB_STLS = 0x534C5453  # не используется: TLS-рукопожатие (Wireless debugging) требует pairing

ADB_VERSION = 0x01000000
MAXDATA = 256 * 1024

DEFAULT_PORTS = (5555, 5877, 5037)
PROJECTOR_HINT = re.compile(
    r"projector|projektor|dlp|laser|beamer|xgimi|formovie|jmgo|wanbo|magcubic|dcolor|"
    r"byintek|hotack|hy300|hy320|nebula|anker|alwtniet|nonete|mogo|horizon|mprojm05",
    re.I,
)


class AdbError(RuntimeError):
    pass


# --------------------------------------------------------------------------- ключи


def key_path():
    return os.environ.get("TV_KEY") or os.path.expanduser("~/.projector/adbkey")


def get_signer():
    """Загружает или создаёт ключ ADB (RSA 2048) и возвращает объект с .sign() и .public_blob()."""
    path = key_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if not os.path.exists(path):
        _generate_key(path)
    return _RsaSigner(path)


def _generate_key(path):
    """Создаёт пару ключей. openssl есть на macOS и почти на любом Linux; если нет — понятная ошибка."""
    out = path + ".pub"
    openssl = shutil.which("openssl")
    if not openssl:
        raise AdbError(
            "нужен openssl, чтобы создать ключ ADB.\n"
            "macOS: он уже есть (/usr/bin/openssl). Linux: apt install openssl."
        )
    subprocess.run(
        [openssl, "genrsa", "-out", path, "2048"],
        check=True, capture_output=True,
    )
    subprocess.run(
        [openssl, "rsa", "-in", path, "-pubout", "-out", out],
        check=True, capture_output=True,
    )
    os.chmod(path, 0o600)


class _RsaSigner:
    """Подпись AUTH-токена через openssl (PKCS#1 v1.5, SHA-1) — ровно так, как это делает adb."""

    def __init__(self, path):
        self.path = path
        self.openssl = shutil.which("openssl")
        if not self.openssl:
            raise AdbError("нужен openssl для подписи AUTH-токена (см. сообщение выше)")
        self._pub = self._public_n_e()

    def _public_n_e(self):
        raw = self._run(["rsa", "-in", self.path, "-noout", "-text"])
        text = raw.decode("ascii", "replace")
        n_hex = _grab_hex_block(text, "modulus")
        # openssl печатает показатель одной строкой: "publicExponent: 65537 (0x10001)"
        m = re.search(r"publicExponent:\s*\d+\s*\(0x([0-9a-fA-F]+)\)", text)
        if not m:
            raise AdbError("не удалось прочитать publicExponent из ключа ADB")
        return (int(n_hex, 16), int(m.group(1), 16))

    def _run(self, args, text=True):
        p = subprocess.run(
            [self.openssl] + args, capture_output=True, check=True,
        )
        return p.stdout if text else p.stdout

    def sign(self, token):
        p = subprocess.run(
            [self.openssl, "dgst", "-sha1", "-sign", self.path],
            input=token, capture_output=True, check=True,
        )
        return p.stdout

    def public_blob(self):
        """Тот же формат, что adb: struct RSAPublicKey (len, n0inv, n, rr, e) + подпись."""
        n, e = self._pub
        n0 = 32  # 2048 бит / 8 = 256 байт
        n_bytes = n.to_bytes(256, "little")
        n0inv = _modinv(n & 0xFFFFFFFF, 1 << 32)
        rr = pow(2, 2048 * 2, n).to_bytes(256, "little")
        e_bytes = e.to_bytes(4, "little")
        body = b"".join([
            struct.pack("<I", 64 + 256 + 256 + 4),
            struct.pack("<I", n0inv),
            n_bytes,
            rr,
            e_bytes,
        ])
        return body + self.sign(body)


def _modinv(a, m):
    return pow(a, -1, m)


def _grab_hex_block(text, name):
    m = re.search(name + r":\s*\n((?:\s+[0-9a-f:]+\n)+)", text)
    if not m:
        raise AdbError("не удалось прочитать публичный ключ (openssl вернул неожиданный формат)")
    return m.group(1).replace(":", "").replace(" ", "").replace("\n", "")


# --------------------------------------------------------------------------- транспорт


class AdbConnection:
    """Одно соединение с adbd: CNXN/AUTH, затем OPEN/OKAY/WRTE/CLSE для одной команды за раз."""

    def __init__(self, host, port=5555, timeout=30.0, log=None):
        self.host = host
        self.port = int(port)
        self.timeout = timeout
        self.log = log or (lambda *_: None)
        self.sock = None
        self.local_id = 0
        self.remote_id = 0

    # -- низкий уровень

    def _send(self, cmd, arg0, arg1, data=b""):
        header = struct.pack("<6I", cmd, arg0, arg1, len(data), _checksum(data), cmd ^ 0xFFFFFFFF)
        self.sock.sendall(header + data)

    def _recv_exact(self, n):
        buf = b""
        while len(buf) < n:
            chunk = self.sock.recv(n - len(buf))
            if not chunk:
                raise AdbError("устройство закрыло соединение")
            buf += chunk
        return buf

    def _read_packet(self):
        header = self._recv_exact(24)
        cmd, arg0, arg1, length, _crc, _magic = struct.unpack("<6I", header)
        data = self._recv_exact(length) if length else b""
        return cmd, arg0, arg1, data

    # -- рукопожатие

    def connect(self):
        self.log(f"подключаюсь к {self.host}:{self.port} …")
        try:
            self.sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
        except OSError as e:
            raise AdbError(
                f"не удалось подключиться к {self.host}:{self.port} — {e}.\n"
                "Проверьте: проектор включён, `Отладка по ADB` включена в настройках, "
                "компьютер в той же сети, IP верный (см. --discover)."
            )
        self.sock.settimeout(self.timeout)
        self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        signer = get_signer()
        payload = b"host::\x00"
        self._send(ADB_CNXN, ADB_VERSION, MAXDATA, payload)
        auth_round = 0
        while True:
            cmd, _, _, data = self._read_packet()
            if cmd == ADB_CNXN:
                banner = data.split(b"\x00")[0].decode("utf-8", "replace")
                self.log(f"устройство ответило: {banner.split(';')[0]}")
                return banner
            if cmd == ADB_AUTH:
                auth_round += 1
                if auth_round == 1:
                    # Первый токен: подписываем ключом, который устройство могло уже запомнить.
                    self._send(ADB_AUTH, 2, 0, signer.sign(data))  # AUTH_SIGNATURE
                    continue
                # Второй токен: ключ неизвестен. Отправляем публичный ключ — именно на этом
                # шаге на экране проектора появляется «Разрешить отладку по USB?».
                self._send(ADB_AUTH, 3, 0, signer.public_blob())  # AUTH_RSAPUBLICKEY
                self.log(
                    "\n*** НА ЭКРАНЕ ПРОЕКТОРА ПОЯВИТСЯ ЗАПРОС «Разрешить отладку?» — "
                    "нажмите «Разрешить» пультом. Жду… ***"
                )
                continue
            if cmd == ADB_STLS:
                raise AdbError(
                    "устройство требует зашифрованное соединение (Wireless debugging с pairing).\n"
                    "Включите обычную «Отладка по ADB» в настройках проектора — она работает по 5555."
                )
            raise AdbError(f"неожиданный пакет при подключении: cmd=0x{cmd:08x}")

    def close(self):
        if self.sock:
            try:
                self.sock.close()
            except OSError:
                pass
            self.sock = None

    # -- одна шелл-команда

    def shell(self, command, timeout=None):
        """Выполняет команду и возвращает её вывод (bytes). Timeout — общий на всю команду.

        Пакеты устройства приходят с ЕГО идентификаторами (arg0 — id удалённого конца). Прошивки
        вроде t950s_be311 присылают запоздавшие CLSE/OKAY от уже закрытых команд, и если читать
        «следующий пакет» без проверки, ответ предыдущей команды достаётся текущей: вывод
        съезжает на одну команду. Поэтому ждём пакеты только своего канала и чужие отбрасываем.
        """
        self.local_id = (self.local_id + 1) & 0xFFFFFFFF
        lid = self.local_id
        dest = b"shell:" + command.encode("utf-8") + b"\x00"
        self._send(ADB_OPEN, lid, 0, dest)
        deadline = time.time() + (timeout or self.timeout)
        out = io.BytesIO()
        remote = None
        while True:
            if time.time() > deadline:
                raise AdbError("команда не уложилась в таймаут; устройство не ответило")
            try:
                cmd, a0, a1, data = self._read_packet()
            except socket.timeout:
                break  # молчание после вывода — считаем, что команда закончилась
            if cmd == ADB_OKAY:
                # OKAY(a1=<наш id>) — открытие канала. Всё остальное — чужая запоздалая запись.
                if a1 != lid:
                    continue
                remote = a0
                continue
            if cmd == ADB_WRTE:
                if a1 != lid:      # пакет другого канала
                    continue
                out.write(data)
                self._send(ADB_OKAY, lid, a0)
                continue
            if cmd == ADB_CLSE:
                if a1 != lid:
                    continue
                self._send(ADB_CLSE, lid, a0)   # подтверждаем закрытие, иначе устройство
                self.remote_id = None           # держит канал и путает следующие команды
                break
            raise AdbError(f"неожиданный пакет в shell: cmd=0x{cmd:08x}")
        return out.getvalue()


def _checksum(data):
    return sum(data) & 0xFFFFFFFF


# --------------------------------------------------------------------------- поиск устройства


def local_subnet():
    """Возвращает (адрес_в_локальной_сети, маска) или None, если в сети не разобраться."""
    try:
        import fcntl
        import termios
        import array
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        ifreq = struct.pack("256s", b"en0")
        try:
            res = fcntl.ioctl(s.fileno(), 0x8915, ifreq)  # SIOCGIFADDR
            return socket.inet_ntoa(res[20:24])
        finally:
            s.close()
    except Exception:
        pass
    return None


def discover(ports=DEFAULT_PORTS, timeout=0.4, subnet=None, progress=None):
    """Сканирует /24 подсеть на открытые порты ADB. Возвращает список (ip, port)."""
    subnet = subnet or local_subnet()
    if not subnet:
        return []
    base = subnet.rsplit(".", 1)[0]
    found = []

    def probe(i, port):
        ip = f"{base}.{i}"
        s = socket.socket()
        s.settimeout(timeout)
        try:
            if s.connect_ex((ip, port)) == 0:
                return (ip, port)
        finally:
            s.close()
        return None

    import concurrent.futures
    with concurrent.futures.ThreadPoolExecutor(max_workers=96) as ex:
        futs = [ex.submit(probe, i, p) for i in range(1, 255) for p in ports]
        for f in concurrent.futures.as_completed(futs):
            r = f.result()
            if r:
                found.append(r)
                if progress:
                    progress(r)
    return sorted(found)


def probe_device(host, port):
    """Подключается и возвращает словарь с model/device/release/sdk/firmware/brand или None."""
    conn = AdbConnection(host, port, timeout=12)
    try:
        banner = conn.connect()
        out = conn.shell("getprop")
        props = {}
        for line in out.decode("utf-8", "replace").splitlines():
            m = re.match(r"\[([^\]]+)\]: \[(.*)\]", line)
            if m:
                props[m.group(1)] = m.group(2)
        return {
            "banner": banner,
            "brand": props.get("ro.product.brand", ""),
            "model": props.get("ro.product.model", ""),
            "device": props.get("ro.product.device", ""),
            "android": props.get("ro.build.version.release", ""),
            "sdk": props.get("ro.build.version.sdk", ""),
            "firmware": props.get("ro.build.display.id", ""),
            "abi": props.get("ro.product.cpu.abi", ""),
            "adb_secure": props.get("ro.adb.secure", ""),
        }
    except AdbError as e:
        return {"error": str(e)}
    finally:
        conn.close()


def is_projector(info):
    blob = " ".join(str(v) for v in info.values())
    return bool(PROJECTOR_HINT.search(blob))


# --------------------------------------------------------------------------- экран


def screenshot(dev, local, keys=(), wait=0.0):
    """Нажимает клавиши (если заданы), делает снимок на устройстве и забирает его."""
    for key in keys:
        dev.shell(f"input keyevent {key}")
        time.sleep(wait or 0.35)
    remote = "/sdcard/_proj_shot.png"
    dev.shell(f"screencap -p {remote}")
    data = dev.pull_bytes(remote)
    dev.shell(f"rm -f {remote}")
    if not data:
        raise AdbError("скриншот пустой: так бывает в HDMI/Netflix на некоторых прошивках")
    # некоторые вендоры дописывают строку в начало PNG — находим сигнатуру
    idx = data.find(b"\x89PNG\r\n\x1a\n")
    if idx > 0:
        data = data[idx:]
    with open(local, "wb") as f:
        f.write(data)
    return local


def _open_sync(dev):
    dev.local_id = (dev.local_id + 1) & 0xFFFFFFFF
    dev._send(ADB_OPEN, dev.local_id, 0, b"sync:\x00")
    deadline = time.time() + dev.timeout
    while True:
        if time.time() > deadline:
            raise AdbError("не удалось открыть sync-канал")
        cmd, a0, _a1, _d = dev._read_packet()
        if cmd == ADB_OKAY:
            dev.remote_id = a0   # OKAY(remote, local): удалённый конец — arg0
            dev._sync_buf = b""  # новый канал — старый буфер не годится
            return
        if cmd == ADB_CLSE:
            raise AdbError("устройство закрыло sync-канал")


def _send_sync(dev, data):
    """Отправляет блок sync. Подтверждение OKAY не ждём и не съедаем его здесь.

    Sync — это поток байт внутри канала: устройство отвечает OKAY на наш WRTE, а следом
    присылает собственный блок (DATA/DONE/OKAY/FAIL). Если съесть OKAY внутри отправки,
    следующий блок останется без подтверждения, и устройство будет ждать вечно —
    на реальном проекторе это выглядело как «pull зависает на 160 КБ».
    Поэтому OKAY пропускается в _read_sync, где он и приходит.
    """
    dev._send(ADB_WRTE, dev.local_id, dev.remote_id, data)


def _read_sync(dev):
    """Читает очередной блок sync, подтверждая каждый ADB-пакет.

    Содержимое sync — непрерывный поток блоков (8-байтовый заголовок: 4 байта типа + 4 длины).
    Один ADB-пакет может содержать несколько блоков или часть блока, поэтому блоки собираются
    из буфера, а не считаются «один WRTE = один блок».
    """
    buf = getattr(dev, "_sync_buf", b"")
    while len(buf) < 8:
        buf += _read_sync_chunk(dev)
    kind = buf[:4]
    length = struct.unpack("<I", buf[4:8])[0]
    while len(buf) < 8 + length:
        buf += _read_sync_chunk(dev)
    payload = buf[8:8 + length]
    dev._sync_buf = buf[8 + length:]
    return kind, payload


def _read_sync_chunk(dev):
    """Читает один ADB-пакет sync-канала и подтверждает его."""
    while True:
        cmd, a0, _a1, data = dev._read_packet()
        if cmd == ADB_WRTE:
            dev._send(ADB_OKAY, dev.local_id, a0)
            return data
        if cmd == ADB_OKAY:
            continue
        if cmd == ADB_CLSE:
            raise AdbError("устройство закрыло канал во время передачи")
        raise AdbError(f"неожиданный пакет при передаче: cmd=0x{cmd:08x}")


def _read_sync_response(dev):
    """Читает ответ sync-сервера на запись: "OKAY" (без длины), "FAIL"+len+текст или "DONE"."""
    while True:
        cmd, a0, _a1, data = dev._read_packet()
        if cmd == ADB_WRTE:
            dev._send(ADB_OKAY, dev.local_id, a0)
            if data[:4] == b"OKAY":
                return b"OKAY", b""
            if data[:4] == b"FAIL":
                length = struct.unpack("<I", data[4:8])[0]
                return b"FAIL", data[8:8 + length]
            if data[:4] == b"DONE":
                return b"DONE", data[4:]
            return data[:4], data[4:]
        if cmd == ADB_OKAY:
            continue
        if cmd == ADB_CLSE:
            raise AdbError("устройство закрыло канал при записи")
        raise AdbError(f"неожиданный пакет при записи: cmd=0x{cmd:08x}")


def _pull_bytes(self, remote):
    _open_sync(self)
    _send_sync(self, b"RECV" + struct.pack("<I", len(remote.encode())) + remote.encode())
    out = b""
    while True:
        kind, payload = _read_sync(self)
        if kind == b"DATA":
            out += payload
        elif kind == b"DONE":
            return out
        elif kind == b"FAIL":
            # файла нет: adb в этом случае отдаёт пустой DONE, а не ошибку
            return b""


AdbConnection.pull_bytes = _pull_bytes


SYNC_CHUNK = 64 * 1024   # adbd не любит один огромный WRTE: режем, как это делает настоящий adb


def push_bytes(dev, remote, blob, mode=0o644):
    """Заливает файл на устройство.

    Один SEND-блок может содержать данные не целиком: adbd принимает поток и подтверждает
    каждый пакет. Для файлов больше 64 КБ данные режутся на части — иначе устройство
    перестаёт отвечать на середине (так и было с APK 575 КБ).
    """
    _open_sync(dev)
    # Формат этого adbd: "путь,режим" (ID_SEND_V1), а не просто путь.
    target = f"{remote},{mode}".encode()
    _send_sync(dev, b"SEND" + struct.pack("<I", len(target)) + target)
    # Данные идут фреймами "DATA"+len+байты; сырой поток этот adbd отвергает
    # ("invalid data message").
    for i in range(0, len(blob), SYNC_CHUNK):
        piece = blob[i:i + SYNC_CHUNK]
        _send_sync(dev, b"DATA" + struct.pack("<I", len(piece)) + piece)
    _send_sync(dev, b"DONE" + struct.pack("<I", 0))
    kind, payload = _read_sync_response(dev)
    if kind == b"FAIL":
        raise AdbError("устройство отказало в записи: "
                       + payload.decode("utf-8", "replace"))


# --------------------------------------------------------------------------- CLI


def main(argv):
    if not argv:
        print(__doc__.strip())
        return 2
    cmd = argv[0]
    if cmd in ("-h", "--help"):
        print(__doc__.strip())
        return 0

    if cmd == "--discover":
        subnet = argv[1] if len(argv) > 1 else None
        print("Сканирую локальную сеть на порты ADB (это занимает до минуты)…")
        found = discover(subnet=subnet, progress=lambda r: print(f"  найден порт {r[1]} на {r[0]}"))
        if not found:
            print("\nНичего не найдено.")
            print("Проверьте на проекторе: Настройки → Система → О проекторе → 7 раз OK на «Сборка»,")
            print("затем Для разработчиков → «Отладка по ADB» / «Отладка по сети» = вкл.")
            return 1
        print("\nПроверяю, что это ADB и что за устройство:")
        for host, port in found:
            info = probe_device(host, port)
            if "error" in info:
                print(f"  {host}:{port} — порт открыт, но ADB не ответил: {info['error'].splitlines()[0]}")
                continue
            kind = "ПРОЕКТОР" if is_projector(info) else "Android-устройство"
            print(f"  {host}:{port} — {kind}: {info['brand']} {info['model']} "
                  f"(device {info['device']}, Android {info['android']}, SDK {info['sdk']})")
            print(f"     прошивка: {info['firmware']}")
            print(f"     → export TV_HOST={host} TV_PORT={port}")
        return 0

    host_env = os.environ.get("TV_HOST", "").strip()
    if not host_env:
        print("Не задан TV_HOST. Пример: TV_HOST=192.168.1.50 python3 tools/adb.py --probe\n"
              "Найти проектор: python3 tools/adb.py --discover", file=sys.stderr)
        return 2
    port = int(os.environ.get("TV_PORT") or 0)
    host = host_env
    if ":" in host_env:
        host, p = host_env.rsplit(":", 1)
        port = int(p)
    port = port or 5555
    timeout = float(os.environ.get("TV_TIMEOUT") or 30)

    dev = AdbConnection(host, port, timeout=timeout)
    try:
        if cmd == "--probe":
            info = probe_device(host, port)
            if "error" in info:
                print(info["error"], file=sys.stderr)
                return 1
            print(f"устройство : {info['brand']} {info['model']} ({info['device']})")
            print(f"Android    : {info['android']} (SDK {info['sdk']})")
            print(f"прошивка   : {info['firmware']}")
            print(f"ABI        : {info['abi']}")
            print(f"adb secure : {info['adb_secure']} (пусто или 0 = порт открыт всем в сети)")
            return 0

        dev.connect()

        if cmd == "--screenshot":
            local = argv[1]
            keys = [k for k in argv[2:] if not k.startswith("--")]
            screenshot(dev, local, keys=keys)
            print(f"скриншот сохранён: {local}")
            return 0
        if cmd == "--push":
            local, remote = argv[1], argv[2]
            with open(local, "rb") as f:
                push_bytes(dev, remote, f.read())
            print(f"загружено: {local} → {remote}")
            return 0
        if cmd == "--pull":
            remote, local = argv[1], argv[2]
            data = dev.pull_bytes(remote)
            with open(local, "wb") as f:
                f.write(data)
            print(f"скачано: {remote} → {local} ({len(data)} байт)")
            return 0
        if cmd == "--reboot":
            dev.shell("reboot")
            print("команда перезагрузки отправлена; подождите 60–90 секунд")
            return 0
        if cmd == "--batch":
            lines = [ln.strip() for ln in sys.stdin if ln.strip() and not ln.startswith("#")]
            for line in lines:
                print(f"$ {line}")
                out = dev.shell(line, timeout=timeout)
                sys.stdout.write(out.decode("utf-8", "replace"))
                sys.stdout.flush()
            return 0

        command = " ".join(argv)
        out = dev.shell(command, timeout=timeout)
        sys.stdout.buffer.write(out)
        sys.stdout.flush()
        return 0
    except AdbError as e:
        print(f"ошибка: {e}", file=sys.stderr)
        return 1
    finally:
        dev.close()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
