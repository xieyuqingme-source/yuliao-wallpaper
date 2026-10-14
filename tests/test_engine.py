"""Rotation safety tests: temporary images and a fake desktop adapter only.

No real Windows wallpaper APIs are imported, instantiated, or called here.
"""

from __future__ import annotations

import copy
import hashlib
import json
import random
import tempfile
import unittest
from pathlib import Path

from PIL import Image

from yuliao_wallpaper.engine import Rotation
from yuliao_wallpaper.store import Store


class FakeClock:
    def __init__(self, now=100.0):
        self.now = now

    def __call__(self):
        return self.now


class FakeWallpaper:
    """Records intended changes without inspecting or changing the desktop."""

    def __init__(self):
        self.events = []
        self.current = None
        self.size = (320, 180)
        self.fail_capture = False
        self.fail_apply = False
        self.fail_restore = False

    def screen_size(self):
        self.events.append(("screen_size",))
        return self.size

    def capture_original(self):
        self.events.append(("capture",))
        if self.fail_capture:
            raise OSError("simulated original capture failure")

    def apply(self, path):
        self.events.append(("apply", Path(path)))
        if self.fail_apply:
            raise OSError("simulated wallpaper apply failure")
        self.current = Path(path)

    def restore(self):
        self.events.append(("restore",))
        if self.fail_restore:
            raise OSError("simulated original restore failure")
        self.current = None

    @property
    def apply_attempts(self):
        return [event[1] for event in self.events if event[0] == "apply"]

    @property
    def capture_count(self):
        return sum(event[0] == "capture" for event in self.events)


