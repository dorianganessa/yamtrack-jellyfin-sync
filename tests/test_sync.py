import collections
import contextlib
import datetime as dt
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import urllib.parse

import sync

USER = "00000000000000000000000000000001"
MOVIE = "00000000000000000000000000000002"
EPISODE = "00000000000000000000000000000003"
SERIES = "00000000000000000000000000000004"


def library_movie(item_id=MOVIE, played=False):
    return {"Id": item_id, "Type": "Movie", "Name": "Film", "ProviderIds": {"Tmdb": "20"}, "UserData": {"Played": played}}


def library_episode(start=1, end=None, item_id=EPISODE, season=1):
    return {"Id": item_id, "Type": "Episode", "Name": "Episode", "SeriesId": SERIES,
            "ParentIndexNumber": season, "IndexNumber": start, "IndexNumberEnd": end, "UserData": {"Played": False}}


SHOW = {"Id": SERIES, "Type": "Series", "ProviderIds": {"Tmdb": "30"}}
DATE = "2025-01-01T00:00:00.000000Z"
RECORD = {"title": "Example", "date": DATE}


class MatchingTests(unittest.TestCase):
    def test_typo_in_dry_run_setting_cannot_start_writes(self):
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run([sys.executable, str(Path(sync.__file__)), "watch", "--state", directory],
                                    env={**os.environ, "SYNC_DRY_RUN": "tru"}, capture_output=True, text=True)
            self.assertEqual(result.returncode, 2)
            self.assertIn("SYNC_DRY_RUN must be true or false", result.stderr)
            self.assertEqual(list(Path(directory).iterdir()), [])
    def test_movie_dedup_and_existing_history(self):
        candidates, report = sync.match({"20": RECORD}, {}, [library_movie(played=True)])
        self.assertEqual(candidates, [])
        self.assertEqual(report[0]["reason"], "already_watched")

    def test_episode_uses_parent_series_not_episode_tmdb(self):
        ep = library_episode()
        ep["ProviderIds"] = {"Tmdb": "99999"}
        candidates, _ = sync.match({}, {("30", 1, 1): RECORD}, [SHOW, ep])
        self.assertEqual([c["id"] for c in candidates], [EPISODE])

    def test_combined_requires_all_episodes_and_latest_date(self):
        ep = library_episode(end=2)
        candidates, report = sync.match({}, {("30", 1, 1): RECORD}, [SHOW, ep])
        self.assertFalse(candidates)
        self.assertEqual(report[0]["reason"], "partly_watched_combined_file")
        newer = {"title": "Show", "date": "2025-02-01T00:00:00Z"}
        candidates, _ = sync.match({}, {("30", 1, 1): RECORD, ("30", 1, 2): newer}, [SHOW, ep])
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]["date"], newer["date"])

    def test_overlapping_episode_files_and_movie_editions_are_skipped(self):
        ep2 = library_episode(item_id=MOVIE)
        candidates, report = sync.match({}, {("30", 1, 1): RECORD}, [SHOW, library_episode(), ep2])
        self.assertFalse(candidates)
        self.assertEqual([r["reason"] for r in report], ["ambiguous", "ambiguous"])
        candidates, _ = sync.match({"20": RECORD}, {}, [library_movie(), library_movie(item_id=EPISODE)])
        self.assertFalse(candidates)

    def test_specials_missing_ids_and_new_media(self):
        source = {("30", 0, 1): RECORD}
        candidates, report = sync.match({}, source, [SHOW])
        self.assertFalse(candidates)
        self.assertEqual(report[0]["reason"], "absent_from_library")
        candidates, _ = sync.match({}, source, [SHOW, library_episode(season=0)])
        self.assertEqual(len(candidates), 1)
        candidates, report = sync.match({}, source, [library_episode(season=0)])
        self.assertFalse(candidates)
        self.assertIn("missing_identifiers", [r["reason"] for r in report])

    def test_unknown_played_flag_fails_closed(self):
        item = library_movie()
        del item["UserData"]
        with self.assertRaises(ValueError):
            sync.match({"20": RECORD}, {}, [item])


class DatabaseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "db.sqlite3"
        self.db = sqlite3.connect(self.path)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
        CREATE TABLE users_user(id INTEGER,username TEXT,jellyfin_mark_played_enabled INTEGER,jellyfin_mark_unplayed_enabled INTEGER);
        CREATE TABLE app_item(id INTEGER,source TEXT,media_id TEXT,title TEXT,season_number INTEGER,episode_number INTEGER);
        CREATE TABLE app_movie(item_id INTEGER,user_id INTEGER,status TEXT,end_date TEXT);
        CREATE TABLE app_season(id INTEGER,user_id INTEGER);
        CREATE TABLE app_episode(item_id INTEGER,related_season_id INTEGER,end_date TEXT);
        INSERT INTO users_user VALUES(1,'target',0,0),(2,'other',0,0);
        INSERT INTO app_item VALUES(1,'tmdb','20','Film',NULL,NULL),(2,'tmdb','30','Show',1,1),(3,'manual','uuid','Manual',NULL,NULL);
        INSERT INTO app_movie VALUES(1,1,'Completed','2024-01-01'),(1,1,'Completed','2025-01-01'),(1,2,'Completed','2025-03-01'),(3,1,'Completed',NULL);
        INSERT INTO app_season VALUES(1,1),(2,2);
        INSERT INTO app_episode VALUES(2,1,'2025-01-01'),(2,2,'2025-04-01');
        """)
        self.db.commit()

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def test_wal_user_isolation_dedup_and_no_mutation(self):
        before = list(self.db.iterdump())
        movies, episodes, counts = sync.source_records(self.path, 1)
        self.assertEqual(movies, {"20": {"title": "Film", "date": DATE}})
        self.assertEqual(episodes, {("30", 1, 1): {"title": "Show", "date": DATE}})
        self.assertEqual(counts["movie_records"], 2)
        self.assertEqual(list(self.db.iterdump()), before)

    def test_missing_db_not_created_and_missing_user_rejected(self):
        absent = self.path.with_name("missing.db")
        with self.assertRaises(sqlite3.OperationalError): sync.source_records(absent, 1)
        self.assertFalse(absent.exists())
        with self.assertRaises(ValueError): sync.source_records(self.path, 99)

    def test_invalid_date_stops_source_read(self):
        self.db.execute("UPDATE app_movie SET end_date='invalid' WHERE user_id=1")
        self.db.commit()
        with self.assertRaises(ValueError): sync.source_records(self.path, 1)

    def test_subsecond_rewatch_dates_sort_correctly(self):
        self.db.execute("UPDATE app_movie SET end_date='2025-01-01T00:00:00.5' WHERE user_id=1")
        self.db.execute("INSERT INTO app_movie VALUES(1,1,'Completed','2025-01-01T00:00:00')")
        self.db.commit()
        movies, _, _ = sync.source_records(self.path, 1)
        self.assertEqual(movies['20']['date'], '2025-01-01T00:00:00.500000Z')


class FakeServer(BaseHTTPRequestHandler):
    items = []
    posts = []
    lose_response = False
    unsafe_trakt = False
    webhook = None
    webhook_types = ["PlaybackStart", "PlaybackStop"]

    def log_message(self, *args):
        pass

    def respond(self, value, status=200):
        self.send_response(status)
        self.end_headers()
        self.wfile.write(json.dumps(value).encode())

    def do_GET(self):
        path = urllib.parse.urlsplit(self.path)
        if path.path == "/Users/" + USER:
            self.respond({"Id": USER})
        elif path.path == "/Items":
            start = int(urllib.parse.parse_qs(path.query)["startIndex"][0])
            self.respond({"Items": self.items[start:start + 1], "TotalRecordCount": len(self.items)})
        elif path.path.endswith("/Items/" + MOVIE):
            self.respond(self.items[0])
        elif path.path == "/Plugins":
            plugins = [{"Id": sync.TRAKT_ID}] if self.unsafe_trakt else []
            if self.webhook: plugins.append({"Id": self.webhook, "Name": "Webhook"})
            self.respond(plugins)
        elif path.path == "/Plugins/" + sync.WEBHOOK_ID + "/Configuration":
            self.respond({"GenericOptions": [{"NotificationTypes": self.webhook_types}]})
        elif path.path.endswith("/Configuration"):
            self.respond({"TraktUsers": [{"LinkedMbUserId": USER, "SkipUnwatchedImportFromTrakt": False}]})
        else:
            self.respond({}, 404)

    def do_POST(self):
        if urllib.parse.urlsplit(self.path).path != "/Users/" + USER + "/PlayedItems/" + MOVIE:
            self.respond({}, 403)
            return
        self.posts.append(self.path)
        self.items[0]["UserData"]["Played"] = True
        if self.lose_response:
            self.connection.close()
            return
        self.respond(self.items[0]["UserData"])

    def do_DELETE(self):
        raise AssertionError("unwatch not allowed")


class APITests(unittest.TestCase):
    def setUp(self):
        FakeServer.items = [library_movie(), SHOW]
        FakeServer.posts = []
        FakeServer.lose_response = False
        FakeServer.unsafe_trakt = False
        FakeServer.webhook = None
        FakeServer.webhook_types = ["PlaybackStart", "PlaybackStop"]
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), FakeServer)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.api = sync.Jellyfin("http://127.0.0.1:" + str(self.server.server_port), USER, "test", retries=0)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def test_pagination_and_idempotent_writes(self):
        self.assertEqual(len(self.api.library()), 2)
        self.assertEqual(self.api.mark_played(MOVIE, DATE), "changed")
        self.assertEqual(self.api.mark_played(MOVIE, DATE), "already_watched")
        self.assertEqual(len(FakeServer.posts), 1)
        self.assertIn("datePlayed=", FakeServer.posts[0])

    def test_unknown_post_outcome_is_read_before_retry(self):
        FakeServer.lose_response = True
        self.assertEqual(self.api.mark_played(MOVIE, DATE), "changed")
        self.assertEqual(len(FakeServer.posts), 1)

    def test_trakt_guard_and_dry_run_have_no_writes(self):
        self.assertTrue(self.api.trakt_safe())
        FakeServer.unsafe_trakt = True
        self.assertFalse(self.api.trakt_safe())
        sync.match({"20": RECORD}, {}, self.api.library())
        self.assertEqual(FakeServer.posts, [])

    def test_full_run_dry_run_guard_write_and_repeat(self):
        with tempfile.TemporaryDirectory() as directory:
            args = SimpleNamespace(database="unused", yamtrack_user=1, state=Path(directory),
                                   command="dry-run", max_writes=1, write_delay=0, retention=2)
            source = ({"20": RECORD}, {}, {"manual_webhook_feedback_enabled": False})
            with patch("sync.source_records", return_value=source), contextlib.redirect_stdout(None):
                self.assertTrue(sync.run(args, self.api))
                self.assertEqual(FakeServer.posts, [])
                args.command = "sync"
                FakeServer.unsafe_trakt = True
                self.assertFalse(sync.run(args, self.api))
                self.assertEqual(FakeServer.posts, [])
                FakeServer.unsafe_trakt = False
                self.assertTrue(sync.run(args, self.api))
                self.assertTrue(sync.run(args, self.api))
            self.assertEqual(len(FakeServer.posts), 1)
            self.assertEqual(json.loads((args.state / "latest.json").read_text())["changed"], 0)
            self.assertEqual(len(list(args.state.glob("run-*.json"))), 2)

    def test_missing_played_state_cannot_write(self):
        del FakeServer.items[0]["UserData"]
        with self.assertRaises(sync.APIError):
            self.api.mark_played(MOVIE, DATE)
        self.assertEqual(FakeServer.posts, [])

    def test_feedback_guard_preserves_playback_only_preferences(self):
        FakeServer.webhook = sync.WEBHOOK_ID
        self.assertTrue(self.api.feedback_safe(True))
        FakeServer.webhook_types = ["UserDataSaved"]
        self.assertFalse(self.api.feedback_safe(True))
        self.assertTrue(self.api.feedback_safe(False))
        FakeServer.webhook = MOVIE
        self.assertFalse(self.api.feedback_safe(True))


if __name__ == "__main__":
    unittest.main()
