#!/usr/bin/env python3
"""Тесты для tools/adb.py на фальшивом adbd — без проектора и без сети.

Запуск: python3 tools/test_adb.py

Проверяется протокол ADB, а не догадки о нём:
  * рукопожатие CNXN → AUTH(токен) → AUTH_SIGNATURE → публичный ключ → CNXN;
  * второй AUTH-токен даёт AUTH_RSAPUBLICKEY (иначе проектор не покажет «Разрешить отладку»);
  * shell-команда приходит в OPEN ("shell:<команда>"), вывод читается из WRTE;
  * pull через sync:RECV отдаёт байты файла;
  * --probe разбирает getprop, проектор не путается с телефоном.
"""
import os
import socket
import struct
import sys
import threading
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import adb  # noqa: E402

PROPS = """[ro.product.brand]: [XGIMI]
[ro.product.model]: [MoGo 4]
[ro.product.device]: [altai]
[ro.build.version.release]: [14]
[ro.build.version.sdk]: [34]
[ro.build.display.id]: [UKNV.260612.001]
[ro.product.cpu.abi]: [arm64-v8a]
[ro.adb.secure]: [1]
"""


class FakeAdbd(threading.Thread):
    """Минимальный adbd: shell и sync:RECV, по потоку на соединение."""

    def __init__(self):
        super().__init__(daemon=True)
        self.sock = socket.socket()
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(8)
        self.port = self.sock.getsockname()[1]
        self.files = {"/sdcard/test.txt": b"hello from the projector\n"}
        self.commands = []
        self.saw_public_key = False
        self._id = 100
        self._lock = threading.Lock()

    def _next(self):
        with self._lock:
            self._id += 1
            return self._id

    def run(self):
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    def _serve(self, conn):
        while True:
            pkt = self._read(conn)
            if not pkt:
                return
            cmd, a0, a1, data = pkt
            if cmd == adb.ADB_CNXN:
                self._send(conn, adb.ADB_AUTH, self._next(), 0, b"\x11" * 20)
            elif cmd == adb.ADB_AUTH:
                if a0 == 3:  # AUTH_RSAPUBLICKEY
                    self.saw_public_key = True
                    self._send(conn, adb.ADB_CNXN, adb.ADB_VERSION, adb.MAXDATA,
                               b"device::ro.product.name=fake;")
                else:
                    # ключ «неизвестен»: просим ещё раз, чтобы клиент прислал публичный ключ
                    self._send(conn, adb.ADB_AUTH, self._next(), 0, b"\x22" * 20)
            elif cmd == adb.ADB_OPEN:
                our = self._next()
                self._send(conn, adb.ADB_OKAY, a0, our, b"")
                dest = data.split(b"\x00")[0]
                if dest.startswith(b"shell:"):
                    self._shell(conn, a0, our, dest[len(b"shell:"):].decode("utf-8", "replace"))
                # sync: сервер отвечает на последующий WRTE (RECV/...) сам
            elif cmd == adb.ADB_WRTE:
                # OKAY(remote=наш id канала, local=id клиента), как настоящий adbd
                self._send(conn, adb.ADB_OKAY, a1, a0, b"")
                if data.startswith(b"RECV"):
                    name = data[8:8 + struct.unpack("<I", data[4:8])[0]].decode()
                    blob = self.files.get(name, b"")
                    # sync-блок: 4 байта типа + 4 байта длины + данные
                    self._send(conn, adb.ADB_WRTE, a1, a0,
                               b"DATA" + struct.pack("<I", len(blob)) + blob)
                    self._send(conn, adb.ADB_WRTE, a1, a0, b"DONE" + struct.pack("<I", 0))

    def _shell(self, conn, local, remote, command):
        self.commands.append(command)
        if command == "getprop":
            out = PROPS.encode()
        elif command.startswith(("screencap", "input keyevent", "rm ")):
            out = b""
        else:
            out = f"ran: {command}\n".encode()
        if out:
            self._send(conn, adb.ADB_WRTE, remote, local, out)
        self._send(conn, adb.ADB_CLSE, remote, local, b"")

    @staticmethod
    def _read(conn):
        hdr = b""
        while len(hdr) < 24:
            chunk = conn.recv(24 - len(hdr))
            if not chunk:
                return None
            hdr += chunk
        cmd, a0, a1, length, _crc, _magic = struct.unpack("<6I", hdr)
        data = b""
        while len(data) < length:
            chunk = conn.recv(length - len(data))
            if not chunk:
                return None
            data += chunk
        return cmd, a0, a1, data

    @staticmethod
    def _send(conn, cmd, a0, a1, data):
        hdr = struct.pack("<6I", cmd, a0, a1, len(data), sum(data) & 0xFFFFFFFF, cmd ^ 0xFFFFFFFF)
        try:
            conn.sendall(hdr + data)
        except OSError:
            pass


class AdbTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ["TV_KEY"] = os.path.expanduser("~/.projector/_test_key")
        cls.server = FakeAdbd()
        cls.server.start()

    def setUp(self):
        self.conn = adb.AdbConnection("127.0.0.1", self.server.port, timeout=5)
        self.conn.connect()

    def tearDown(self):
        self.conn.close()

    def test_shell_command_output(self):
        self.assertEqual(self.conn.shell("pm list packages"), b"ran: pm list packages\n")

    def test_command_is_sent_in_open_payload(self):
        self.conn.shell("settings get global adb_enabled")
        self.assertIn("settings get global adb_enabled", self.server.commands)

    def test_public_key_sent_on_second_auth_token(self):
        self.assertTrue(self.server.saw_public_key,
                        "без AUTH_RSAPUBLICKEY проектор не покажет запрос «Разрешить отладку»")

    def test_pull_file_via_sync(self):
        self.assertEqual(self.conn.pull_bytes("/sdcard/test.txt"), b"hello from the projector\n")

    def test_pull_missing_file_is_empty(self):
        self.assertEqual(self.conn.pull_bytes("/sdcard/nope"), b"")

    def test_probe_reads_properties(self):
        info = adb.probe_device("127.0.0.1", self.server.port)
        self.assertEqual(info["model"], "MoGo 4")
        self.assertEqual(info["sdk"], "34")
        self.assertEqual(info["android"], "14")

    def test_projector_detected(self):
        info = adb.probe_device("127.0.0.1", self.server.port)
        self.assertTrue(adb.is_projector(info), "XGIMI MoGo 4 должен опознаваться как проектор")

    def test_phone_not_mistaken_for_projector(self):
        self.assertFalse(adb.is_projector({"brand": "Google", "model": "Pixel 7", "device": "panther"}))


if __name__ == "__main__":
    unittest.main(verbosity=2)
