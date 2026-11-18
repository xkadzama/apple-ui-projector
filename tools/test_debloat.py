#!/usr/bin/env python3
"""Тесты защитных правил деблоата — самое важное место: ошибка здесь ломает устройство.

Запуск: python3 tools/test_debloat.py

Проверяется:
  * ядро Android и ТВ-middleware отклоняются целиком (кирпич);
  * владелец Home пропускается, пока новый лончер не стал домашним;
  * setupwraith пропускается, если сохранённая Wi-Fi сеть принадлежит ему;
  * пакеты, отключённые до нас, не попадают в план (restore не должен их включать);
  * разбор списка: комментарии, BOM, CRLF, дубликаты, битые имена;
  * разбор вывода resolve-activities.
"""
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import debloat  # noqa: E402


class ReadListTests(unittest.TestCase):
    def write(self, text, encoding="utf-8"):
        fd, path = tempfile.mkstemp(suffix=".txt")
        with os.fdopen(fd, "w", encoding=encoding, newline="") as f:
            f.write(text)
        return path

    def test_comments_and_blanks_ignored(self):
        p = self.write("# заголовок\n\ncom.a.b   # телеметрия\ncom.c.d\n")
        self.assertEqual(debloat.read_list(p), ["com.a.b", "com.c.d"])

    def test_crlf_and_bom(self):
        p = self.write("\ufeffcom.a.b\r\ncom.c.d\r\n", encoding="utf-8")
        self.assertEqual(debloat.read_list(p), ["com.a.b", "com.c.d"])

    def test_duplicates_removed(self):
        p = self.write("com.a.b\ncom.a.b\n")
        self.assertEqual(debloat.read_list(p), ["com.a.b"])

    def test_bad_name_refused(self):
        p = self.write("com.a.b\npm disable com.evil\n")
        with self.assertRaises(SystemExit) as cm:
            debloat.read_list(p)
        self.assertIn("не похоже на имя пакета", str(cm.exception))

    def test_missing_file(self):
        with self.assertRaises(SystemExit):
            debloat.read_list("/nonexistent/list.txt")


class CoreGuardTests(unittest.TestCase):
    def plan(self, pkgs):
        return debloat.build_plan(
            pkgs, installed=set(pkgs), disabled_now=set(), owners_home=["com.stock.launcher"],
            home_pkg="com.stock.launcher", wifi_owners=[], sdk=33,
        )

    def test_core_package_refused(self):
        for pkg in ("com.android.permissioncontroller", "com.android.systemui",
                    "com.android.tv.settings", "com.cltv.mal", "android"):
            with self.assertRaises(SystemExit) as cm:
                self.plan([pkg])
            self.assertIn("ОТКАЗ", str(cm.exception))

    def test_providers_refused_but_safe_providers_allowed(self):
        with self.assertRaises(SystemExit):
            self.plan(["com.android.providers.media"])
        plan = self.plan(["com.android.providers.calendar"])
        self.assertEqual(plan.apply, ["com.android.providers.calendar"])

    def test_ordinary_telemetry_allowed(self):
        plan = self.plan(["com.xgimi.bugreportsender", "com.google.android.feedback"])
        self.assertEqual(sorted(plan.apply),
                         ["com.google.android.feedback", "com.xgimi.bugreportsender"])


class HomeGuardTests(unittest.TestCase):
    def test_stock_launcher_skipped_when_only_home(self):
        plan = debloat.build_plan(
            ["com.google.android.apps.tv.launcherx"],
            installed={"com.google.android.apps.tv.launcherx"},
            disabled_now=set(), owners_home=["com.google.android.apps.tv.launcherx"],
            home_pkg="com.google.android.apps.tv.launcherx", wifi_owners=[], sdk=34,
        )
        self.assertEqual(plan.apply, [])
        self.assertEqual(len(plan.skip), 1)
        self.assertIn("Home", plan.skip[0][1])

    def test_stock_launcher_allowed_once_new_launcher_exists(self):
        plan = debloat.build_plan(
            ["com.google.android.apps.tv.launcherx"],
            installed={"com.google.android.apps.tv.launcherx"},
            disabled_now=set(),
            owners_home=["com.google.android.apps.tv.launcherx", "com.gathin.tvhome"],
            home_pkg="com.gathin.tvhome", wifi_owners=[], sdk=34,
        )
        self.assertEqual(plan.apply, ["com.google.android.apps.tv.launcherx"])

    def test_setup_wizard_does_not_count_as_a_launcher(self):
        plan = debloat.build_plan(
            ["com.google.android.apps.tv.launcherx"],
            installed={"com.google.android.apps.tv.launcherx"},
            disabled_now=set(),
            owners_home=["com.google.android.apps.tv.launcherx",
                         "com.google.android.tungsten.setupwraith"],
            home_pkg="com.google.android.apps.tv.launcherx", wifi_owners=[], sdk=34,
        )
        self.assertEqual(plan.apply, [],
                         "сетап-мастер — не запасной домашний экран")

    def test_force_home_overrides(self):
        plan = debloat.build_plan(
            ["com.google.android.apps.tv.launcherx"],
            installed={"com.google.android.apps.tv.launcherx"},
            disabled_now=set(), owners_home=["com.google.android.apps.tv.launcherx"],
            home_pkg="com.google.android.apps.tv.launcherx", wifi_owners=[], sdk=34,
            force_home=True,
        )
        self.assertEqual(plan.apply, ["com.google.android.apps.tv.launcherx"])


