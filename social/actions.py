"""Small persistent at-most-once store. No credentials or message text."""
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import sqlite3
import time
import uuid


class SocialError(Exception):
    pass


class ActionStore:
    def __init__(self, path):
        self.path = Path(path)

    @contextmanager
    def db(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path, timeout=2)
        conn.row_factory = sqlite3.Row
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS actions(
              action_id TEXT PRIMARY KEY, owner_uid TEXT, action_type TEXT,
              target TEXT, digest TEXT, status TEXT, created_at REAL,
              updated_at REAL, metadata TEXT, error_code TEXT);
            CREATE TABLE IF NOT EXISTS refs(
              bvid TEXT,rpid TEXT,root TEXT,parent TEXT,uid TEXT,
              PRIMARY KEY(bvid,rpid));
            CREATE TABLE IF NOT EXISTS events(
              event_id TEXT PRIMARY KEY,source TEXT,occurred_at TEXT,
              observed_at REAL,reference_json TEXT,processed_at REAL,
              recent_event_id TEXT);
            CREATE TABLE IF NOT EXISTS event_snapshots(
              token TEXT PRIMARY KEY,created_at REAL,ids_json TEXT);
        """)
        conn.commit()
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def prepare(self, action_id, owner_uid, action_type, target, contents, metadata, hourly_limit=3):
        digest = hashlib.sha256(json.dumps([owner_uid, action_type, target, contents], ensure_ascii=False,
            sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
        now = time.time()
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            prior = db.execute("SELECT * FROM actions WHERE action_id=?", (action_id,)).fetchone()
            if prior:
                if prior["digest"] != digest:
                    raise SocialError("ACTION_ID_CONFLICT")
                return False, dict(prior)
            if action_type in ("like", "favorite", "follow"):
                # A settled inverse is a new deliberate transition. Only the
                # most recent state blocks an identical new request; an older
                # unresolved send to this target always blocks any transition.
                duplicate = db.execute("SELECT * FROM actions WHERE owner_uid=? AND action_type=? AND target=? AND status IN ('prepared','unknown') ORDER BY created_at DESC,rowid DESC LIMIT 1",
                    (owner_uid, action_type, target)).fetchone()
                if not duplicate:
                    latest = db.execute("SELECT * FROM actions WHERE owner_uid=? AND action_type=? AND target=? ORDER BY created_at DESC,rowid DESC LIMIT 1",
                        (owner_uid, action_type, target)).fetchone()
                    duplicate = latest if latest and latest["digest"] == digest else None
            else:
                duplicate = db.execute("SELECT * FROM actions WHERE owner_uid=? AND (digest=? OR (target=? AND status IN ('prepared','unknown'))) AND (created_at>? OR status IN ('prepared','unknown')) ORDER BY created_at DESC LIMIT 1",
                    (owner_uid, digest, target, now - 86400)).fetchone()
            if duplicate:
                return False, dict(duplicate)
            hourly = db.execute("SELECT COUNT(*) FROM actions WHERE owner_uid=? AND created_at>?", (owner_uid, now - 3600)).fetchone()[0]
            if hourly >= hourly_limit:
                raise SocialError("SOCIAL_HOURLY_LIMIT_REACHED")
            db.execute("INSERT INTO actions VALUES(?,?,?,?,?,?,?,?,?,?)", (action_id, owner_uid,
                action_type, target, digest, "prepared", now, now, json.dumps(metadata), None))
        return True, None

    def finish(self, action_id, status, metadata, error_code=None):
        with self.db() as db:
            db.execute("UPDATE actions SET status=?,updated_at=?,metadata=?,error_code=? WHERE action_id=?",
                (status, time.time(), json.dumps(metadata), error_code, action_id))

    def get(self, action_id, owner_uid):
        with self.db() as db:
            row = db.execute("SELECT * FROM actions WHERE action_id=? AND owner_uid=?", (action_id, owner_uid)).fetchone()
        if not row:
            return None
        value = dict(row)
        value["metadata"] = json.loads(value["metadata"])
        value.pop("digest", None)
        return value

    def remember(self, comments):
        with self.db() as db:
            for item in comments:
                db.execute("INSERT OR REPLACE INTO refs VALUES(?,?,?,?,?)", (item["bvid"], item["rpid"],
                    item["root"], item["parent"], item["uid"]))
                for child in item.get("replies", []):
                    db.execute("INSERT OR REPLACE INTO refs VALUES(?,?,?,?,?)", (child["bvid"], child["rpid"],
                        child["root"], child["parent"], child["uid"]))

    def ref(self, bvid, rpid):
        with self.db() as db:
            row = db.execute("SELECT * FROM refs WHERE bvid=? AND rpid=?", (bvid, rpid)).fetchone()
        return dict(row) if row else None

    def events(self, incoming, limit, acknowledgements=(), recent_event_id="", cursor=""):
        now = time.time()
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            for event_id in acknowledgements:
                if not db.execute("SELECT 1 FROM events WHERE event_id=?", (event_id,)).fetchone():
                    raise SocialError("ACK_EVENT_NOT_PREVIOUSLY_OBSERVED")
            for item in incoming:
                db.execute("INSERT OR IGNORE INTO events VALUES(?,?,?,?,?,NULL,NULL)", (
                    item["event_id"], item["source"], item.get("occurred_at"), now,
                    json.dumps(item["reference"], ensure_ascii=False)))
            for event_id in acknowledgements:
                db.execute("UPDATE events SET processed_at=?,recent_event_id=CASE WHEN ?!='' THEN ? ELSE recent_event_id END WHERE event_id=?",
                    (now, recent_event_id, recent_event_id, event_id))
            if cursor:
                try:
                    token, offset = cursor.split(":")
                    offset = int(offset)
                    if offset < 0:
                        raise ValueError()
                except (ValueError, TypeError):
                    raise SocialError("INVALID_EVENT_SNAPSHOT_CURSOR") from None
                snapshot = db.execute("SELECT * FROM event_snapshots WHERE token=?", (token,)).fetchone()
                if not snapshot or now - snapshot["created_at"] > 86400:
                    raise SocialError("EVENT_SNAPSHOT_EXPIRED_OR_UNKNOWN")
                event_ids = json.loads(snapshot["ids_json"])
            else:
                token, offset = uuid.uuid4().hex, 0
                event_ids = [row[0] for row in db.execute("SELECT event_id FROM events WHERE processed_at IS NULL ORDER BY observed_at,event_id LIMIT 5000")]
                db.execute("DELETE FROM event_snapshots WHERE created_at<?", (now - 86400,))
                db.execute("INSERT INTO event_snapshots VALUES(?,?,?)", (token, now, json.dumps(event_ids)))
            rows = [db.execute("SELECT * FROM events WHERE event_id=?", (event_id,)).fetchone()
                for event_id in event_ids[offset:offset + limit]]
        output = []
        for row in rows:
            item = dict(row)
            item["reference"] = json.loads(item.pop("reference_json"))
            output.append(item)
        next_offset = offset + len(rows)
        next_cursor = token + ":" + str(next_offset) if next_offset < len(event_ids) else None
        return output, next_cursor, len(event_ids)
