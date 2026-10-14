from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from PIL import Image

from yuliao_wallpaper import store as store_module
from yuliao_wallpaper.store import DEFAULT_SETTINGS, Store

ONLINE_A = "a" * 64
CITY = "b" * 64
BLOCKED = "c" * 64


class StoreTests(unittest.TestCase):
    def setUp(self):
        # Use the explicitly writable workspace: Windows sandbox redirection
        # of the default temporary directory can deny atomic file replacement.
        self.temporary = tempfile.TemporaryDirectory(prefix="store-tests-", dir=Path(__file__).parent)
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.data = self.root / "private-data"
        self.store = Store(self.data)

    def image(self, name="image.png", size=(180, 120), color="red", parent=None):
        path = (parent or self.root) / name
        Image.new("RGB", size, color).save(path)
        return path

    def online(self, record_id=ONLINE_A, theme="landscape", **changes):
        return {"id": record_id, "origin": "online", "theme": theme,
                "title": "在线图片",
                "path": str(self.root / f"{record_id}.jpg"),
                "favorite": False, "blocked": False, **changes}

    def test_defaults_are_independent_between_stores(self):
        self.assertEqual(self.store.settings, DEFAULT_SETTINGS)
        second = Store(self.root / "second")
        self.store.state["history"].append("example")
        self.assertEqual(second.state["history"], [])

    def test_import_portrait_preserves_original_and_persists_metadata(self):
        picture = self.image("portrait.png", (80, 140))
        original = picture.read_bytes()
        count, errors = self.store.import_files([picture], "旅行", ["海边", "海边"])
        self.assertEqual((count, errors), (1, []))
        record = next(iter(self.store.records.values()))
        self.assertEqual(record["path"], str(picture.absolute()))
        self.assertEqual(record["album"], "旅行")
        self.assertEqual(record["tags"], ["海边"])
        self.assertEqual(record["origin"], "local")
        self.assertEqual(len(record["id"]), 64)
        self.assertEqual(picture.read_bytes(), original)
        reloaded = Store(self.data)
        self.assertEqual(reloaded.records, self.store.records)
        self.assertEqual(reloaded.status, "")

    def test_content_deduplication_retains_favorite_and_blocked(self):
        picture = self.image()
        count, errors = self.store.import_files([picture])
        self.assertEqual((count, errors), (1, []))
        record = next(iter(self.store.records.values()))
        record["favorite"] = True
        record["blocked"] = True
        copy = self.root / "same-content.png"
        copy.write_bytes(picture.read_bytes())
        count, errors = self.store.import_files([copy], "新的相册", ["新标签"])
        self.assertEqual((count, errors), (0, []))
        record = self.store.records[record["id"]]
        self.assertTrue(record["favorite"])
        self.assertTrue(record["blocked"])
        self.assertEqual(record["title"], "same-content.png")
        self.assertEqual(record["album"], "新的相册")

    def test_malformed_small_large_and_wrong_format_are_rejected(self):
        corrupt = self.root / "broken.jpg"
        corrupt.write_bytes(b"not an image")
        small = self.image("small.png", (63, 100))
        disguised = self.root / "disguised.png"
        Image.new("RGB", (100, 100)).save(disguised, format="GIF")
        with mock.patch.object(store_module, "MAX_IMAGE_BYTES", 1024):
            big = self.root / "large.jpg"
            big.write_bytes(b"x" * 1025)
            count, errors = self.store.import_files([corrupt, small, disguised, big])
        self.assertEqual(count, 0)
        self.assertEqual(len(errors), 4)
        self.assertEqual(self.store.records, {})
        self.assertTrue(any("30 MB" in error for error in errors))

    def test_pixel_limit_and_decoder_bomb_warning_are_rejected(self):
        picture = self.image()
        with mock.patch.object(store_module, "MAX_IMAGE_PIXELS", 100):
            count, errors = self.store.import_files([picture])
        self.assertEqual(count, 0)
        self.assertIn("4000 万像素", errors[0])
        with mock.patch.object(Image, "MAX_IMAGE_PIXELS", 15000):
            count, errors = self.store.import_files([picture])
        self.assertEqual(count, 0)
        self.assertEqual(len(errors), 1)

    def test_folder_import_is_nonrecursive_and_bounded(self):
        folder = self.root / "selected"
        folder.mkdir()
        self.image("a.png", parent=folder)
        self.image("b.png", parent=folder, color="blue")
        nested = folder / "nested"
        nested.mkdir()
        self.image("hidden.png", parent=nested, color="green")
        (folder / "ignored.txt").write_text("ignored", encoding="utf-8")
        with mock.patch.object(store_module, "MAX_FOLDER_FILES", 1):
            count, errors = self.store.import_folder(folder)
        self.assertEqual(count, 1)
        self.assertTrue(any("单次最多导入" in error for error in errors))
        count, errors = self.store.import_folder(folder)
        self.assertEqual((count, errors), (1, []))
        self.assertEqual(len(self.store.records), 2)
        self.assertNotIn("hidden.png", [record["title"] for record in self.store.records.values()])

    def test_selected_symlink_is_not_followed(self):
        picture = self.image()
        link = self.root / "linked.png"
        try:
            link.symlink_to(picture)
        except (OSError, NotImplementedError):
            self.skipTest("This Windows account cannot create symbolic links")
        count, errors = self.store.import_files([link])
        self.assertEqual(count, 0)
        self.assertIn("符号链接", errors[0])

    def test_filters_narrow_sources_albums_tags_and_favorites(self):
        local = self.image()
        self.store.import_files([local], "旅行", ["山"])
        local_record = next(iter(self.store.records.values()))
        self.store.add_records([
            self.online(),
            self.online(CITY, "city"),
            self.online(BLOCKED, blocked=True),
        ])
        self.assertEqual({record["id"] for record in self.store.eligible()}, {local_record["id"], ONLINE_A})
        self.store.settings["mode"] = "online"
        self.assertEqual([record["id"] for record in self.store.eligible()], [ONLINE_A])
        self.store.settings.update(mode="mixed", album="旅行", tag="山")
        self.assertEqual([record["id"] for record in self.store.eligible()], [local_record["id"]])
        self.store.settings["tag"] = "不匹配"
        self.assertEqual(self.store.eligible(), [])
        self.store.settings.update(album="", tag="", favorites_only=True)
        self.assertEqual(self.store.eligible(), [])
        self.store.records[ONLINE_A]["favorite"] = True
        self.assertEqual([record["id"] for record in self.store.eligible()], [ONLINE_A])

    def test_state_round_trip_and_upsert_flags(self):
        self.store.add_records([self.online(favorite=True, blocked=True)])
        self.store.add_records([self.online(title="更新标题")])
        self.store.state.update(history=[ONLINE_A], history_index=0, seen=[ONLINE_A], current_id=ONLINE_A)
        self.store.settings.update(interval_seconds=0, download_hours=12)
        self.store.save()
        loaded = Store(self.data)
        self.assertEqual(loaded.state, self.store.state)
        self.assertEqual(loaded.settings, self.store.settings)
        self.assertTrue(loaded.records[ONLINE_A]["favorite"])
        self.assertTrue(loaded.records[ONLINE_A]["blocked"])
        self.assertEqual(loaded.records[ONLINE_A]["title"], "更新标题")
        self.assertIn("更新标题", loaded.config_path.read_text(encoding="utf-8"))

    def test_corrupt_config_is_preserved_in_unique_backups(self):
        broken = b"{not-json"
        self.store.config_path.write_bytes(broken)
        first = Store(self.data)
        second = Store(self.data)
        backups = list(self.data.glob("config.corrupt-*.json"))
        self.assertEqual(len(backups), 2)
        self.assertTrue(all(path.read_bytes() == broken for path in backups))
        self.assertEqual(first.settings, DEFAULT_SETTINGS)
        self.assertIn("已保留", first.status)
        first.save()
        self.assertEqual(json.loads(first.config_path.read_text(encoding="utf-8"))["settings"], DEFAULT_SETTINGS)
        self.assertTrue(all(path.read_bytes() == broken for path in backups))
        self.assertIn("已保留", second.status)

    def test_invalid_structure_is_recovered_and_saved_state_is_sanitized(self):
        self.store.config_path.write_text('{"records":[]}', encoding="utf-8")
        recovered = Store(self.data)
        self.assertEqual(recovered.records, {})
        self.assertTrue(recovered.warnings)
        recovered.add_records([self.online()])
        recovered.settings.update(mode="invalid", interval_seconds=-3, download_hours=float("inf"))
        recovered.state.update(history=["missing", ONLINE_A], history_index=999, seen=["missing", ONLINE_A, ONLINE_A], current_id="missing")
        recovered.save()
        loaded = Store(self.data)
        self.assertEqual(loaded.settings["mode"], "mixed")
        self.assertEqual(loaded.settings["interval_seconds"], 1800)
        self.assertEqual(loaded.settings["download_hours"], 6)
        self.assertEqual(loaded.state["history"], [ONLINE_A])
        self.assertEqual(loaded.state["history_index"], 0)
        self.assertEqual(loaded.state["seen"], [ONLINE_A])
        self.assertEqual(loaded.state["current_id"], "")

    def test_manual_download_zero_survives_restart(self):
        self.store.settings.update(download_hours=0, interval_seconds=0)
        self.store.save()
        loaded = Store(self.data)
        self.assertEqual(loaded.settings["download_hours"], 0)
        self.assertEqual(loaded.settings["interval_seconds"], 0)

    def test_shared_online_content_retains_all_themes_after_restart(self):
        self.store.add_records([self.online(theme="landscape", themes=["animals", "landscape"])])
        self.store.records[ONLINE_A]["favorite"] = True
        additions = self.store.add_records([self.online(theme="city", themes=["animals", "city"])])
        self.assertEqual(additions, 0)
        self.assertEqual(self.store.records[ONLINE_A]["themes"], ["landscape", "animals", "city"])
        self.assertTrue(self.store.records[ONLINE_A]["favorite"])
        self.store.save()
        loaded = Store(self.data)
        for theme in ("landscape", "animals", "city"):
            with self.subTest(theme=theme):
                loaded.settings.update(mode="online", themes=[theme])
                self.assertEqual([record["id"] for record in loaded.eligible()], [ONLINE_A])
        loaded.settings["themes"] = ["unknown"]
        self.assertEqual(loaded.eligible(), [])

    def test_v01_migration_changes_memory_only_and_backs_up_once(self):
        records = {ONLINE_A: self.online(), CITY: self.online(CITY, "city")}
        original = json.dumps({"version": 1, "settings": {"theme": "city", "interval_seconds": 1800},
                               "records": records, "state": {}}, ensure_ascii=False).encode("utf-8")
        self.store.config_path.write_bytes(original)
        loaded = Store(self.data)
        self.assertEqual(loaded.settings["themes"], ["city"])
        self.assertEqual(loaded.settings["download_count"], 10)
        self.assertEqual(loaded.state["order"], [ONLINE_A, CITY])
        self.assertEqual(loaded.config_path.read_bytes(), original)
        self.assertEqual(list(self.data.glob("config.pre-0.2-*.json")), [])
        loaded.save()
        backups = list(self.data.glob("config.pre-0.2-*.json"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_bytes(), original)
        self.assertEqual(json.loads(loaded.config_path.read_text(encoding="utf-8"))["version"], 2)
        Store(self.data).save()
        self.assertEqual(len(list(self.data.glob("config.pre-0.2-*.json"))), 1)

    def test_failed_v01_backup_refuses_config_replacement(self):
        original = b'{"version":1,"settings":{"theme":"animals"}}'
        self.store.config_path.write_bytes(original)
        loaded = Store(self.data)
        with mock.patch.object(store_module.os, "fsync", side_effect=OSError("backup cannot flush")):
            with self.assertRaisesRegex(OSError, "旧配置备份失败"):
                loaded.save()
        self.assertEqual(loaded.config_path.read_bytes(), original)
        self.assertEqual(list(self.data.glob("config.pre-0.2-*.json")), [])
        loaded.save()
        self.assertEqual(len(list(self.data.glob("config.pre-0.2-*.json"))), 1)

    def test_migration_preserves_history_cursor_across_missing_entries(self):
        records = {ONLINE_A: self.online(), CITY: self.online(CITY), BLOCKED: self.online(BLOCKED)}
        original = {"version": 1, "records": records, "state": {
            "history": [ONLINE_A, "missing", CITY, BLOCKED], "history_index": 2,
            "order": [BLOCKED, "missing", ONLINE_A, BLOCKED],
        }}
        self.store.config_path.write_text(json.dumps(original), encoding="utf-8")
        loaded = Store(self.data)
        self.assertEqual(loaded.state["history"], [ONLINE_A, CITY, BLOCKED])
        self.assertEqual(loaded.state["history_index"], 1)
        self.assertEqual(loaded.state["history"][loaded.state["history_index"]], CITY)
        self.assertEqual(loaded.state["order"], [BLOCKED, ONLINE_A, CITY])
        loaded.save()
        self.assertEqual(Store(self.data).state["history_index"], 1)

    def test_multitheme_or_and_limits_survive_restart(self):
        self.store.add_records([self.online(), self.online(CITY, "city"), self.online(BLOCKED, "animals")])
        self.store.settings.update(mode="online", themes=["landscape", "city", "city"],
                                   interval_seconds=60, download_count=999)
        self.store.save()
        loaded = Store(self.data)
        self.assertEqual(loaded.settings["themes"], ["landscape", "city"])
        self.assertEqual(loaded.settings["interval_seconds"], 300)
        self.assertEqual(loaded.settings["download_count"], 30)
        self.assertEqual([record["id"] for record in loaded.eligible()], [ONLINE_A, CITY])

    def test_move_blocks_and_visible_slots_persist(self):
        ids = [f"{value:064x}" for value in range(1, 6)]
        self.store.add_records([self.online(key) for key in ids])
        self.assertEqual(self.store.move_records(ids[1:3], -1), 2)
        self.assertEqual(self.store.state["order"], [ids[1], ids[2], ids[0], ids[3], ids[4]])
        self.assertEqual(self.store.move_records([ids[4]], -1, visible_ids=(ids[1], ids[0], ids[4])), 1)
        self.assertEqual(self.store.state["order"], [ids[1], ids[2], ids[4], ids[3], ids[0]])
        loaded = Store(self.data)
        self.assertEqual([record["id"] for record in loaded.eligible()], self.store.state["order"])
        self.assertEqual(loaded.move_records([ids[1]], -1), 0)

    def test_soft_remove_preserves_files_favorites_current_and_cleans_history(self):
        picture = self.image()
        original = picture.read_bytes()
        self.store.import_files([picture])
        local_id = next(iter(self.store.records))
        self.store.add_records([self.online()])
        self.store.records[local_id]["favorite"] = True
        self.store.state.update(history=[ONLINE_A, local_id, ONLINE_A], history_index=1,
                                seen=[local_id, ONLINE_A], current_id=local_id)
        self.assertEqual(self.store.remove_records([local_id]), 1)
        loaded = Store(self.data)
        self.assertEqual(picture.read_bytes(), original)
        self.assertTrue(loaded.records[local_id]["favorite"])
        self.assertTrue(loaded.records[local_id]["deleted"])
        self.assertEqual(loaded.state["current_id"], local_id)
        self.assertEqual(loaded.state["history"], [ONLINE_A, ONLINE_A])
        self.assertEqual(loaded.state["history_index"], 0)
        self.assertEqual(loaded.state["seen"], [ONLINE_A])
        self.assertEqual([record["id"] for record in loaded.eligible()], [ONLINE_A])
        self.assertEqual(loaded.restore_removed([local_id]), 1)
        self.assertTrue(loaded.records[local_id]["favorite"])
        self.assertEqual([record["id"] for record in loaded.ordered_records()], [local_id, ONLINE_A])

    def test_removed_online_tombstone_blocks_refresh_and_local_reimport_restores(self):
        self.store.add_records([self.online(favorite=True)])
        self.store.remove_records([ONLINE_A])
        self.assertEqual(self.store.add_records([self.online(title="新的在线标题")]), 0)
        self.assertEqual(self.store.records[ONLINE_A]["title"], "在线图片")
        self.assertEqual(self.store.eligible(), [])
        self.assertEqual(self.store.add_records([self.online()], restore_removed=True), 1)
        self.assertTrue(self.store.records[ONLINE_A]["favorite"])
        picture = self.image()
        self.store.import_files([picture])
        local_id = next(key for key in self.store.records if key != ONLINE_A)
        self.store.records[local_id]["favorite"] = True
        self.store.remove_records([local_id])
        self.assertEqual(self.store.import_files([picture]), (1, []))
        self.assertFalse(self.store.records[local_id]["deleted"])
        self.assertTrue(self.store.records[local_id]["favorite"])

    def test_missing_or_invalid_record_fields_trigger_recovery_backup(self):
        for damaged in (
            {"id": ONLINE_A, "origin": "online", "title": "图片"},
            {"id": ONLINE_A, "origin": "online", "title": 3, "path": str(self.root / "image.jpg")},
            {"id": "bad", "origin": "online", "title": "图片", "path": str(self.root / "image.jpg")},
            {"id": ONLINE_A, "origin": "invalid", "title": "图片", "path": str(self.root / "image.jpg")},
        ):
            with self.subTest(damaged=damaged):
                self.store.config_path.write_text(json.dumps({"records": {damaged["id"]: damaged}}), encoding="utf-8")
                recovered = Store(self.data)
                self.assertEqual(recovered.records, {})
                self.assertTrue(recovered.warnings)
        self.assertEqual(len(list(self.data.glob("config.corrupt-*.json"))), 4)

    def test_atomic_replace_failure_keeps_previous_config_and_cleans_temporary(self):
        self.store.save()
        previous = self.store.config_path.read_bytes()
        self.store.settings["album"] = "新相册"
        with mock.patch.object(store_module.os, "replace", side_effect=OSError("simulated failure")):
            with self.assertRaises(OSError):
                self.store.save()
        self.assertEqual(self.store.config_path.read_bytes(), previous)
        self.assertEqual(list(self.data.glob("config-*.tmp")), [])


if __name__ == "__main__":
    unittest.main()
