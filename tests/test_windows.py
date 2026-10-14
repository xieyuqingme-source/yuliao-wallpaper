"""All Windows calls are fakes. These tests never alter the real desktop."""

import json
import tempfile
import unittest
from pathlib import Path

from PIL import Image

from yuliao_wallpaper.windows import WallpaperError, WindowsWallpaper


class FakeAPI:
    def __init__(self, wallpaper="", monitors=1):
        self.wallpaper = str(wallpaper)
        self.monitors = monitors
        self.calls = []
        self.fail = False

    def get_wallpaper(self):
        return self.wallpaper

    def set_wallpaper(self, path):
        self.calls.append(path)
        if self.fail:
            raise WallpaperError("simulated SPI failure")
        self.wallpaper = path

    def screen_size(self):
        return 1920, 1080

    def monitor_count(self):
        return self.monitors


class FakeRegistry:
    def __init__(self, values=None):
        self.values = dict(values or {})
        self.fail_on = None

    def get(self, name):
        return self.values.get(name)

    def set(self, name, value, kind):
        if name == self.fail_on:
            self.fail_on = None
            raise OSError("simulated registry failure")
        self.values[name] = (value, kind)

    def delete(self, name):
        self.values.pop(name, None)


class WindowsWallpaperTests(unittest.TestCase):
    def setUp(self):
        # Explicitly stay inside the project; Windows sandbox temp redirection
        # can otherwise deny atomic os.replace even for fake-only tests.
        self.temp = tempfile.TemporaryDirectory(prefix=".windows-test-", dir=Path(__file__).resolve().parent)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.original = self.make_image(self.root / "original.bmp", "blue")
        self.data = self.root / "private"
        self.data.mkdir()
        self.new = self.make_image(self.data / "next.bmp", "red")
        self.api = FakeAPI(self.original)
        self.reg = FakeRegistry({
            "WallpaperStyle": ("6", 1), "TileWallpaper": ("0", 1),
            "Wallpaper": (str(self.original), 1), "TranscodedImageCache": (b"\x00\xff", 3),
        })
        self.wall = WindowsWallpaper(self.data, api=self.api, registry=self.reg,
                                     cache_path=self.root / "absent-cache")

    @staticmethod
    def make_image(path, color):
        Image.new("RGB", (16, 10), color).save(path, format="BMP")
        return path

    def test_construction_screen_and_missing_backup_are_read_only(self):
        self.assertFalse(self.wall.has_backup())
        self.assertEqual(self.wall.screen_size(), (1920, 1080))
        self.assertEqual(self.api.calls, [])
        self.assertFalse(self.wall.backup_dir.exists())

    def test_apply_requires_committed_backup_before_registry_or_api_writes(self):
        before = dict(self.reg.values)
        with self.assertRaises(WallpaperError):
            self.wall.apply(self.new)
        self.assertEqual(self.reg.values, before)
        self.assertEqual(self.api.calls, [])

    def test_capture_is_durable_and_immutable(self):
        first = self.wall.capture_original()
        self.assertEqual(self.api.calls, [])
        self.assertTrue(self.wall.has_backup())
        self.assertEqual(first["registry"]["TranscodedImageCache"]["binary"], "AP8=")
        self.assertEqual((self.wall.backup_dir / first["snapshot_file"]).read_bytes(), self.original.read_bytes())
        self.wall.apply(self.new)
        fresh_instance = WindowsWallpaper(self.data, api=self.api, registry=self.reg)
        self.assertEqual(fresh_instance.capture_original(), first)
        self.assertEqual(self.reg.values["WallpaperStyle"], ("10", 1))
        self.assertEqual(self.api.wallpaper, str(self.new.resolve()))

    def test_restore_original_source_and_exact_settings(self):
        self.reg.values["WallpaperStyle"] = ("2", 2)
        self.reg.values.pop("TileWallpaper")
        self.wall.capture_original()
        self.wall.apply(self.new)
        self.wall.restore()
        self.assertEqual(self.api.wallpaper, str(self.original))
        self.assertEqual(self.reg.values["WallpaperStyle"], ("2", 2))
        self.assertNotIn("TileWallpaper", self.reg.values)
        self.assertFalse(self.wall.has_backup())
        self.assertTrue(list((self.data / "original_history").glob("*/original.json")))

    def test_restore_uses_copy_if_source_deleted_or_edited(self):
        record = self.wall.capture_original()
        self.wall.apply(self.new)
        self.make_image(self.original, "green")
        self.wall.restore()
        expected = Path(self.api.wallpaper)
        self.assertTrue(expected.is_relative_to(self.data / "original_history"))
        self.assertEqual(expected.name, record["snapshot_file"])
        self.wall.capture_original()
        self.wall.apply(self.new)
        self.original.unlink()
        self.wall.restore()
        self.assertEqual(self.api.wallpaper, str(expected))

    def test_blank_original_restores_empty_path(self):
        self.api.wallpaper = ""
        record = self.wall.capture_original()
        self.assertIsNone(record["snapshot_file"])
        self.wall.apply(self.new)
        self.wall.restore()
        self.assertEqual(self.api.wallpaper, "")

    def test_missing_original_and_cache_blocks_without_desktop_changes(self):
        self.original.unlink()
        with self.assertRaises(WallpaperError):
            self.wall.capture_original()
        self.assertFalse(self.wall.has_backup())
        self.assertEqual(self.api.calls, [])
        self.assertFalse(self.wall.manifest.exists())
        self.assertTrue((self.wall.backup_dir / ".capture.lock").exists())

    def test_valid_single_theme_cache_can_preserve_inaccessible_original(self):
        cache = self.make_image(self.root / "TranscodedWallpaper", "purple")
        self.original.unlink()
        self.wall.cache_path = cache
        source_metadata = b"\0" * 24 + (str(self.original) + "\0").encode("utf-16-le")
        self.reg.values["TranscodedImageCache"] = (source_metadata, 3)
        record = self.wall.capture_original()
        self.assertEqual(record["snapshot_source"], "windows_theme_cache")
        self.wall.apply(self.new)
        self.wall.restore()
        self.assertEqual(Path(self.api.wallpaper).read_bytes(), cache.read_bytes())

    def test_unidentified_or_mismatched_theme_cache_is_not_used(self):
        cache = self.make_image(self.root / "TranscodedWallpaper", "purple")
        self.original.unlink()
        self.wall.cache_path = cache
        unknown_source = b"\0" * 24 + (str(self.new) + "\0").encode("utf-16-le")
        for metadata in (b"\0\xff", unknown_source):
            with self.subTest(metadata=metadata):
                self.reg.values["TranscodedImageCache"] = (metadata, 3)
                with self.assertRaises(WallpaperError):
                    self.wall.capture_original()
                self.assertFalse(self.wall.has_backup())
                self.assertEqual(self.api.calls, [])

    def test_corrupt_snapshot_or_record_is_not_overwritten(self):
        record = self.wall.capture_original()
        snapshot = self.wall.backup_dir / record["snapshot_file"]
        snapshot.write_bytes(b"tampered")
        self.assertFalse(self.wall.has_backup())
        for action in (self.wall.capture_original, self.wall.restore, lambda: self.wall.apply(self.new)):
            with self.assertRaises(WallpaperError):
                action()
        self.assertEqual(snapshot.read_bytes(), b"tampered")
        self.assertEqual(self.api.calls, [])

    def test_manifest_path_escape_is_rejected(self):
        self.wall.capture_original()
        record = json.loads(self.wall.manifest.read_text(encoding="utf-8"))
        record["snapshot_file"] = "../../original.bmp"
        self.wall.manifest.write_text(json.dumps(record), encoding="utf-8")
        self.assertFalse(self.wall.has_backup())
        with self.assertRaises(WallpaperError):
            self.wall.restore()
        self.assertEqual(self.api.calls, [])

    def test_malformed_manifest_shapes_are_safe_errors(self):
        self.wall.capture_original()
        record = json.loads(self.wall.manifest.read_text(encoding="utf-8"))
        malformed = [[], None, dict(record, registry=[]),
                     dict(record, registry={"WallpaperStyle": [], "TileWallpaper": {"present": False}})]
        for candidate in malformed:
            with self.subTest(candidate=candidate):
                self.wall.manifest.write_text(json.dumps(candidate), encoding="utf-8")
                self.assertFalse(self.wall.has_backup())
                with self.assertRaises(WallpaperError):
                    self.wall.restore()
        self.assertEqual(self.api.calls, [])

    def test_existing_stale_lock_file_does_not_block_capture(self):
        self.wall.backup_dir.mkdir()
        (self.wall.backup_dir / ".capture.lock").write_bytes(b"stale")
        self.wall.capture_original()
        self.assertTrue(self.wall.has_backup())

    def test_restore_then_enable_captures_the_new_session_wallpaper(self):
        first = self.wall.capture_original()
        self.wall.apply(self.new)
        self.wall.restore()
        self.api.wallpaper = str(self.new)
        second = self.wall.capture_original()
        self.assertNotEqual(second["original_path"], first["original_path"])
        self.assertEqual(second["original_path"], str(self.new))
        self.wall.restore()
        self.assertEqual(self.api.wallpaper, str(self.new))

    def test_failed_restore_preserves_active_snapshot(self):
        self.wall.capture_original()
        self.wall.apply(self.new)
        before = dict(self.reg.values)
        self.api.fail = True
        with self.assertRaises(WallpaperError):
            self.wall.restore()
        self.assertTrue(self.wall.has_backup())
        self.assertEqual(self.reg.values, before)
        self.assertEqual(self.api.wallpaper, str(self.new))

    def test_apply_rejects_image_outside_private_root_or_invalid_image(self):
        self.wall.capture_original()
        with self.assertRaises(WallpaperError):
            self.wall.apply(self.original)
        self.new.write_text("not an image", encoding="ascii")
        with self.assertRaises(WallpaperError):
            self.wall.apply(self.new)
        self.assertEqual(self.api.calls, [])

    def test_spi_failure_rolls_back_style_and_keeps_snapshot(self):
        self.wall.capture_original()
        before = dict(self.reg.values)
        self.api.fail = True
        with self.assertRaises(WallpaperError):
            self.wall.apply(self.new)
        self.assertEqual(self.reg.values, before)
        self.assertEqual(self.api.wallpaper, str(self.original))
        self.assertTrue(self.wall.has_backup())

    def test_partial_registry_failure_rolls_back_style(self):
        self.wall.capture_original()
        before = dict(self.reg.values)
        self.reg.fail_on = "TileWallpaper"
        with self.assertRaises(OSError):
            self.wall.apply(self.new)
        self.assertEqual(self.reg.values, before)
        self.assertEqual(self.api.calls, [])

    def test_multiple_monitors_block_capture_and_apply(self):
        self.api.monitors = 2
        with self.assertRaises(WallpaperError):
            self.wall.capture_original()
        self.assertEqual(self.api.calls, [])
        self.api.monitors = 1
        self.wall.capture_original()
        self.api.monitors = 2
        with self.assertRaises(WallpaperError):
            self.wall.apply(self.new)
        with self.assertRaises(WallpaperError):
            self.wall.restore()
        self.assertEqual(self.api.calls, [])

    def test_wallpaper_changed_during_capture_never_commits(self):
        calls = iter((str(self.original), str(self.new)))
        self.api.get_wallpaper = lambda: next(calls)
        with self.assertRaises(WallpaperError):
            self.wall.capture_original()
        self.assertFalse(self.wall.manifest.exists())
        self.assertFalse(self.wall.has_backup())
        self.assertEqual(self.api.calls, [])


if __name__ == "__main__":
    unittest.main()
