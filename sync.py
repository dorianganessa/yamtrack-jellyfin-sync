"""Read-only Yamtrack reader and additive Jellyfin sync. Python standard library only."""
import argparse
import collections
import contextlib
import datetime as dt
import fcntl
import json
import os
from pathlib import Path
import signal
import sqlite3
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

UTC = dt.timezone.utc
TRAKT_ID = "4fe3201ed6ae4f2e8917e12bda571281"
WEBHOOK_ID = "71552a5a5c5c4350a2aeebe451a30173"
STOP = threading.Event()


def watched_date(value):
    if not value:
        return None
    date = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    # Django's SQLite backend stores USE_TZ timestamps as naive UTC.
    if date.tzinfo is None:
        date = date.replace(tzinfo=UTC)
    if date > dt.datetime.now(UTC) + dt.timedelta(minutes=5):
        raise ValueError("future watched date")
    return date.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def source_records(path, user_id):
    uri = Path(path).resolve().as_uri() + "?mode=ro"
    movies, episodes = {}, {}
    counts = collections.Counter()
    with contextlib.closing(sqlite3.connect(uri, uri=True, timeout=10)) as db:
        db.execute("PRAGMA query_only=ON")
        db.execute("BEGIN")
        required = {
            "users_user": {"id", "username", "jellyfin_mark_played_enabled", "jellyfin_mark_unplayed_enabled"},
            "app_item": {"id", "source", "media_id", "title", "season_number", "episode_number"},
            "app_movie": {"item_id", "user_id", "status", "end_date"},
            "app_season": {"id", "user_id"},
            "app_episode": {"item_id", "related_season_id", "end_date"},
        }
        for table, columns in required.items():
            present = {row[1] for row in db.execute("PRAGMA table_info(" + table + ")")}
            if not columns <= present:
                raise ValueError("incompatible Yamtrack schema: " + table)
        user = db.execute("SELECT username,jellyfin_mark_played_enabled,jellyfin_mark_unplayed_enabled FROM users_user WHERE id=?", (user_id,)).fetchone()
        if not user:
            raise ValueError("Yamtrack user missing")
        counts["manual_webhook_feedback_enabled"] = bool(user[1] or user[2])
        queries = [
            (movies, "movie_records", """SELECT i.media_id,i.title,m.end_date FROM app_movie m
             JOIN app_item i ON i.id=m.item_id
             WHERE m.user_id=? AND m.status='Completed' AND i.source='tmdb'"""),
            (episodes, "episode_records", """SELECT i.media_id,i.title,e.end_date,i.season_number,i.episode_number FROM app_episode e
             JOIN app_item i ON i.id=e.item_id JOIN app_season s ON s.id=e.related_season_id
             WHERE s.user_id=? AND i.source='tmdb'"""),
        ]
        for target, label, query in queries:
            for row in db.execute(query, (user_id,)):
                counts[label] += 1
                if not str(row[0]).isdigit() or (len(row) > 3 and (row[3] is None or row[4] is None or row[3] < 0 or row[4] < 0)):
                    counts["invalid_identifiers"] += 1
                    continue
                key = str(int(row[0])) if len(row) == 3 else (str(int(row[0])), row[3], row[4])
                date = watched_date(row[2])
                record = {"title": row[1], "date": date}
                previous = target.get(key)
                if previous is None or (date or "") > (previous["date"] or ""):
                    target[key] = record
        counts["distinct_movies"] = len(movies)
        counts["distinct_episodes"] = len(episodes)
    return movies, episodes, dict(counts)


class APIError(RuntimeError):
    pass


