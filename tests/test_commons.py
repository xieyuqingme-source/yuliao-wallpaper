"""Offline security/caching tests; no real downloads or wallpaper changes."""
from email.message import Message
import hashlib
import io
import json
from pathlib import Path
import random
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit

from PIL import Image

from yuliao_wallpaper import commons


def metadata(name="CC BY-SA 4.0", url="https://creativecommons.org/licenses/by-sa/4.0/"):
    return {"Artist": {"value": '<a href="https://example.com">Alice &amp; Bob</a>'},
            "LicenseShortName": {"value": name}, "LicenseUrl": {"value": url}}


def image_bytes(size=(1280, 720), fmt="JPEG", color=(42, 70, 91)):
    buffer = io.BytesIO()
    Image.new("RGB", size, color).save(buffer, fmt)
    return buffer.getvalue()


def api_page(meta=None, **overrides):
    info = {"width": 3000, "height": 2000, "size": 12345, "mime": "image/jpeg", "mediatype": "BITMAP",
            "url": "https://upload.wikimedia.org/wikipedia/commons/a/ab/Test.jpg",
            "thumburl": "https://upload.wikimedia.org/wikipedia/commons/thumb/a/ab/Test.jpg/1920px-Test.jpg",
            "descriptionurl": "https://commons.wikimedia.org/wiki/File:Test.jpg",
            "extmetadata": metadata() if meta is None else meta}
    info.update(overrides)
    return {"title": "File:Test.jpg", "imageinfo": [info]}


class Response:
    def __init__(self, data, mime="image/jpeg", url="https://upload.wikimedia.org/test.jpg", length=None):
        self.data = io.BytesIO(data)
        self.url = url
        self.headers = Message()
        self.headers["Content-Type"] = mime
        if length is not None:
            self.headers["Content-Length"] = str(length)

    def read(self, size):
        return self.data.read(size)

    def geturl(self):
        return self.url

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class MetadataTests(unittest.TestCase):
    def test_metadata_is_bounded_text_without_active_content(self):
        dirty = '<script>do_bad()</script><style>evil</style><p>A &amp; B</p><img onerror="bad">雪'
        self.assertEqual(commons.clean_text(dirty), "A & B 雪")
        self.assertEqual(commons.clean_text("a" * 10_000, 20), "a" * 20)
        self.assertEqual(commons.clean_text({"value": "x"}), "")

    def test_only_conservative_official_licenses_are_accepted(self):
        valid = [
            ("CC BY-SA 4.0", "http://creativecommons.org/licenses/by-sa/4.0/"),
            ("CC BY 3.0", "//creativecommons.org/licenses/by/3.0/"),
            ("CC0", "https://creativecommons.org/publicdomain/zero/1.0/"),
            ("Public domain", "https://creativecommons.org/publicdomain/mark/1.0/"),
        ]
        for name, url in valid:
            with self.subTest(name=name):
                self.assertIsNotNone(commons.verified_license(metadata(name, url)))
        invalid = [
            ("CC BY-NC 4.0", "https://creativecommons.org/licenses/by-nc/4.0/"),
            ("CC BY-ND 4.0", "https://creativecommons.org/licenses/by-nd/4.0/"),
            ("CC BY-SA 4.0", "https://creativecommons.org.evil.example/licenses/by-sa/4.0/"),
            ("CC BY-SA 4.0", "https://creativecommons.org/licenses/by/4.0/"),
            ("CC BY-SA 4.0", "https://creativecommons.org/licenses/by-sa/4.0/?next=evil"),
            ("Public domain", ""), ("Copyrighted", ""),
        ]
        for name, url in invalid:
            with self.subTest(name=name, url=url):
                self.assertIsNone(commons.verified_license(metadata(name, url)))
        pd = metadata("Public domain", "")
        pd["Copyrighted"] = {"value": "False"}
        self.assertEqual(commons.verified_license(pd)[0], "Public domain")

    def test_download_url_rejects_unsafe_schemes_hosts_ports_and_credentials(self):
        for url in ["http://upload.wikimedia.org/x", "file:///C:/x", "https://evil.example/x",
                    "https://upload.wikimedia.org.evil.example/x", "https://upload.wikimedia.org:444/x",
                    "https://thumb.wikimedia.org.evil.example/x", "https://random.wikimedia.org/x",
                    "https://user:secret@upload.wikimedia.org/x", "https://upload.wikimedia.org/x#frag",
                    "https://upload.wikimedia.org:bad/x", "https://upload.wikimedia.org/x\n"]:
            with self.subTest(url=url), self.assertRaises(commons.CommonsError):
                commons.validate_download_url(url)
        self.assertEqual(commons.validate_download_url("https://upload.wikimedia.org/a.jpg"),
                         "https://upload.wikimedia.org/a.jpg")
        self.assertEqual(commons.validate_download_url("https://thumb.wikimedia.org/a.jpg"),
                         "https://thumb.wikimedia.org/a.jpg")

    def test_redirect_is_checked_before_following(self):
        with self.assertRaises(commons.CommonsError):
            commons._SafeRedirect().redirect_request(None, None, 302, "", {}, "https://evil.example/track")

    def test_images_reject_wrong_mime_corruption_and_portraits(self):
        data = image_bytes()
        self.assertEqual(commons.validate_image(data, "image/jpeg"), (".jpg", 1280, 720))
        for body, mime in [(data, "image/png"), (b"<html>not an image</html>", "image/jpeg"),
                           (data[:100], "image/jpeg"), (image_bytes((720, 1280)), "image/jpeg"),
                           (image_bytes((1200, 700)), "image/jpeg")]:
            with self.subTest(mime=mime, size=len(body)), self.assertRaises(commons.CommonsError):
                commons.validate_image(body, mime)
        with patch.object(commons, "MAX_PIXELS", 100), self.assertRaises(commons.CommonsError):
            commons.validate_image(data, "image/jpeg")

    def test_decompression_bomb_warning_is_an_error(self):
        with patch.object(Image, "MAX_IMAGE_PIXELS", 500_000), self.assertRaises(commons.CommonsError):
            commons.validate_image(image_bytes(), "image/jpeg")


