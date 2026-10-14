"""UI contracts tested without creating Tk, windows, tray icons or a browser."""
import importlib
import queue
import threading
import unittest
from pathlib import Path
from unittest import mock

from yuliao_wallpaper import ui


class Variable:
    def __init__(self, value=None):
        self.value = value

    def get(self):
        return self.value

    def set(self, value):
        self.value = value


def headless_app():
    app = ui.App.__new__(ui.App)
    app.root = mock.Mock()
    app.store = mock.Mock()
    app.store.settings = {
        "mode": "mixed", "theme": "landscape", "themes": ["landscape"],
        "interval_seconds": 1800, "download_hours": 6, "download_count": 10,
        "album": "", "tag": "", "favorites_only": False,
    }
    app.store.records = {}
    app.rotation = mock.Mock()
    app.rotation.lock = threading.RLock()
    app.rotation.enabled = False
    app.rotation.paused = False
    app.rotation.due = None
    app.status, app.activity = Variable(), Variable()
    app.mode = Variable(next(label for label, value in ui.MODES.items() if value == "mixed"))
    app.theme_vars = {key: Variable(key == "landscape") for key in ui.THEMES.values()}
    app.interval = Variable("30 分钟")
    app.minutes = Variable("60")
    app.download = Variable("每 6 小时")
    app.download_count = Variable("10 张")
    app.custom_count = Variable("10")
    app.album_filter, app.tag_filter, app.favorites = Variable(""), Variable(""), Variable(False)
    app.busy, app.closing = False, False
    app.job_kind = ""
    app.cancel_event = None
    app.events = queue.Queue()
    app.cancel_btn = mock.Mock()
    app._refresh = mock.Mock()
    app._buttons = mock.Mock()
    app.client = mock.Mock()
    app.client.last_status = ""
    app.client.last_stats = {}
    app._job = mock.Mock(return_value=True)
    app._selected_ids = mock.Mock(return_value=())
    return app


