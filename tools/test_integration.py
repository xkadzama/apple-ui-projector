#!/usr/bin/env python3
"""Интеграционный тест: весь цикл на «фальшивом проекторе».

Настоящего устройства нет, поэтому adbd эмулируется: он ведёт себя как Android TV —
отвечает на pm list packages / pm disable-user / pm enable / dumpsys wifi /
cmd package resolve-activity и запоминает состояние.

Проверяется то, что на живом устройстве ломается чаще всего:
  * бэкап снимается ДО изменений и содержит верные данные;
  * debloat отключает только разрешённое и проверяет результат;
  * Home guard не даёт остаться без домашнего экрана;
  * restore возвращает ровно то, что мы отключили, и не трогает чужое;
  * повторный запуск restore ничего не ломает (идемпотентность).

Запуск: python3 tools/test_integration.py
"""
import importlib
import io
import json
import os
import shutil
import socket
import struct
import sys
import tempfile
import threading
import unittest
from contextlib import redirect_stdout

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import adb  # noqa: E402


class FakeProjector(threading.Thread):
    """adbd + состояние Android TV: пакеты, отключённые пакеты, Home, Wi-Fi."""

    def __init__(self):
        super().__init__(daemon=True)
        self.sock = socket.socket()
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(8)
        self.port = self.sock.getsockname()[1]
        self.packages = {
            "com.android.systemui", "com.android.settings", "com.android.tv.settings",
            "com.google.android.gms", "com.google.android.apps.tv.launcherx",
            "com.google.android.youtube.tv", "com.netflix.ninja",
            "com.google.android.feedback", "com.google.android.play.games",
            "com.android.adservices.api", "com.xgimi.tof", "com.xgimi.autokst",
            "com.xgimi.bugreportsender", "com.android.permissioncontroller",
        }
        self.disabled = set()
        self.home = "com.google.android.apps.tv.launcherx/.home.HomeActivity"
        self.other_home = ["com.google.android.apps.tv.launcherx"]
        self.wifi_owners = ["android.uid.system:1000"]
        self.commands = []
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

    # -- протокол

    def _serve(self, conn):
        while True:
            pkt = self._read(conn)
            if not pkt:
                return
            cmd, a0, a1, data = pkt
            if cmd == adb.ADB_CNXN:
                self._send(conn, adb.ADB_AUTH, self._next(), 0, b"\x11" * 20)
            elif cmd == adb.ADB_AUTH:
                if a0 == 3:
                    self._send(conn, adb.ADB_CNXN, adb.ADB_VERSION, adb.MAXDATA, b"device::fake;")
                else:
                    self._send(conn, adb.ADB_AUTH, self._next(), 0, b"\x22" * 20)
            elif cmd == adb.ADB_OPEN:
                our = self._next()
                self._send(conn, adb.ADB_OKAY, a0, our, b"")
                dest = data.split(b"\x00")[0]
                if dest.startswith(b"shell:"):
                    self._shell(conn, a0, our, dest[len(b"shell:"):].decode("utf-8", "replace"))

    def _shell(self, conn, local, remote, command):
        self.commands.append(command)
        out = self._run_command(command)
        if out:
            self._send(conn, adb.ADB_WRTE, remote, local, out.encode())
        self._send(conn, adb.ADB_CLSE, remote, local, b"")

    def _run_command(self, cmd):
        cmd = cmd.strip()
        if cmd == "getprop":
            return ("[ro.product.brand]: [XGIMI]\n[ro.product.model]: [MoGo 4]\n"
                    "[ro.product.device]: [altai]\n[ro.build.version.release]: [13]\n"
                    "[ro.build.version.sdk]: [33]\n[ro.build.display.id]: [TEST.1]\n"
                    "[ro.product.cpu.abi]: [arm64-v8a]\n[ro.adb.secure]: [1]\n")
        if cmd == "pm list packages":
            pkgs = self.packages | {"com.gathin.tvhome"}
            return "\n".join(f"package:{p}" for p in sorted(pkgs)) + "\n"
        if cmd == "pm list packages -d --user 0":
            return "".join(f"package:{p}\n" for p in sorted(self.disabled))
        if cmd.startswith("pm disable-user --user 0 "):
            pkg = cmd.rsplit(" ", 1)[1]
            if pkg not in self.packages and pkg != "com.gathin.tvhome":
                return "Error: unknown package"
            if pkg in self.disabled:
                return f"Package {pkg} new state: disabled-user"
            self.disabled.add(pkg)
            return f"Package {pkg} new state: disabled-user"
        if cmd.startswith("pm enable --user 0 "):
            pkg = cmd.rsplit(" ", 1)[1]
            self.disabled.discard(pkg)
            return f"Package {pkg} new state: enabled"
        if cmd.startswith("pm grant") or cmd.startswith("settings"):
            return ""
        if cmd == "dumpsys wifi":
            return "\n".join(f"mNetworkName={{cname={o}}}" for o in self.wifi_owners)
        if cmd.startswith("cmd package resolve-activity"):
            return "priority=0\n" + self.home + "\n"
        if cmd.startswith("cmd package query-activities"):
            return "\n".join(f"priority=0\n{p}/x.Activity" for p in self.other_home) + "\n"
        if cmd.startswith("cmd package set-home-activity"):
            comp = cmd.rsplit(" ", 1)[1]
            self.home = comp
            pkg = comp.split("/")[0]
            if pkg not in self.other_home:
                self.other_home.append(pkg)
            return "Success"
        if cmd in ("sync", "input keyevent KEYCODE_HOME"):
            return ""
        return ""

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


class IntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.home = tempfile.mkdtemp(prefix="projhome-")
        os.environ["HOME"] = cls.home
        os.environ["TV_KEY"] = os.path.join(cls.home, ".projector", "testkey")
        cls.device = FakeProjector()
        cls.device.start()
        os.environ["TV_HOST"] = f"127.0.0.1"
        os.environ["TV_PORT"] = str(cls.device.port)
        # модули читают ~/.projector при импорте
        for mod in ("adb", "backup", "debloat", "restore"):
            if mod in sys.modules:
                importlib.reload(sys.modules[mod])
        import backup
        import debloat
        import restore
        cls.backup, cls.debloat, cls.restore = backup, debloat, restore

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.home, ignore_errors=True)

    def setUp(self):
        self.device.disabled = set()
        self.device.home = "com.google.android.apps.tv.launcherx/.home.HomeActivity"
        self.device.other_home = ["com.google.android.apps.tv.launcherx"]
        self.device.wifi_owners = ["android.uid.system:1000"]
        for f in ("last_apply.json",):
            p = os.path.join(self.home, ".projector", f)
            if os.path.exists(p):
                os.unlink(p)
        bdir = os.path.join(self.home, ".projector", "backups")
        if os.path.isdir(bdir):
            shutil.rmtree(bdir)

    def run_quiet(self, fn, *args):
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = fn(list(args))
        return code, buf.getvalue()

    def test_full_cycle_backup_debloat_restore(self):
        list_path = os.path.join(self.home, "list.txt")
        with open(list_path, "w", encoding="utf-8") as f:
            f.write("# тест\n")
            f.write("com.google.android.feedback\n")
            f.write("com.google.android.play.games\n")
            f.write("com.android.adservices.api\n")
            f.write("com.xgimi.bugreportsender\n")

        code, out = self.run_quiet(self.backup.main)
        self.assertEqual(code, 0, out)
        self.assertIn("Бэкап сохранён", out)
        bdir = self.backup.latest_backup()
        self.assertTrue(os.path.exists(os.path.join(bdir, "meta.json")))
        with open(os.path.join(bdir, "meta.json"), encoding="utf-8") as f:
            meta = json.load(f)
        self.assertEqual(meta["model"], "MoGo 4")
        self.assertEqual(meta["sdk"], "33")

        code, out = self.run_quiet(self.debloat.main, list_path, "--dry-run")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.device.disabled, set(), "--dry-run не должен ничего менять")

        code, out = self.run_quiet(self.debloat.main, list_path)
        self.assertEqual(code, 0, out)
        self.assertEqual(self.device.disabled, {
            "com.google.android.feedback", "com.google.android.play.games",
            "com.android.adservices.api", "com.xgimi.bugreportsender"}, out)
        # Регрессия: сразу после успешного отключения скрипт писал, что НИ ОДИН пакет
        # не отключился, потому что результат проверки инвертировался дважды.
        self.assertNotIn("не отключились", out)
        self.assertIn("пакет(ов) отключены", out)

        code, out = self.run_quiet(self.restore.main)
        self.assertEqual(code, 0, out)
        self.assertEqual(self.device.disabled, set(), "restore должен всё вернуть")

    def test_restore_leaves_foreign_disables_alone(self):
        # пользователь сам отключил YouTube Music до нас
        self.device.disabled = {"com.google.android.youtube.tvmusic"}
        self.device.packages.add("com.google.android.youtube.tvmusic")
        list_path = os.path.join(self.home, "l2.txt")
        with open(list_path, "w", encoding="utf-8") as f:
            f.write("com.google.android.feedback\n")

        self.run_quiet(self.backup.main)
        self.run_quiet(self.debloat.main, list_path)
        self.assertIn("com.google.android.feedback", self.device.disabled)
        self.run_quiet(self.restore.main)
        self.assertEqual(self.device.disabled, {"com.google.android.youtube.tvmusic"},
                         "чужое отключение не должно включаться")

    def test_home_guard_blocks_stock_launcher(self):
        list_path = os.path.join(self.home, "l3.txt")
        with open(list_path, "w", encoding="utf-8") as f:
            f.write("com.google.android.apps.tv.launcherx\n")
        self.run_quiet(self.backup.main)
        code, out = self.run_quiet(self.debloat.main, list_path)
        self.assertEqual(code, 0, out)
        self.assertNotIn("com.google.android.apps.tv.launcherx", self.device.disabled,
                         "нельзя отключать единственный домашний экран")
        self.assertIn("Home", out)

    def test_core_package_refused_before_touching_device(self):
        list_path = os.path.join(self.home, "l4.txt")
        with open(list_path, "w", encoding="utf-8") as f:
            f.write("com.android.systemui\ncom.google.android.feedback\n")
        self.run_quiet(self.backup.main)
        with self.assertRaises(SystemExit):
            self.debloat.main([list_path])
        self.assertEqual(self.device.disabled, set(), "при отказе ничего не меняется")

    def test_restore_is_idempotent(self):
        list_path = os.path.join(self.home, "l5.txt")
        with open(list_path, "w", encoding="utf-8") as f:
            f.write("com.google.android.play.games\n")
        self.run_quiet(self.backup.main)
        self.run_quiet(self.debloat.main, list_path)
        self.run_quiet(self.restore.main)
        code, out = self.run_quiet(self.restore.main)
        self.assertEqual(code, 0, out)
        self.assertEqual(self.device.disabled, set())


if __name__ == "__main__":
    unittest.main(verbosity=2)
