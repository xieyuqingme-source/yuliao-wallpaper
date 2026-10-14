from __future__ import annotations

import hashlib
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from PIL import Image

from yuliao_wallpaper import export as export_module
from yuliao_wallpaper.export import export_records


class ExportTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="export-tests-", dir=Path(__file__).parent)
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.destination = self.root / "导出目录"

    def record(self, name="风景.png", color="red", **changes):
        picture = self.root / name
        Image.new("RGB", (180, 120), color).save(picture, format="PNG")
        return {"id": hashlib.sha256(picture.read_bytes()).hexdigest(), "path": str(picture.absolute()),
                "title": name, "origin": "online", "author": "照片作者", "source_url": "https://commons.wikimedia.org/wiki/File:Example.png",
                "license_name": "CC BY 4.0", "license_url": "https://creativecommons.org/licenses/by/4.0/",
                "crop_note": "居中裁切为主屏比例（原图不变）", **changes}

    def manifest(self, result):
        return json.loads((self.destination / result["manifest"]).read_text(encoding="utf-8"))

    def test_only_selected_copies_export_with_attribution_and_no_private_paths(self):
        chosen = self.record()
        unselected = self.record("未选中.png", "blue")
        original = Path(chosen["path"]).read_bytes()
        result = export_records([chosen], self.destination)
        self.assertEqual(result["count"], 1)
        self.assertEqual(result["errors"], [])
        self.assertEqual((self.destination / result["files"][0]).read_bytes(), original)
        self.assertEqual(Path(chosen["path"]).read_bytes(), original)
        self.assertNotIn(unselected["title"], " ".join(path.name for path in self.destination.iterdir()))
        manifest = self.manifest(result)
        metadata = manifest["records"][0]
        self.assertEqual(metadata["sha256"], hashlib.sha256(original).hexdigest())
        self.assertEqual(metadata["mime"], "image/png")
        self.assertEqual(metadata["author"], "照片作者")
        self.assertEqual(metadata["filename"], result["files"][0])
        self.assertNotIn("path", metadata)
        for key in ("manifest", "manifest_markdown"):
            content = (self.destination / result[key]).read_text(encoding="utf-8")
            self.assertNotIn(str(self.root), content)
            self.assertIn("CC BY 4.0", content)
            self.assertIn("居中裁切", content)

    def test_repeated_exports_never_overwrite_images_or_manifests(self):
        record = self.record()
        first = export_records([record], self.destination)
        originals = {path.name: path.read_bytes() for path in self.destination.iterdir()}
        second = export_records([record], self.destination)
        self.assertEqual(second["count"], 1)
        self.assertNotEqual(first["files"], second["files"])
        self.assertNotEqual(first["manifest"], second["manifest"])
        self.assertNotEqual(first["manifest_markdown"], second["manifest_markdown"])
        for name, content in originals.items():
            self.assertEqual((self.destination / name).read_bytes(), content)

    def test_unicode_titles_are_safe_windows_names_and_cannot_escape_directory(self):
        record = self.record(title="../../CON?.png:中文\\.. .png")
        result = export_records([record], self.destination)
        self.assertEqual(result["count"], 1)
        filename = result["files"][0]
        self.assertIn("中文", filename)
        self.assertFalse(any(char in filename for char in '<>:"/\\|?*'))
        self.assertFalse(filename.endswith((".", " ")))
        self.assertEqual((self.destination / filename).parent, self.destination)
        reserved = export_records([{**record, "title": "CON.png"}], self.destination)
        self.assertTrue(reserved["files"][0].startswith("图片_CON-"))

    def test_invalid_files_do_not_prevent_other_selected_copies(self):
        valid = self.record()
        corrupt = self.root / "损坏.jpg"
        corrupt.write_bytes(b"not an image")
        wrong_format = self.root / "伪装.png"
        Image.new("RGB", (180, 120)).save(wrong_format, format="GIF")
        result = export_records([{**valid, "path": str(corrupt)}, {**valid, "path": str(wrong_format)}, valid], self.destination)
        self.assertEqual(result["count"], 1)
        self.assertEqual(len(result["errors"]), 2)
        self.assertEqual(self.manifest(result)["count"], 1)

    def test_individual_byte_and_pixel_limits_reject_before_copy(self):
        record = self.record()
        with mock.patch.object(export_module, "MAX_IMAGE_BYTES", 10):
            result = export_records([record], self.destination)
        self.assertEqual(result["count"], 0)
        self.assertEqual(len(result["errors"]), 1)
        self.assertEqual(list(self.destination.iterdir()), [])
        with mock.patch.object(export_module, "MAX_IMAGE_PIXELS", 100):
            result = export_records([record], self.destination)
        self.assertEqual(result["count"], 0)
        self.assertEqual(list(self.destination.iterdir()), [])

    def test_count_and_total_limits_report_exact_completed_count(self):
        one = self.record()
        two = self.record("第二张.png", "blue")
        with mock.patch.object(export_module, "MAX_EXPORT_RECORDS", 1):
            result = export_records([one, two], self.destination)
        self.assertEqual(result["count"], 1)
        self.assertEqual(len(result["errors"]), 1)
        second_destination = self.root / "总量限制"
        with mock.patch.object(export_module, "MAX_EXPORT_BYTES", len(Path(one["path"]).read_bytes()) + 1):
            result = export_records([one, two], second_destination)
        self.assertEqual(result["count"], 1)
        self.assertEqual(len(result["errors"]), 1)
        self.assertEqual(result["total_bytes"], len(Path(one["path"]).read_bytes()))

    def test_pre_cancelled_or_empty_selection_writes_nothing(self):
        record = self.record()
        event = threading.Event()
        event.set()
        result = export_records([record], self.destination, event)
        self.assertTrue(result["cancelled"])
        self.assertEqual(result["count"], 0)
        self.assertFalse(self.destination.exists())
        result = export_records([], self.destination)
        self.assertEqual(result["count"], 0)
        self.assertFalse(self.destination.exists())

    def test_cancel_during_copy_removes_partial_created_file(self):
        class CancelAfterChecks:
            calls = 0

            def is_set(self):
                self.calls += 1
                return self.calls >= 4

        result = export_records([self.record()], self.destination, CancelAfterChecks())
        self.assertTrue(result["cancelled"])
        self.assertEqual(result["count"], 0)
        self.assertEqual(list(self.destination.iterdir()), [])

    def test_cancel_between_images_keeps_complete_copy_and_matching_manifest(self):
        class CancelAfterChecks:
            calls = 0

            def is_set(self):
                self.calls += 1
                return self.calls >= 5

        result = export_records([self.record(), self.record("第二张.png", "blue")], self.destination, CancelAfterChecks())
        self.assertTrue(result["cancelled"])
        self.assertEqual(result["count"], 1)
        self.assertEqual(self.manifest(result)["count"], 1)
        self.assertTrue(self.manifest(result)["cancelled"])

    def test_detected_destination_or_source_junction_is_rejected(self):
        record = self.record()
        real_detection = export_module._is_link_or_reparse

        def linked_destination(path):
            return path == self.destination or real_detection(path)

        with mock.patch.object(export_module, "_is_link_or_reparse", side_effect=linked_destination):
            result = export_records([record], self.destination)
        self.assertEqual(result["count"], 0)
        self.assertEqual(len(result["errors"]), 1)
        self.assertFalse(self.destination.exists())

        def linked_source(path):
            return path == Path(record["path"]) or real_detection(path)

        with mock.patch.object(export_module, "_is_link_or_reparse", side_effect=linked_source):
            result = export_records([record], self.destination)
        self.assertEqual(result["count"], 0)
        self.assertEqual(list(self.destination.iterdir()), [])

    def test_private_file_urls_are_not_saved_in_manifest(self):
        record = self.record(origin="local", source_url="file:///C:/private/image.png", license_url="file:///C:/secret/license.txt")
        result = export_records([record], self.destination)
        metadata = self.manifest(result)["records"][0]
        self.assertEqual(metadata["source_url"], "")
        self.assertEqual(metadata["license_url"], "")
        content = (self.destination / result["manifest_markdown"]).read_text(encoding="utf-8")
        self.assertIn("本地导入", content)
        self.assertNotIn("C:/", content)


if __name__ == "__main__":
    unittest.main()