class Jellyfin:
    def __init__(self, url, user_id, key, timeout=20, retries=2):
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme not in ("http", "https") or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("invalid Jellyfin URL")
        self.url = url.rstrip("/")
        self.user_id = uuid.UUID(user_id).hex
        self.key, self.timeout, self.retries = key.strip(), timeout, retries
        if not self.key:
            raise ValueError("empty API key")
        # Refuse redirects so credentials cannot be forwarded to another origin.
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *args, **kwargs):
                return None
        self.opener = urllib.request.build_opener(NoRedirect())

    def _request(self, method, path, query=None):
        url = self.url + path
        if query:
            url += "?" + urllib.parse.urlencode(query)
        req = urllib.request.Request(url, headers={"Authorization": 'MediaBrowser Token="' + self.key + '"'}, method=method,
                                     data=b"" if method == "POST" else None)
        try:
            with self.opener.open(req, timeout=self.timeout) as response:
                body = response.read()
                return json.loads(body) if body else None
        except urllib.error.HTTPError as error:
            raise APIError("HTTP " + str(error.code)) from None
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as error:
            raise APIError(type(error).__name__) from None

    def get(self, path, query=None):
        for attempt in range(self.retries + 1):
            try:
                return self._request("GET", path, query)
            except APIError as error:
                if str(error).startswith(("HTTP 400", "HTTP 401", "HTTP 403", "HTTP 404")) or attempt == self.retries:
                    raise
                if STOP.wait(min(2 ** attempt, 5)):
                    raise APIError("stopping")

    def library(self):
        user = self.get("/Users/" + self.user_id)
        if user.get("Policy", {}).get("IsDisabled"):
            raise ValueError("Jellyfin user disabled")
        items, seen = [], set()
        offset = 0
        while True:
            page = self.get("/Items", {"userId": self.user_id, "recursive": "true",
                "includeItemTypes": "Movie,Series,Episode", "fields": "ProviderIds",
                "enableUserData": "true", "startIndex": offset, "limit": 500})
            batch = page["Items"]
            if not batch and offset < page["TotalRecordCount"]:
                raise ValueError("incomplete library pagination")
            for item in batch:
                if item["Id"] in seen:
                    raise ValueError("library changed during pagination; retry next run")
                seen.add(item["Id"])
                items.append(item)
            offset += len(batch)
            if offset >= page["TotalRecordCount"]:
                return items

    def item(self, item_id):
        return self.get("/Users/" + self.user_id + "/Items/" + uuid.UUID(item_id).hex)

    def trakt_safe(self):
        plugins = self.get("/Plugins")
        if not any(uuid.UUID(p["Id"]).hex == TRAKT_ID and p.get("Status") != "Disabled" for p in plugins):
            return True
        config = self.get("/Plugins/" + TRAKT_ID + "/Configuration")
        for user in config.get("TraktUsers", []):
            if uuid.UUID(user["LinkedMbUserId"]).hex == self.user_id and not user.get("SkipUnwatchedImportFromTrakt", False):
                return False
        return True

    def feedback_safe(self, manual_events_enabled):
        if not manual_events_enabled:
            return True
        plugins = self.get("/Plugins")
        for plugin in plugins:
            if "webhook" not in plugin.get("Name", "").lower() or plugin.get("Status") == "Disabled":
                continue
            if uuid.UUID(plugin["Id"]).hex != WEBHOOK_ID:
                return False
            config = self.get("/Plugins/" + WEBHOOK_ID + "/Configuration")
            # Allow the verified official playback-only integration. Refuse unknown
            # event/plugin configurations rather than changing Yamtrack preferences.
            for name, destinations in config.items():
                if name.endswith("Options") and isinstance(destinations, list):
                    for destination in destinations:
                        types = destination.get("NotificationTypes")
                        if not isinstance(types, list) or any(t not in ("PlaybackStart", "PlaybackStop") for t in types):
                            return False
        return True

    def mark_played(self, item_id, date):
        """The only write operation exposed by this client. Never blindly retry a POST."""
        path = "/Users/" + self.user_id + "/PlayedItems/" + uuid.UUID(item_id).hex
        for attempt in range(self.retries + 1):
            current = self.item(item_id)
            played = current.get("UserData", {}).get("Played")
            if not isinstance(played, bool):
                raise APIError("missing played state during pre-write check")
            if played:
                return "already_watched"
            try:
                self._request("POST", path, {"datePlayed": date} if date else None)
            except APIError as error:
                # Even a failed/timeout response can have committed the change.
                if self.item(item_id).get("UserData", {}).get("Played"):
                    return "changed"
                if str(error).startswith(("HTTP 400", "HTTP 401", "HTTP 403", "HTTP 404")) or attempt == self.retries:
                    raise
                STOP.wait(min(2 ** attempt, 5))
                continue
            if not self.item(item_id).get("UserData", {}).get("Played"):
                raise APIError("played state not confirmed")
            return "changed"