class RotationTests(unittest.TestCase):
    def setUp(self):
        # Keep all writes within the explicitly writable workspace. Default
        # Windows temporary-directory redirection can deny atomic replacements.
        self.temporary = tempfile.TemporaryDirectory(
            prefix="engine-tests-", dir=Path(__file__).parent,
        )
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.store = Store(self.root / "private-data")
        self.store.settings.update(mode="local", interval_seconds=300)
        originals = []
        for index, color in enumerate(("red", "green", "blue")):
            path = self.root / f"local-{index}.png"
            Image.new("RGB", (640, 360), color).save(path)
            originals.append(path)
        count, errors = self.store.import_files(originals, "test-album", ["test-tag"])
        self.assertEqual((count, errors), (3, []))
        self.items = list(self.store.records.values())
        self.ids = [item["id"] for item in self.items]
        self.wallpaper = FakeWallpaper()
        self.clock = FakeClock()
        self.rotation = Rotation(
            self.store, self.wallpaper, clock=self.clock, rng=random.Random(41),
        )

    def enable_first(self):
        return self.rotation.enable(self.ids[0])

    def snapshot(self):
        return copy.deepcopy(self.store.state)

    def test_passive_initialization_and_settings_never_apply_wallpaper(self):
        self.rotation.reschedule()
        self.assertIsNone(self.rotation.tick(now=999999))
        self.rotation.pause()
        self.rotation.reschedule()
        self.assertIsNone(self.rotation.tick(now=999999))
        for action in (self.rotation.next, self.rotation.previous, self.rotation.resume):
            with self.assertRaises(ValueError):
                action()
        self.assertFalse(self.rotation.enabled)
        self.assertIsNone(self.rotation.due)
        self.assertEqual(self.wallpaper.events, [])

    def test_enable_captures_before_apply_and_persists_rendered_image(self):
        source = Path(self.items[0]["path"])
        original_bytes = source.read_bytes()
        result = self.enable_first()
        self.assertEqual(result["id"], self.ids[0])
        self.assertEqual(
            [event[0] for event in self.wallpaper.events],
            ["screen_size", "capture", "apply"],
        )
        self.assertTrue(self.rotation.enabled)
        self.assertFalse(self.rotation.paused)
        self.assertEqual(self.rotation.due, 400.0)
        self.assertEqual(self.store.state["current_id"], self.ids[0])
        self.assertEqual(self.store.state["history"], [self.ids[0]])
        self.assertEqual(Store(self.store.data_root).state, self.store.state)
        self.assertEqual(source.read_bytes(), original_bytes)
        with Image.open(self.wallpaper.current) as rendered:
            self.assertEqual((rendered.format, rendered.mode, rendered.size), ("BMP", "RGB", (320, 180)))

    def test_apply_selected_is_explicit_enable_and_does_not_capture_again(self):
        self.rotation.apply_selected(self.ids[0])
        self.rotation.apply_selected(self.ids[1])
        self.assertTrue(self.rotation.enabled)
        self.assertEqual(self.wallpaper.capture_count, 1)
        self.assertEqual(len(self.wallpaper.apply_attempts), 2)
        self.assertEqual(self.store.state["current_id"], self.ids[1])

    def test_unknown_explicit_selection_does_not_choose_another_image(self):
        before = self.snapshot()
        with self.assertRaises(ValueError):
            self.rotation.apply_selected("not-a-record")
        self.assertEqual(self.wallpaper.events, [])
        self.assertEqual(self.store.state, before)
        self.assertFalse(self.rotation.enabled)

    def test_blocked_explicit_selection_does_not_choose_another_image(self):
        self.items[0]["blocked"] = True
        before = self.snapshot()
        with self.assertRaises(ValueError):
            self.rotation.apply_selected(self.ids[0])
        self.assertEqual(self.wallpaper.events, [])
        self.assertEqual(self.store.state, before)
        self.assertFalse(self.rotation.enabled)

    def test_enabled_unknown_selection_preserves_current_and_schedule(self):
        self.enable_first()
        before, due = self.snapshot(), self.rotation.due
        self.clock.now = 200
        with self.assertRaises(ValueError):
            self.rotation.apply_selected("not-a-record")
        self.assertEqual(self.store.state, before)
        self.assertEqual(self.rotation.due, due)
        self.assertEqual(len(self.wallpaper.apply_attempts), 1)

    def test_missing_source_does_not_capture_or_modify_desktop(self):
        Path(self.items[0]["path"]).unlink()
        before = self.snapshot()
        with self.assertRaises(ValueError):
            self.enable_first()
        self.assertEqual(self.wallpaper.events, [])
        self.assertFalse(self.rotation.enabled)
        self.assertEqual(self.store.state, before)

    def test_corrupt_source_does_not_capture_or_modify_desktop(self):
        Path(self.items[0]["path"]).write_bytes(b"This is not an image.")
        before = self.snapshot()
        with self.assertRaises(OSError):
            self.enable_first()
        self.assertEqual(self.wallpaper.capture_count, 0)
        self.assertEqual(self.wallpaper.apply_attempts, [])
        self.assertFalse(self.rotation.enabled)
        self.assertEqual(self.store.state, before)

    def test_rejected_display_dimensions_do_not_capture_or_modify_desktop(self):
        self.wallpaper.size = (17000, 300)
        with self.assertRaises(ValueError):
            self.enable_first()
        self.assertEqual(self.wallpaper.capture_count, 0)
        self.assertEqual(self.wallpaper.apply_attempts, [])
        self.assertFalse(self.rotation.enabled)

    def test_failed_original_capture_refuses_desktop_change(self):
        self.wallpaper.fail_capture = True
        before = self.snapshot()
        with self.assertRaises(OSError):
            self.enable_first()
        self.assertEqual(self.wallpaper.capture_count, 1)
        self.assertEqual(self.wallpaper.apply_attempts, [])
        self.assertEqual(self.store.state, before)
        self.assertFalse(self.rotation.enabled)
        self.assertIsNone(self.rotation.due)

    def test_failed_first_apply_keeps_rotation_disabled_and_state_unchanged(self):
        self.wallpaper.fail_apply = True
        before = self.snapshot()
        persisted = self.store.config_path.read_bytes()
        with self.assertRaises(OSError):
            self.enable_first()
        self.assertEqual(self.wallpaper.capture_count, 1)
        self.assertEqual(self.store.state, before)
        self.assertEqual(self.store.config_path.read_bytes(), persisted)
        self.assertIsNone(self.wallpaper.current)
        self.assertFalse(self.rotation.enabled)
        self.assertIsNone(self.rotation.due)

    def test_rotation_cycles_visit_all_eligible_images_before_repeating(self):
        shown = [self.enable_first()["id"]]
        shown.extend(self.rotation.next()["id"] for _ in range(8))
        for start in (0, 3, 6):
            cycle = shown[start:start + 3]
            self.assertEqual(len(set(cycle)), 3)
            self.assertEqual(set(cycle), set(self.ids))
        self.assertTrue(all(first != second for first, second in zip(shown, shown[1:])))
        self.assertEqual(self.wallpaper.capture_count, 1)

    def test_large_album_does_not_forget_seen_images_before_cycle_finishes(self):
        # Model an imported collection with 2002 distinct record identities;
        # reuse the safe tiny fixture path to isolate selection from disk load.
        image_ids = [hashlib.sha256(f"large-album-{index}".encode()).hexdigest()
                     for index in range(2002)]
        self.store.records = {
            image_id: {**self.items[0], "id": image_id}
            for image_id in image_ids
        }
        self.store.state["seen"] = image_ids[:2000]
        self.store.state["current_id"] = image_ids[1999]
        self.rotation.rng.choice = lambda pool: pool[0]
        self.rotation.enable(image_ids[2000])
        self.assertEqual(self.rotation.next()["id"], image_ids[2001])

    def test_previous_and_next_skip_blocked_history_entries(self):
        for image_id in self.ids:
            self.rotation.apply_selected(image_id)
        self.items[1]["blocked"] = True
        self.assertEqual(self.rotation.previous()["id"], self.ids[0])
        self.assertEqual(self.store.state["history_index"], 0)
        self.assertEqual(self.rotation.next()["id"], self.ids[2])
        self.assertEqual(self.store.state["history_index"], 2)
        self.assertEqual(self.store.state["history"], self.ids)

    def test_previous_at_history_beginning_keeps_current_wallpaper(self):
        self.enable_first()
        before = self.snapshot()
        with self.assertRaises(ValueError):
            self.rotation.previous()
        self.assertEqual(self.store.state, before)
        self.assertEqual(len(self.wallpaper.apply_attempts), 1)

    def test_manual_mode_has_no_timer_but_allows_explicit_next(self):
        self.store.settings["interval_seconds"] = 0
        self.enable_first()
        self.assertIsNone(self.rotation.due)
        self.assertIsNone(self.rotation.tick(now=999999))
        self.assertEqual(len(self.wallpaper.apply_attempts), 1)
        self.rotation.next()
        self.assertEqual(len(self.wallpaper.apply_attempts), 2)
        self.assertIsNone(self.rotation.due)
        self.rotation.pause()
        self.rotation.resume()
        self.assertIsNone(self.rotation.due)

    def test_pause_and_resume_restart_interval_without_immediate_change(self):
        self.enable_first()
        self.rotation.pause()
        self.clock.now = 1000
        self.assertIsNone(self.rotation.tick())
        self.assertIsNone(self.rotation.due)
        self.rotation.resume()
        self.assertFalse(self.rotation.paused)
        self.assertEqual(self.rotation.due, 1300)
        self.assertEqual(len(self.wallpaper.apply_attempts), 1)
        self.assertIsNone(self.rotation.tick(now=1299))
        self.assertIsNotNone(self.rotation.tick(now=1300))
        self.assertEqual(len(self.wallpaper.apply_attempts), 2)

    def test_sleep_jump_applies_at_most_once_and_resets_from_observed_time(self):
        self.enable_first()
        self.assertIsNotNone(self.rotation.tick(now=100000))
        self.assertEqual(len(self.wallpaper.apply_attempts), 2)
        self.assertEqual(self.rotation.due, 100300)
        self.assertIsNone(self.rotation.tick(now=100000))
        self.assertIsNone(self.rotation.tick(now=100299))
        self.assertEqual(len(self.wallpaper.apply_attempts), 2)
        self.assertIsNotNone(self.rotation.tick(now=100300))
        self.assertEqual(len(self.wallpaper.apply_attempts), 3)
        self.assertEqual(self.rotation.due, 100600)

    def test_failed_manual_apply_keeps_current_file_state_and_schedule(self):
        self.enable_first()
        current = self.wallpaper.current
        current_bytes = current.read_bytes()
        before, due = self.snapshot(), self.rotation.due
        persisted = self.store.config_path.read_bytes()
        self.wallpaper.fail_apply = True
        with self.assertRaises(OSError):
            self.rotation.apply_selected(self.ids[1])
        self.assertEqual(self.wallpaper.current, current)
        self.assertEqual(current.read_bytes(), current_bytes)
        self.assertEqual(self.store.state, before)
        self.assertEqual(self.store.config_path.read_bytes(), persisted)
        self.assertEqual(self.rotation.due, due)
        self.assertTrue(self.rotation.enabled)
        self.assertFalse(self.rotation.paused)

    def test_failed_cycle_rollover_preserves_seen_and_current_state(self):
        self.enable_first()
        self.rotation.next()
        self.rotation.next()
        self.assertEqual(set(self.store.state["seen"]), set(self.ids))
        before, due = self.snapshot(), self.rotation.due
        current = self.wallpaper.current
        current_bytes = current.read_bytes()
        self.wallpaper.fail_apply = True
        with self.assertRaises(OSError):
            self.rotation.next()
        self.assertEqual(self.store.state, before)
        self.assertEqual(self.rotation.due, due)
        self.assertEqual(self.wallpaper.current, current)
        self.assertEqual(current.read_bytes(), current_bytes)

    def test_failed_tick_preserves_current_and_pauses_without_retry_storm(self):
        self.enable_first()
        before = self.snapshot()
        self.wallpaper.fail_apply = True
        with self.assertRaises(OSError):
            self.rotation.tick(now=400)
        self.assertEqual(self.store.state, before)
        self.assertTrue(self.rotation.enabled)
        self.assertTrue(self.rotation.paused)
        self.assertIsNone(self.rotation.due)
        self.assertIn("simulated", self.rotation.last_error)
        attempts = len(self.wallpaper.apply_attempts)
        self.assertIsNone(self.rotation.tick(now=999999))
        self.assertEqual(len(self.wallpaper.apply_attempts), attempts)
        self.wallpaper.fail_apply = False
        self.clock.now = 1000
        self.rotation.resume()
        self.assertEqual(self.rotation.due, 1300)
        self.assertEqual(len(self.wallpaper.apply_attempts), attempts)

    def test_restore_disables_rotation_and_cancels_schedule(self):
        self.enable_first()
        self.rotation.restore()
        self.assertEqual(self.wallpaper.events[-1][0], "restore")
        self.assertFalse(self.rotation.enabled)
        self.assertFalse(self.rotation.paused)
        self.assertIsNone(self.rotation.due)
        self.assertIsNone(self.rotation.tick(now=999999))
        self.assertEqual(len(self.wallpaper.apply_attempts), 1)

    def test_failed_restore_keeps_active_rotation_and_schedule(self):
        self.enable_first()
        before, due = self.snapshot(), self.rotation.due
        self.wallpaper.fail_restore = True
        with self.assertRaises(OSError):
            self.rotation.restore()
        self.assertTrue(self.rotation.enabled)
        self.assertEqual(self.rotation.due, due)
        self.assertEqual(self.store.state, before)

    def test_library_removal_preserves_active_desktop_and_current_id(self):
        self.enable_first()
        current = self.wallpaper.current
        current_bytes = current.read_bytes()
        source = Path(self.items[0]["path"])
        source_bytes = source.read_bytes()
        events, due = list(self.wallpaper.events), self.rotation.due
        self.assertEqual(self.store.remove_records([self.ids[0]]), 1)
        self.assertEqual(self.store.state["current_id"], self.ids[0])
        self.assertEqual(Store(self.store.data_root).state["current_id"], self.ids[0])
        self.assertNotIn(self.ids[0], [item["id"] for item in self.store.eligible()])
        self.assertTrue(self.store.records[self.ids[0]]["deleted"])
        self.assertEqual(self.wallpaper.current, current)
        self.assertEqual(self.wallpaper.events, events)
        self.assertEqual(self.rotation.due, due)
        self.assertEqual(current.read_bytes(), current_bytes)
        self.assertEqual(source.read_bytes(), source_bytes)

    def test_removed_history_entries_are_skipped_by_previous_and_next(self):
        for image_id in self.ids:
            self.rotation.apply_selected(image_id)
        events = list(self.wallpaper.events)
        self.store.remove_records([self.ids[1]])
        self.assertEqual(self.wallpaper.events, events)
        self.assertEqual(self.rotation.previous()["id"], self.ids[0])
        self.assertEqual(self.rotation.next()["id"], self.ids[2])
        self.store.remove_records([self.ids[2]])
        self.assertEqual(self.store.state["current_id"], self.ids[2])
        self.assertEqual(self.rotation.next()["id"], self.ids[0])
        self.assertNotIn(self.ids[1], self.store.state["history"])
        self.assertNotIn(self.ids[2], self.store.state["history"])

    def test_restore_removed_preserves_favorite_without_immediate_application(self):
        self.items[0]["favorite"] = True
        self.rotation.enable(self.ids[2])
        current = self.wallpaper.current
        events, due = list(self.wallpaper.events), self.rotation.due
        self.store.remove_records([self.ids[0]])
        self.assertEqual(self.store.restore_removed([self.ids[0]]), 1)
        restored = self.store.records[self.ids[0]]
        self.assertTrue(restored["favorite"])
        self.assertFalse(restored["deleted"])
        self.assertIn(self.ids[0], [item["id"] for item in self.store.eligible()])
        self.assertTrue(Store(self.store.data_root).records[self.ids[0]]["favorite"])
        self.assertEqual(self.wallpaper.events, events)
        self.assertEqual(self.wallpaper.current, current)
        self.assertEqual(self.rotation.due, due)
        self.assertEqual(self.store.state["current_id"], self.ids[2])
        self.rotation.apply_selected(self.ids[0])
        self.assertEqual(len(self.wallpaper.apply_attempts), 2)

    def test_reorder_visible_records_preserves_hidden_slots_and_active_desktop(self):
        self.items[0]["favorite"] = self.items[2]["favorite"] = True
        self.store.settings["favorites_only"] = True
        self.store.save()
        self.enable_first()
        hidden = copy.deepcopy(self.store.records[self.ids[1]])
        before = self.snapshot()
        events, current, due = list(self.wallpaper.events), self.wallpaper.current, self.rotation.due
        visible = [item["id"] for item in self.store.eligible()]
        self.assertEqual(visible, [self.ids[0], self.ids[2]])
        self.assertEqual(self.store.move_records([self.ids[2]], -1, visible_ids=visible), 1)
        self.assertEqual(self.store.state["order"], [self.ids[2], self.ids[1], self.ids[0]])
        self.assertEqual(self.store.records[self.ids[1]], hidden)
        self.assertEqual([item["id"] for item in self.store.eligible()], [self.ids[2], self.ids[0]])
        self.assertEqual(Store(self.store.data_root).state["order"], self.store.state["order"])
        for key in ("current_id", "history", "history_index", "seen", "render_slot"):
            self.assertEqual(self.store.state[key], before[key])
        self.assertEqual(self.wallpaper.events, events)
        self.assertEqual(self.wallpaper.current, current)
        self.assertEqual(self.rotation.due, due)

    def test_short_legacy_interval_migrates_to_five_minutes(self):
        payload = json.loads(self.store.config_path.read_text(encoding="utf-8"))
        payload["version"] = 1
        payload["settings"]["interval_seconds"] = 30
        payload["settings"].pop("themes", None)
        payload["settings"]["theme"] = "city"
        self.store.config_path.write_text(json.dumps(payload), encoding="utf-8")
        migrated = Store(self.store.data_root)
        self.assertEqual(migrated.settings["interval_seconds"], 300)
        self.assertEqual(migrated.settings["themes"], ["city"])
        migrated_rotation = Rotation(migrated, self.wallpaper, clock=self.clock)
        self.assertEqual(self.wallpaper.events, [])
        migrated_rotation.enable(self.ids[0])
        self.assertEqual(migrated_rotation.due, 400)

    def test_direct_rotation_model_enforces_minimum_interval_without_desktop_change(self):
        self.rotation.enabled = True
        self.store.settings["interval_seconds"] = 1
        self.rotation.reschedule()
        self.assertEqual(self.rotation.due, 400)
        self.store.settings["interval_seconds"] = 299
        self.rotation.reschedule()
        self.assertEqual(self.rotation.due, 400)
        self.store.settings["interval_seconds"] = 0
        self.rotation.reschedule()
        self.assertIsNone(self.rotation.due)
        self.assertEqual(self.wallpaper.events, [])

    def test_multiple_online_themes_form_union_for_rotation_and_history(self):
        for item, theme in zip(self.items, ("landscape", "city", "animals")):
            item.update(origin="online", theme=theme, themes=[theme], album="", tags=[])
        self.store.settings.update(mode="online", themes=["city", "animals"], theme="landscape")
        self.store.save()
        self.rotation.rng.choice = lambda pool: pool[0]
        self.assertEqual(self.rotation.enable()["id"], self.ids[1])
        self.assertEqual(self.rotation.next()["id"], self.ids[2])
        self.assertNotIn(self.ids[0], self.store.state["history"])
        events, due = list(self.wallpaper.events), self.rotation.due
        self.store.settings["themes"] = ["landscape", "animals"]
        self.store.save()
        self.assertEqual(self.wallpaper.events, events)
        self.assertEqual(self.rotation.due, due)
        with self.assertRaises(ValueError):
            self.rotation.previous()
        self.assertEqual(self.rotation.next()["id"], self.ids[0])

    def test_download_settings_save_preserves_wallpaper_timer(self):
        self.enable_first()
        self.clock.now = 350
        events = list(self.wallpaper.events)
        self.store.settings.update(download_hours=0, download_count=30)
        self.store.save()
        self.assertEqual(self.rotation.due, 400)
        self.assertEqual(self.wallpaper.events, events)
        self.assertIsNone(self.rotation.tick(now=399))
        self.assertIsNotNone(self.rotation.tick(now=400))
        self.assertEqual(self.rotation.due, 700)

    def test_all_removed_library_retains_current_wallpaper_and_stops_failed_timer(self):
        self.enable_first()
        current, events = self.wallpaper.current, list(self.wallpaper.events)
        self.assertEqual(self.store.remove_records(self.ids), 3)
        self.assertEqual(self.store.eligible(), [])
        self.assertEqual(self.store.state["current_id"], self.ids[0])
        with self.assertRaises(ValueError):
            self.rotation.tick(now=400)
        self.assertTrue(self.rotation.paused)
        self.assertIsNone(self.rotation.due)
        self.assertEqual(self.wallpaper.current, current)
        self.assertEqual(self.wallpaper.events, events)


if __name__ == "__main__":
    unittest.main()