class ClientTests(unittest.TestCase):
    def setUp(self):
        # Windows sandbox redirects the default temp folder to an AppContainer
        # location with different rename permissions. Keep test I/O in workspace.
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.root = Path(self.temp.name)
        self.client = commons.CommonsClient(self.root)

    def tearDown(self):
        self.temp.cleanup()

    def install_payload(self):
        self.client._api = Mock(return_value={"query": {"pages": [api_page()]},
                                              "continue": {"gcmcontinue": "file|Test.jpg|123"}})
        self.client._request_once = Mock(return_value=(image_bytes(), "image/jpeg"))

    def test_fetch_returns_usable_metadata_and_persists_content_named_files(self):
        self.install_payload()
        records = self.client.fetch("landscape")
        self.assertEqual(len(records), 1)
        record = records[0]
        self.assertEqual(set(record), set(commons.PUBLIC_KEYS))
        self.assertEqual(record["author"], "Alice & Bob")
        self.assertEqual(record["origin"], "online")
        self.assertEqual(record["id"], hashlib.sha256(image_bytes()).hexdigest())
        self.assertTrue(Path(record["path"]).is_absolute())
        self.assertTrue(Path(record["path"]).is_file())
        self.assertEqual(self.client._state["themes"]["landscape"]["cursor"], "file|Test.jpg|123")
        reloaded = commons.CommonsClient(self.root)
        reloaded._api = Mock(side_effect=AssertionError("must use cache"))
        self.assertEqual(reloaded.fetch("landscape"), records)

    def test_current_official_thumbnail_host_is_accepted_without_broadening_domains(self):
        candidate = self.client._candidate(api_page(thumburl="https://thumb.wikimedia.org/test.jpg"), "landscape")
        self.assertIsNotNone(candidate)
        self.assertIsNone(self.client._candidate(api_page(thumburl="https://other.wikimedia.org/test.jpg"), "landscape"))

    def test_force_never_bypasses_six_hour_query_cooldown(self):
        self.install_payload()
        first = self.client.fetch("landscape")
        self.assertEqual(self.client.fetch("landscape", force=True), first)
        self.client._api.assert_called_once()
        self.client._request_once.assert_called_once()

    def test_no_redownload_for_already_cached_source_after_refresh_interval(self):
        self.install_payload()
        self.client.fetch("city")
        self.client._state["themes"]["city"]["last_attempt"] -= commons.QUERY_INTERVAL + 1
        self.client.fetch("city")
        self.assertEqual(self.client._api.call_count, 2)
        self.client._request_once.assert_called_once()

    def test_shared_source_can_appear_in_two_themes_without_redownload(self):
        self.install_payload()
        landscape = self.client.fetch("landscape")[0]
        city = self.client.fetch("city")[0]
        self.assertEqual(landscape["id"], city["id"])
        self.assertEqual(city["theme"], "city")
        self.client._request_once.assert_called_once()

    def test_failed_refresh_preserves_cache_and_persists_failure_cooldown(self):
        self.install_payload()
        first = self.client.fetch("animals")
        self.client._state["themes"]["animals"]["last_attempt"] -= commons.QUERY_INTERVAL + 1
        self.client._api = Mock(side_effect=commons.CommonsError("offline"))
        self.assertEqual(self.client.fetch("animals"), first)
        self.assertEqual(self.client.last_error, "offline")
        reloaded = commons.CommonsClient(self.root)
        reloaded._api = Mock(side_effect=AssertionError("failure cooldown must survive restart"))
        self.assertEqual(reloaded.fetch("animals"), first)

    def test_unlicensed_or_unsafe_candidates_are_not_downloaded(self):
        bad = [api_page(meta=metadata("All rights reserved", "")),
               api_page(thumburl="https://evil.example/photo.jpg"),
               api_page(width=700, height=1200), api_page(mime="image/svg+xml"),
               api_page(descriptionurl="https://commons.wikimedia.org/wiki/User:Someone")]
        self.client._api = Mock(return_value={"query": {"pages": bad}})
        self.client._request_once = Mock(side_effect=AssertionError("candidate must be skipped"))
        self.assertEqual(self.client.fetch("city"), [])
        self.client._request_once.assert_not_called()
        self.assertIn("筛选", self.client.last_status)

    def test_malformed_api_structure_is_recoverable(self):
        self.client._api = Mock(return_value={"query": []})
        self.assertEqual(self.client.fetch("city"), [])
        self.assertIn("格式", self.client.last_error)
        self.assertIsNone(self.client._candidate({"imageinfo": [None]}, "city"))
        self.assertIsNone(self.client._candidate(api_page(meta=[]), "city"))

    def test_quota_is_hard_limit_and_keeps_existing_files(self):
        self.install_payload()
        sentinel = self.client.images / "leave-me.txt"
        sentinel.write_text("existing cache")
        with patch.object(commons, "MAX_CACHE_BYTES", 10):
            self.assertEqual(self.client.fetch("city"), [])
        self.assertTrue(sentinel.is_file())
        self.assertIn("200 MB", self.client.last_error)
        self.assertFalse(list(self.client.images.glob("*.jpg")))

    def test_index_path_escape_is_rejected_and_untrusted_absolute_path_is_ignored(self):
        self.install_payload()
        record = self.client.fetch("city")[0]
        state = json.loads(self.client.index.read_text(encoding="utf-8"))
        state["records"][0]["path"] = "C:/private/do-not-read.jpg"
        escaped = dict(state["records"][0])
        escaped["filename"] = "../../private.jpg"
        state["records"].append(escaped)
        self.client.index.write_text(json.dumps(state), encoding="utf-8")
        reloaded = commons.CommonsClient(self.root)
        self.assertEqual(len(reloaded._state["records"]), 1)
        self.assertEqual(reloaded._cached("city", 4)[0]["path"], record["path"])
        with self.assertRaises(commons.CommonsError):
            self.client._safe_file("../outside.jpg")

    def test_stream_byte_limit_with_missing_content_length(self):
        self.client._opener.open = Mock(return_value=Response(b"x" * 100))
        with self.assertRaises(commons.CommonsError):
            self.client._request_once("https://upload.wikimedia.org/test.jpg", 99, {"image/jpeg"})

    def test_content_type_length_and_final_redirect_are_validated(self):
        responses = [Response(b"jpg", mime="text/html"), Response(b"jpg", length=100),
                     Response(b"jpg", url="https://evil.example/photo.jpg")]
        for response in responses:
            self.client._opener.open = Mock(return_value=response)
            with patch("yuliao_wallpaper.commons.time.sleep"), self.assertRaises(commons.CommonsError):
                self.client._request_once("https://upload.wikimedia.org/test.jpg", 10, {"image/jpeg"})

    def test_api_uses_batch_small_candidates_maxlag_and_html_metadata_filter(self):
        body = json.dumps({"query": {"pages": []}}).encode()
        self.client._opener.open = Mock(return_value=Response(body, "application/json", commons.API_URL))
        self.client.fetch("landscape")
        request = self.client._opener.open.call_args.args[0]
        params = parse_qs(urlsplit(request.full_url).query)
        self.assertEqual(params["maxlag"], ["5"])
        self.assertEqual(params["gcmlimit"], ["12"])
        self.assertIn("extmetadata", params["iiprop"][0])
        self.assertIn("YuliaoWallpaper", request.get_header("User-agent"))
        self.assertEqual(self.client._state["themes"]["landscape"]["category_index"], 1)

    def test_transient_http_errors_retry_with_exponential_backoff(self):
        operation = Mock(side_effect=[HTTPError(commons.API_URL, 503, "busy", Message(), None),
                                      HTTPError(commons.API_URL, 503, "busy", Message(), None), "ok"])
        with patch("yuliao_wallpaper.commons.time.sleep") as sleep:
            self.assertEqual(self.client._retry(operation), "ok")
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [2, 4])

    def test_maxlag_retries_then_uses_cache(self):
        self.client._request_once = Mock(return_value=(b'{"error":{"code":"maxlag"}}', "application/json"))
        with patch("yuliao_wallpaper.commons.time.sleep") as sleep:
            self.assertEqual(self.client.fetch("landscape"), [])
        self.assertEqual(self.client._request_once.call_count, 3)
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [5, 5])
        self.assertIn("繁忙", self.client.last_error)


class MultiThemeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.root = Path(self.temp.name)
        self.client = commons.CommonsClient(self.root, rng=random.Random(19))

    def tearDown(self):
        self.temp.cleanup()

    def install_many(self, client=None):
        client = client or self.client
        counter = [0]

        def payload(params):
            count = int(params["gcmlimit"])
            start = counter[0]
            counter[0] += count
            pages = []
            for number in range(start, start + count):
                page = api_page(thumburl=f"https://thumb.wikimedia.org/image-{number}.jpg",
                                descriptionurl=f"https://commons.wikimedia.org/wiki/File:Image-{number}.jpg",
                                sha1=hashlib.sha1(str(number).encode()).hexdigest())
                page["title"] = f"File:Image-{number}.jpg"
                pages.append(page)
            return {"query": {"pages": pages}, "continue": {"gcmcontinue": f"file|next|{counter[0]}"}}

        def download(url, max_bytes, mimes):
            number = int(urlsplit(url).path.removeprefix("/image-").removesuffix(".jpg"))
            color = (number * 31 % 256, number * 73 % 256, number * 127 % 256)
            return image_bytes(color=color), "image/jpeg"

        client._api = Mock(side_effect=payload)
        client._request_once = Mock(side_effect=download)

    def test_supported_eight_themes_do_not_imply_free_licenses(self):
        self.assertEqual(set(commons.THEMES), {"landscape", "city", "animals", "space",
                                              "technology", "ocean", "flowers", "mountains"})
        invalid = api_page(meta=metadata("NASA", ""))
        self.client._api = Mock(return_value={"query": {"pages": [invalid]}})
        self.client._request_once = Mock(side_effect=AssertionError("NASA is not automatically public domain"))
        self.assertEqual(self.client.fetch_many(["space"], limit=4), [])
        self.assertEqual(self.client.last_stats["themes"]["space"]["filtered"], len(commons.THEMES["space"]))
        self.assertIn("筛选", self.client.last_status)

    def test_quantity_limits_and_no_extra_image_downloads(self):
        for requested, expected in [(4, 4), (10, 10), (20, 20), (23, 23), (99, 30)]:
            with self.subTest(requested=requested):
                client = commons.CommonsClient(self.root / str(requested), rng=random.Random(19))
                self.install_many(client)
                result = client.fetch_many(["landscape"], requested)
                self.assertEqual(len(result), expected)
                self.assertEqual(client._request_once.call_count, expected)
                self.assertEqual(len(list(client.images.glob("*.jpg"))), expected)
                self.assertEqual(client.last_stats["downloaded"], expected)
                self.assertEqual(len({r["id"] for r in result}), expected)

    def test_multiple_themes_are_interleaved_and_seed_is_reproducible(self):
        self.install_many()
        result = self.client.fetch_many(["landscape", "city", "animals"], 6)
        self.assertEqual({theme: sum(r["theme"] == theme for r in result)
                          for theme in ("landscape", "city", "animals")},
                         {"landscape": 2, "city": 2, "animals": 2})
        replay = commons.CommonsClient(self.root / "replay", rng=random.Random(19))
        self.install_many(replay)
        replay_result = replay.fetch_many(["landscape", "city", "animals"], 6)
        self.assertEqual([(r["id"], r["theme"]) for r in result],
                         [(r["id"], r["theme"]) for r in replay_result])
        self.assertEqual([c.args[0]["gcmtitle"] for c in self.client._api.call_args_list],
                         [c.args[0]["gcmtitle"] for c in replay._api.call_args_list])

    def test_cancel_before_start_has_no_network_or_cooldown_write(self):
        event = threading.Event()
        event.set()
        self.client._api = Mock(side_effect=AssertionError("cancelled job must not query"))
        self.assertEqual(self.client.fetch_many(["city"], cancel_event=event), [])
        self.assertTrue(self.client.last_stats["cancelled"])
        self.assertFalse(self.client.index.exists())
        self.client._api.assert_not_called()

    def test_cancel_after_one_committed_image_returns_durable_partial_result(self):
        self.install_many()
        event = threading.Event()
        messages = []

        def progress(message):
            messages.append(message)
            if message.startswith("已保存 1/"):
                event.set()

        result = self.client.fetch_many(["flowers"], 10, event, progress)
        self.assertEqual(len(result), 1)
        self.assertTrue(self.client.last_stats["cancelled"])
        self.client._request_once.assert_called_once()
        restored = commons.CommonsClient(self.root)
        self.assertEqual(len(restored._state["records"]), 1)
        restored._api = Mock(side_effect=AssertionError("cancelled attempt gate survives restart"))
        self.assertEqual(restored.fetch_many(["flowers"], 10)[0]["id"], result[0]["id"])
        self.assertEqual(restored.last_stats["downloaded"], 0)
        self.assertIn("已取消", messages[-1])

    def test_cancel_during_chunk_read_counts_bytes_but_commits_no_file(self):
        event = threading.Event()

        class CancelResponse(Response):
            def read(self, size):
                body = super().read(size)
                event.set()
                return body

        payload = json.dumps({"query": {"pages": [api_page()]}}).encode()
        self.client._opener.open = Mock(side_effect=[Response(payload, "application/json", commons.API_URL),
                                                    CancelResponse(image_bytes())])
        with patch.object(self.client, "_wait", side_effect=lambda seconds: self.client._check_cancel()):
            result = self.client.fetch_many(["city"], 4, cancel_event=event)
        self.assertEqual(result, [])
        self.assertTrue(self.client.last_stats["cancelled"])
        self.assertGreater(self.client.last_stats["http_bytes"], len(payload))
        self.assertFalse(list(self.client.images.iterdir()))

    def test_backoff_wait_is_cancellable(self):
        event = Mock()
        event.is_set.return_value = False
        event.wait.return_value = True
        self.client._job = {"cancel_event": event, "progress": None, "http_bytes": 0}
        operation = Mock(side_effect=HTTPError(commons.API_URL, 503, "busy", Message(), None))
        with self.assertRaises(commons._Cancelled):
            self.client._retry(operation)
        operation.assert_called_once()
        event.wait.assert_called_once_with(2)
        self.client._job = None

    def test_api_and_image_reads_share_strict_job_byte_budget(self):
        payload = json.dumps({"query": {"pages": [api_page()]}}).encode()
        image = image_bytes()
        ceiling = len(payload) + len(image) - 1
        self.client._opener.open = Mock(side_effect=[Response(payload, "application/json", commons.API_URL),
                                                    Response(image)])
        with patch.object(commons, "MAX_JOB_BYTES", ceiling), patch("yuliao_wallpaper.commons.time.sleep"):
            self.assertEqual(self.client.fetch_many(["city"], 4), [])
        self.assertTrue(self.client.last_stats["limited"])
        self.assertEqual(self.client.last_stats["http_bytes"], ceiling)
        self.assertFalse(list(self.client.images.glob("*.jpg")))

    def test_byte_budget_includes_failed_retry_response_reads(self):
        error_body = b'{"error":{"code":"maxlag"}}'
        self.client._opener.open = Mock(side_effect=lambda *args, **kwargs:
                                       Response(error_body, "application/json", commons.API_URL))
        with patch("yuliao_wallpaper.commons.time.sleep"):
            self.assertEqual(self.client.fetch_many(["space"], 4), [])
        self.assertEqual(self.client.last_stats["http_bytes"], len(error_body) * 3)
        self.assertEqual(self.client.last_stats["themes"]["space"]["failed"], 1)

    def test_candidate_budget_stops_empty_filter_batches(self):
        bad = api_page(meta=metadata("Unknown license", ""))
        self.client._api = Mock(return_value={"query": {"pages": [bad] * 12},
                                              "continue": {"gcmcontinue": "file|more|1"}})
        self.client._request_once = Mock(side_effect=AssertionError("no valid candidates"))
        self.assertEqual(self.client.fetch_many(["landscape"], 30), [])
        self.assertEqual(self.client.last_stats["candidates"], 240)
        self.assertEqual(self.client._api.call_count, 20)
        self.assertTrue(self.client.last_stats["limited"])

    def test_empty_continuation_pages_cannot_loop_forever(self):
        self.client._api = Mock(return_value={"query": {"pages": []},
                                              "continue": {"gcmcontinue": "file|unchanged|1"}})
        self.assertEqual(self.client.fetch_many(["space"], 30), [])
        self.assertEqual(self.client._api.call_count, 20)
        self.assertEqual(self.client.last_stats["candidates"], 0)
        self.assertEqual(self.client.last_stats["requested_candidates"], 240)
        self.assertTrue(self.client.last_stats["limited"])

    def test_many_reuses_cross_theme_source_and_persists_memberships(self):
        self.client._api = Mock(return_value={"query": {"pages": [api_page()]}})
        self.client._request_once = Mock(return_value=(image_bytes(), "image/jpeg"))
        landscape = self.client.fetch("landscape")[0]
        self.client._request_once = Mock(side_effect=AssertionError("cached source must not redownload"))
        city = self.client.fetch_many(["city"], 1)[0]
        self.assertEqual(city["id"], landscape["id"])
        self.assertEqual(set(city["themes"]), {"landscape", "city"})
        self.assertEqual(self.client.last_stats["downloaded"], 0)
        self.assertEqual(self.client.last_stats["cached"], 1)
        self.client._request_once.assert_not_called()
        restored = commons.CommonsClient(self.root)
        self.assertEqual(set(restored._cached("city", 1)[0]["themes"]), {"landscape", "city"})
        self.assertEqual(len(list(restored.images.glob("*.jpg"))), 1)

    def test_sha1_alias_reuses_file_without_requesting_new_url(self):
        first = api_page(sha1="a" * 40)
        second = api_page(sha1="a" * 40, descriptionurl="https://commons.wikimedia.org/wiki/File:Alias.jpg",
                          thumburl="https://thumb.wikimedia.org/alias.jpg")
        self.client._api = Mock(return_value={"query": {"pages": [first]}})
        self.client._request_once = Mock(return_value=(image_bytes(), "image/jpeg"))
        self.client.fetch("landscape")
        self.client._api = Mock(return_value={"query": {"pages": [second]}})
        self.client._request_once = Mock(side_effect=AssertionError("SHA1 alias must be reused"))
        result = self.client.fetch_many(["technology"], 1)
        self.assertEqual(len(result), 1)
        self.assertEqual(set(result[0]["themes"]), {"landscape", "technology"})
        self.client._request_once.assert_not_called()

    def test_completed_batch_cursor_persists_but_unused_batch_does_not_skip(self):
        self.install_many()
        self.client.fetch_many(["landscape"], 12)
        state = self.client._state["themes"]["landscape"]
        self.assertTrue(state["cursors"])
        self.assertIn("file|next|12", state["cursors"].values())
        second = commons.CommonsClient(self.root / "partial", rng=random.Random(19))
        self.install_many(second)
        second.fetch_many(["landscape"], 1)
        self.assertEqual(second._state["themes"]["landscape"]["cursors"], {})

    def test_outputs_do_not_mutate_cache_tags_and_failures_keep_other_themes(self):
        self.install_many()
        fake_api = self.client._api

        def mixed_api(params):
            if "landscapes" in params["gcmtitle"]:
                raise commons.CommonsError("offline landscape")
            return fake_api(params)

        self.client._api = Mock(side_effect=mixed_api)
        result = self.client.fetch_many(["landscape", "city"], 4)
        self.assertEqual(len(result), 4)
        self.assertEqual(self.client.last_stats["themes"]["landscape"]["failed"], 1)
        self.assertEqual(self.client.last_stats["themes"]["city"]["downloaded"], 4)
        result[0]["tags"].append("private favorite tag")
        self.assertEqual(self.client._state["records"][0]["tags"], [])
        self.assertIn("部分请求失败", self.client.last_status)

    def test_oversized_thumbnail_is_filtered_before_download(self):
        self.assertIsNone(self.client._candidate(api_page(thumbwidth=3840), "city"))
        with self.assertRaises(commons.CommonsError):
            commons.validate_image(image_bytes(size=(2000, 1000)), "image/jpeg")


if __name__ == "__main__":
    unittest.main()