def tmdb(item):
    value = item.get("ProviderIds", {}).get("Tmdb")
    return str(int(value)) if value and str(value).isdigit() else None


def match(movies, episodes, items):
    series = {i["Id"]: i for i in items if i["Type"] == "Series"}
    movie_lookup, episode_lookup = collections.defaultdict(list), collections.defaultdict(list)
    ranges, report, candidates = {}, [], []
    for item in items:
        if item["Type"] == "Movie" and tmdb(item):
            movie_lookup[tmdb(item)].append(item["Id"])
        elif item["Type"] == "Episode":
            show = tmdb(series.get(item.get("SeriesId"), {}))
            season, start = item.get("ParentIndexNumber"), item.get("IndexNumber")
            end = item.get("IndexNumberEnd") if item.get("IndexNumberEnd") is not None else start
            if show and all(isinstance(n, int) and n >= 0 for n in (season, start, end)) and start <= end <= start + 100:
                keys = {(show, season, n) for n in range(start, end + 1)}
                ranges[item["Id"]] = keys
                for key in keys:
                    episode_lookup[key].append(item["Id"])
    for item in items:
        reason, date = None, None
        if item["Type"] == "Movie":
            key = tmdb(item)
            if not key:
                reason = "missing_identifiers"
            elif key not in movies:
                continue
            elif len(movie_lookup[key]) > 1:
                reason = "ambiguous"
            else:
                date = movies[key]["date"]
        elif item["Type"] == "Episode":
            keys = ranges.get(item["Id"])
            if not keys:
                reason = "missing_identifiers"
            elif not keys.intersection(episodes):
                continue
            elif not keys.issubset(episodes):
                reason = "partly_watched_combined_file"
            elif any(len(episode_lookup[key]) > 1 for key in keys):
                reason = "ambiguous"
            else:
                date = max((episodes[key]["date"] or "" for key in keys), default="") or None
        else:
            continue
        if reason is None:
            if not isinstance(item.get("UserData", {}).get("Played"), bool):
                raise ValueError("missing Jellyfin played state")
            reason = "already_watched" if item["UserData"]["Played"] else "candidate"
        entry = {"id": item["Id"], "type": item["Type"], "title": item.get("Name", ""), "reason": reason, "date": date}
        if item["Type"] == "Episode":
            entry.update(series=item.get("SeriesName"), season=item.get("ParentIndexNumber"), episode=item.get("IndexNumber"), episode_end=item.get("IndexNumberEnd"))
        report.append(entry)
        if reason == "candidate":
            candidates.append(entry)
    for key, record in movies.items():
        if key not in movie_lookup:
            report.append({"type": "Movie", "tmdb": key, "title": record["title"], "reason": "absent_from_library"})
    for key, record in episodes.items():
        if key not in episode_lookup:
            report.append({"type": "Episode", "tmdb": key[0], "season": key[1], "episode": key[2], "title": record["title"], "reason": "absent_from_library"})
    return candidates, report


def atomic_json(path, data):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(data, indent=2) + "\n")
    temporary.replace(path)


