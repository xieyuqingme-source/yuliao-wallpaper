"""Entry regressions use mocked Tk objects and never create a window."""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from yuliao_wallpaper import __main__ as entry


class EntryTests(unittest.TestCase):
    def test_check_failure_returns_error_without_creating_tk(self):
        with mock.patch.object(entry, "main", side_effect=PermissionError("atomic rename refused")), \
             mock.patch.object(entry.sys, "argv", ["YuliaoWallpaper.exe", "--self-check"]), \
             mock.patch("tkinter.Tk") as tk, mock.patch.object(entry.sys, "stderr", None):
            self.assertEqual(1, entry.run())
            tk.assert_not_called()

    def test_smoke_failure_returns_error_without_exception_dialog(self):
        with mock.patch.object(entry, "main", side_effect=PermissionError("atomic rename refused")), \
             mock.patch.object(entry.sys, "argv", ["YuliaoWallpaper.exe", "--smoke-ui"]), \
             mock.patch("tkinter.messagebox.showerror") as dialog, \
             mock.patch.object(entry.sys, "stderr", None):
            self.assertEqual(1, entry.run())
            dialog.assert_not_called()

    def test_smoke_persistence_failure_closes_test_resources(self):
        with tempfile.TemporaryDirectory(prefix=".test-entry-", dir=Path.cwd()) as folder:
            fake_app = mock.Mock()
            fake_app.store.import_files.side_effect = PermissionError("config.tmp -> config.json")
            fake_root = mock.Mock()
            with mock.patch("tkinter.Tk", return_value=fake_root), \
                 mock.patch("yuliao_wallpaper.ui.App", return_value=fake_app):
                with self.assertRaises(PermissionError):
                    entry.smoke_ui(Path(folder))
            fake_app._quit.assert_called_once()
            self.assertEqual([], list(Path(folder).iterdir()))

    def test_smoke_constructor_failure_destroys_root(self):
        with tempfile.TemporaryDirectory(prefix=".test-entry-", dir=Path.cwd()) as folder:
            fake_root = mock.Mock()
            with mock.patch("tkinter.Tk", return_value=fake_root), \
                 mock.patch("yuliao_wallpaper.ui.App", side_effect=ValueError("bad config")):
                with self.assertRaises(ValueError):
                    entry.smoke_ui(Path(folder))
            fake_root.destroy.assert_called_once()
            self.assertEqual([], list(Path(folder).iterdir()))


if __name__ == "__main__":
    unittest.main()