class FormValidationTests(unittest.TestCase):
    def test_import_does_not_create_tk_or_open_browser(self):
        with mock.patch("tkinter.Tk") as root, mock.patch("webbrowser.open") as browser:
            importlib.reload(ui)
        root.assert_not_called()
        browser.assert_not_called()

    def test_interval_presets_and_custom_minimum(self):
        self.assertEqual([0, 300, 900, 1800, 3600, 86400],
                         [ui.validate_interval(label, "5") for label in list(ui.INTERVALS)[:-1]])
        self.assertEqual(300, ui.validate_interval("自定义", "5"))
        self.assertEqual(604800, ui.validate_interval("自定义", "10080"))
        for value in ("4", "0", "-1", "10081", "5.5", "not a number"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                ui.validate_interval("自定义", value)

    def test_download_count_bounded(self):
        self.assertEqual(4, ui.validate_download_count("4 张", "invalid"))
        self.assertEqual(10, ui.validate_download_count("10 张", "invalid"))
        self.assertEqual(20, ui.validate_download_count("20 张", "invalid"))
        self.assertEqual(1, ui.validate_download_count("自定义", "1"))
        self.assertEqual(30, ui.validate_download_count("自定义", "30"))
        for value in ("0", "31", "-3", "NaN", "3.5"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                ui.validate_download_count("自定义", value)

    def test_multi_selection_applies_focused_not_first(self):
        self.assertEqual("b", ui.selection_focus(("a", "b"), "b"))
        self.assertEqual("a", ui.selection_focus(("a", "b"), "missing"))
        self.assertIsNone(ui.selection_focus((), "missing"))

    def test_toolbar_rows_have_independent_coordinates_not_shared_grid_columns(self):
        positions, height = ui.toolbar_positions([(65, 30), (120, 30), (65, 30), (230, 30)], 270)
        self.assertEqual([(0, 0, 65, 30), (71, 0, 120, 30), (197, 0, 65, 30),
                          (0, 34, 230, 30)], positions)
        self.assertEqual(68, height)
        for x, y, width, _ in positions:
            self.assertLessEqual(x + width, 270)

    def test_toolbar_wrap_uses_each_rows_tallest_button(self):
        positions, height = ui.toolbar_positions([(100, 24), (100, 40), (150, 24)], 220)
        self.assertEqual((0, 44, 150, 24), positions[2])
        self.assertEqual(72, height)


class LinkTests(unittest.TestCase):
    def test_metadata_link_rejects_malformed_or_untrusted_urls(self):
        good = "https://commons.wikimedia.org/wiki/File:Example.jpg"
        self.assertTrue(ui.safe_source_url(good))
        for value in ("http://commons.wikimedia.org/", "file:///secret.png", "javascript:alert(1)",
                      "https://evil.example/", "https://commons.wikimedia.org@evil.example/",
                      "https://user:secret@commons.wikimedia.org/", "https://commons.wikimedia.org:999/",
                      "https://commons.wikimedia.org:bad/", "https://[", None):
            with self.subTest(value=value):
                self.assertFalse(ui.safe_source_url(value))
        with mock.patch.object(ui.webbrowser, "open") as browser:
            ui.App._open_url("https://commons.wikimedia.org:bad/")
            ui.App._open_url("https://evil.example/")
            browser.assert_not_called()
            ui.App._open_url(good)
            browser.assert_called_once_with(good)

    def test_curated_sites_use_exact_urls_without_broadening_metadata_hosts(self):
        with mock.patch.object(ui.webbrowser, "open") as browser:
            for url in ui.WEBSITE_URLS:
                ui.App._open_website(url + "?redirect=evil")
            browser.assert_not_called()
            ui.App._open_website(ui.WEBSITES[1][1])
            browser.assert_called_once_with(ui.WEBSITES[1][1])
        self.assertFalse(ui.safe_source_url(ui.WEBSITES[1][1]))


class SettingsTests(unittest.TestCase):
    def test_download_and_filter_changes_preserve_wallpaper_due(self):
        app = headless_app()
        app.download.set("每天一次")
        app.download_count.set("20 张")
        app.theme_vars["space"].set(True)
        app.album_filter.set("my album")
        self.assertTrue(ui.App._settings(app))
        self.assertEqual(20, app.store.settings["download_count"])
        self.assertEqual(["landscape", "space"], app.store.settings["themes"])
        self.assertEqual("landscape", app.store.settings["theme"])
        app.rotation.reschedule.assert_not_called()

    def test_interval_changes_reschedule_once(self):
        app = headless_app()
        app.interval.set("5 分钟")
        self.assertTrue(ui.App._settings(app))
        self.assertEqual(300, app.store.settings["interval_seconds"])
        app.rotation.reschedule.assert_called_once_with()

    def test_invalid_custom_count_does_not_mutate_or_save(self):
        app = headless_app()
        previous = app.store.settings.copy()
        app.download_count.set("自定义")
        app.custom_count.set("31")
        self.assertFalse(ui.App._settings(app))
        self.assertEqual(previous, app.store.settings)
        app.store.save.assert_not_called()
        app.rotation.reschedule.assert_not_called()

    def test_failed_persistence_restores_settings_and_timer(self):
        app = headless_app()
        previous = app.store.settings.copy()
        app.interval.set("5 分钟")
        app.store.save.side_effect = PermissionError("private profile unavailable")
        self.assertFalse(ui.App._settings(app))
        self.assertEqual(previous, app.store.settings)
        app.rotation.reschedule.assert_not_called()

    def test_empty_theme_selection_is_readable_without_popup(self):
        app = headless_app()
        for variable in app.theme_vars.values():
            variable.set(False)
        with mock.patch.object(ui.messagebox, "showerror") as dialog:
            self.assertFalse(ui.App._settings(app))
        self.assertIn("至少选择", app.status.get())
        app.store.save.assert_not_called()
        dialog.assert_not_called()


class JobContractsTests(unittest.TestCase):
    def test_download_snapshots_themes_and_count_and_queues_progress(self):
        app = headless_app()
        app._settings = mock.Mock(return_value=True)
        app.client.fetch_many.return_value = [{"id": "one"}]
        app.store.add_records.return_value = 1
        ui.App._download(app)
        job = app._job.call_args.args[0]
        cancel = app.cancel_event
        app.store.settings.update(themes=["animals"], download_count=30)
        result = job()
        call = app.client.fetch_many.call_args
        self.assertEqual(("landscape",), call.args[0])
        self.assertEqual(10, call.kwargs["limit"])
        self.assertIs(cancel, call.kwargs["cancel_event"])
        call.kwargs["progress"]("下载 1 / 10")
        self.assertEqual(("progress", "下载 1 / 10", None), app.events.get_nowait())
        self.assertEqual(1, result["download"])
        app.rotation.reschedule.assert_not_called()

    def test_cancel_requests_worker_event_without_destroying_ui(self):
        app = headless_app()
        app.busy, app.cancel_event = True, threading.Event()
        ui.App._cancel(app)
        self.assertTrue(app.cancel_event.is_set())
        app.root.destroy.assert_not_called()
        app.cancel_btn.configure.assert_called_once_with(state="disabled")

    def test_pause_remains_immediate_during_download(self):
        app = headless_app()
        app.busy, app.job_kind, app.rotation.enabled = True, "download", True
        ui.App._pause(app)
        app.rotation.pause.assert_called_once_with()
        app._buttons.assert_called_once_with()

    def test_busy_download_preserves_overdue_rotation_until_worker_available(self):
        app = headless_app()
        app.busy, app.rotation.enabled, app.rotation.due = True, True, 1
        with mock.patch.object(ui.time, "monotonic", return_value=100):
            ui.App._poll(app)
            app._job.assert_not_called()
            self.assertEqual(1, app.rotation.due)
            app.busy = False
            ui.App._poll(app)
        self.assertEqual(1, app._job.call_count)
        self.assertEqual("rotation", app._job.call_args.kwargs["kind"])
        app._job.call_args.args[0]()
        app.rotation.tick.assert_called_once_with()

    def test_progress_does_not_finish_job_or_schedule_another_job(self):
        app = headless_app()
        app.busy, app.cancel_event = True, threading.Event()
        app.events.put(("progress", "正在下载", None))
        ui.App._poll(app)
        self.assertTrue(app.busy)
        self.assertEqual("正在下载", app.status.get())
        app._job.assert_not_called()

    def test_move_converts_toolbar_direction_and_snapshots_selection(self):
        for direction, expected in (("up", -1), ("down", 1)):
            with self.subTest(direction=direction):
                app = headless_app()
                app.tree = mock.Mock()
                app.tree.get_children.return_value = ("one", "two", "three")
                app._selected_ids.return_value = ("two",)
                ui.App._move(app, direction)
                fn = app._job.call_args.args[0]
                app._selected_ids.return_value = ("one",)
                fn()
                app.store.move_records.assert_called_once_with(("two",), expected, visible_ids=("one", "two", "three"))
                app.store.save.assert_not_called()  # Store method saves atomically itself.

    def test_export_copies_only_selected_snapshot(self):
        app = headless_app()
        app.store.records = {"one": {"id": "one", "title": "selected", "origin": "local"},
                             "two": {"id": "two", "title": "private unselected", "origin": "local"}}
        app._selected_ids.return_value = ("one",)
        with mock.patch.object(ui.filedialog, "askdirectory", return_value=str(Path.cwd() / "export-test")), \
             mock.patch.object(ui, "export_records", return_value={"count": 1, "errors": [], "manifest_markdown": "licenses.md"}) as export:
            ui.App._export(app)
            fn = app._job.call_args.args[0]
            app._selected_ids.return_value = ("two",)
            app.store.records["one"]["title"] = "changed after click"
            result = fn()
        self.assertEqual([{"id": "one", "title": "selected", "origin": "local"}], export.call_args.args[0])
        self.assertEqual(1, result["export"])
        self.assertIsNotNone(export.call_args.kwargs["cancel_event"])

    def test_multi_export_decline_never_opens_folder_or_jobs(self):
        app = headless_app()
        app.store.records = {"one": {"id": "one", "origin": "local"}, "two": {"id": "two", "origin": "online"}}
        app._selected_ids.return_value = ("one", "two")
        with mock.patch.object(ui.messagebox, "askokcancel", return_value=False) as confirm, \
             mock.patch.object(ui.filedialog, "askdirectory") as choose:
            ui.App._export(app)
        self.assertIn("2 张", confirm.call_args.args[1])
        choose.assert_not_called()
        app._job.assert_not_called()

    def test_removed_apply_is_rejected_before_wallpaper_or_confirmation(self):
        app = headless_app()
        app._settings = mock.Mock(return_value=True)
        app.selected_id = "one"
        app.store.records = {"one": {"id": "one", "deleted": True}}
        with mock.patch.object(ui.messagebox, "askokcancel") as confirm:
            ui.App._enable(app)
        self.assertIn("恢复", app.status.get())
        app._job.assert_not_called()
        confirm.assert_not_called()


if __name__ == "__main__":
    unittest.main()