class SetupWraithTests(unittest.TestCase):
    def test_skipped_when_wifi_belongs_to_it(self):
        plan = debloat.build_plan(
            ["com.google.android.tungsten.setupwraith"],
            installed={"com.google.android.tungsten.setupwraith"},
            disabled_now=set(), owners_home=[], home_pkg="", wifi_owners=[
                "android.uid.system:1000", "com.google.android.tungsten.setupwraith:1000"],
            sdk=34,
        )
        self.assertEqual(plan.apply, [])
        self.assertIn("Wi-Fi", plan.skip[0][1])

    def test_skipped_when_wifi_unknown(self):
        plan = debloat.build_plan(
            ["com.google.android.tungsten.setupwraith"],
            installed={"com.google.android.tungsten.setupwraith"},
            disabled_now=set(), owners_home=[], home_pkg="", wifi_owners=[], sdk=34,
        )
        self.assertEqual(plan.apply, [],
                         "если владельца сети проверить нельзя — не рискуем")

    def test_allowed_when_wifi_belongs_to_system(self):
        plan = debloat.build_plan(
            ["com.google.android.tungsten.setupwraith"],
            installed={"com.google.android.tungsten.setupwraith"},
            disabled_now=set(), owners_home=[], home_pkg="",
            wifi_owners=["android.uid.system:1000"], sdk=34,
        )
        self.assertEqual(plan.apply, ["com.google.android.tungsten.setupwraith"])


class AlreadyDisabledTests(unittest.TestCase):
    def test_previously_disabled_package_is_skipped(self):
        plan = debloat.build_plan(
            ["com.example.thing"], installed={"com.example.thing"},
            disabled_now={"com.example.thing"}, owners_home=[], home_pkg="",
            wifi_owners=[], sdk=34,
        )
        self.assertEqual(plan.apply, [])
        self.assertIn("уже отключён", plan.skip[0][1])

    def test_absent_package_reported_not_applied(self):
        plan = debloat.build_plan(
            ["com.gone.app"], installed=set(), disabled_now=set(),
            owners_home=[], home_pkg="", wifi_owners=[], sdk=34,
        )
        self.assertEqual(plan.apply, [])
        self.assertEqual(plan.absent, ["com.gone.app"])


class ParsingTests(unittest.TestCase):
    def test_owner_of_home(self):
        text = ("priority=2\ncom.google.android.apps.tv.launcherx/.home.HomeActivity\n")
        self.assertEqual(debloat.owner_of_home(text),
                         "com.google.android.apps.tv.launcherx")

    def test_home_packages(self):
        text = ("priority=0\ncom.android.settings/.FallbackHome\n"
                "com.gathin.tvhome/.MainActivity\n")
        self.assertEqual(debloat.home_packages(text),
                         ["com.android.settings", "com.gathin.tvhome"])


class FakeDev:
    """Отдаёт заранее заданный ответ на pm list packages -d."""

    def __init__(self, out):
        self.out = out

    def shell(self, command, timeout=None):
        return self.out.encode()


class VerifyDisabledTests(unittest.TestCase):
    """Регрессия: проверка возвращала то же, что получала, и все пакеты выглядели
    «не отключившимися» сразу после успешного отключения на реальном проекторе."""

    def test_reports_only_packages_still_enabled(self):
        dev = FakeDev("package:com.a.b\npackage:com.c.d\n")
        self.assertEqual(debloat.verify_disabled(dev, ["com.a.b", "com.c.d"]), [])

    def test_reports_the_ones_that_did_not_take(self):
        dev = FakeDev("package:com.a.b\n")
        self.assertEqual(debloat.verify_disabled(dev, ["com.a.b", "com.c.d"]),
                         ["com.c.d"])

    def test_empty_disabled_list_means_nothing_took(self):
        dev = FakeDev("")
        self.assertEqual(sorted(debloat.verify_disabled(dev, ["com.a.b", "com.c.d"])),
                         ["com.a.b", "com.c.d"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