def run(args, api):
    started = time.time()
    report = {"started_at": dt.datetime.now(UTC).isoformat(), "dry_run": args.command == "dry-run", "changes": [], "success": False}
    try:
        movies, episodes, source = source_records(args.database, args.yamtrack_user)
        report["source"] = source
        items = api.library()
        report["library"] = dict(collections.Counter(i["Type"] for i in items))
        candidates, entries = match(movies, episodes, items)
        report["entries"] = entries
        report["summary"] = dict(collections.Counter(e["type"] + ":" + e["reason"] for e in entries))
        report["trakt_safe"] = api.trakt_safe()
        report["webhook_feedback_safe"] = api.feedback_safe(source["manual_webhook_feedback_enabled"])
        if args.command != "dry-run":
            if not report["trakt_safe"]:
                raise ValueError("Trakt unwatched imports enabled; refusing writes")
            if not report["webhook_feedback_safe"]:
                raise ValueError("manual watched webhook feedback possible; refusing writes")
            for entry in candidates[:args.max_writes]:
                if STOP.is_set():
                    raise ValueError("stopping")
                result = api.mark_played(entry["id"], entry["date"])
                report["changes"].append({**entry, "result": result})
                # Keep a durable manifest even if the process is killed mid-batch.
                atomic_json(args.state / "in-progress.json", {"started_at": report["started_at"], "changes": report["changes"]})
                if args.write_delay:
                    STOP.wait(args.write_delay)
        report["success"] = True
    except Exception as error:
        # Never expose response bodies, keys, or headers in error logs.
        report["error"] = str(error) if isinstance(error, (ValueError, APIError)) else type(error).__name__
    report["duration_seconds"] = round(time.time() - started, 2)
    report["changed"] = sum(c["result"] == "changed" for c in report["changes"])
    report["finished_at"] = dt.datetime.now(UTC).isoformat()
    atomic_json(args.state / ("run-" + str(time.time_ns()) + ".json"), report)
    atomic_json(args.state / "latest.json", report)
    if report["success"]:
        atomic_json(args.state / "last-success.json", {"time": time.time(), "dry_run": report["dry_run"], "duration_seconds": report["duration_seconds"]})
    (args.state / "in-progress.json").unlink(missing_ok=True)
    for old in sorted(args.state.glob("run-*.json"))[:-args.retention]:
        old.unlink()
    print(json.dumps({k: v for k, v in report.items() if k not in ("entries", "changes")}), flush=True)
    return report["success"]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["dry-run", "sync", "watch", "health"])
    parser.add_argument("--database", default=os.getenv("YAMTRACK_DATABASE", "/source/db.sqlite3"))
    parser.add_argument("--yamtrack-user", type=int, default=int(os.getenv("YAMTRACK_USER_ID", "1")))
    parser.add_argument("--jellyfin-user", default=os.getenv("JELLYFIN_USER_ID", ""))
    parser.add_argument("--url", default=os.getenv("JELLYFIN_URL", "http://host.docker.internal:8096"))
    parser.add_argument("--key-file", default=os.getenv("JELLYFIN_API_KEY_FILE", "/run/secrets/jellyfin_api_key"))
    parser.add_argument("--state", type=Path, default=Path(os.getenv("SYNC_STATE", "/state")))
    parser.add_argument("--interval", type=int, default=int(os.getenv("SYNC_INTERVAL", "300")))
    parser.add_argument("--max-writes", type=int, default=int(os.getenv("SYNC_MAX_WRITES", "100")))
    parser.add_argument("--write-delay", type=float, default=float(os.getenv("SYNC_WRITE_DELAY", "0.1")))
    parser.add_argument("--timeout", type=float, default=float(os.getenv("SYNC_HTTP_TIMEOUT", "20")))
    parser.add_argument("--retries", type=int, default=int(os.getenv("SYNC_HTTP_RETRIES", "2")))
    parser.add_argument("--retention", type=int, default=int(os.getenv("SYNC_REPORT_RETENTION", "100")))
    args = parser.parse_args()
    if args.interval < 10 or args.max_writes < 1 or args.retention < 1 or args.write_delay < 0 or args.retries < 0 or args.timeout <= 0:
        parser.error("invalid interval, batch, delay, retention, retry, or timeout setting")
    watch_mode = os.getenv("SYNC_DRY_RUN", "true").strip().lower()
    if args.command == "watch" and watch_mode not in ("true", "false"):
        parser.error("SYNC_DRY_RUN must be true or false")
    if args.command == "health":
        try:
            last = json.loads((args.state / "last-success.json").read_text())
            return int(time.time() - last["time"] > max(args.interval * 3, last.get("duration_seconds", 0) * 2 + args.interval))
        except (OSError, ValueError, KeyError):
            return 1
    args.state.mkdir(parents=True, exist_ok=True)
    with (args.state / "run.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("another sync is running", flush=True)
            return 1
        api = Jellyfin(args.url, args.jellyfin_user, Path(args.key_file).read_text(), args.timeout, args.retries)
        for sig in (signal.SIGINT, signal.SIGTERM):
            signal.signal(sig, lambda *_: STOP.set())
        if args.command != "watch":
            return int(not run(args, api))
        args.command = "dry-run" if watch_mode == "true" else "sync"
        while not STOP.is_set():
            run(args, api)
            STOP.wait(args.interval)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
