"""Persistent allocation and measurement for the For You discovery experiment."""
import hashlib
import json
import math
import sqlite3
import time
from contextlib import contextmanager

HOUR = 3600
INITIAL_WINDOW = 24 * HOUR
MAX_AGE = 48 * HOUR
OFFER_TTL = 15 * 60
AUDIENCE_TTL = 24 * HOUR
TARGETS = (200, 200, 400)
LIMIT = 32

SCHEMA = """
PRAGMA journal_mode = WAL;
CREATE TABLE IF NOT EXISTS posts (
    post_id TEXT PRIMARY KEY, author_id TEXT NOT NULL, published_at INTEGER NOT NULL,
    stage INTEGER NOT NULL DEFAULT 0, started_at INTEGER NOT NULL,
    deadline INTEGER NOT NULL, state TEXT NOT NULL DEFAULT 'active',
    score REAL, completed_at INTEGER
);
CREATE INDEX IF NOT EXISTS post_queue ON posts (state, stage, deadline);
CREATE INDEX IF NOT EXISTS author_rate ON posts (author_id, published_at);
CREATE TABLE IF NOT EXISTS audience (
    viewer_id TEXT PRIMARY KEY, updated_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS established (
    viewer_id TEXT NOT NULL, author_id TEXT NOT NULL, PRIMARY KEY (viewer_id, author_id)
);
CREATE TABLE IF NOT EXISTS offers (
    viewer_id TEXT NOT NULL, request_id TEXT NOT NULL,
    payload TEXT NOT NULL, accepted TEXT NOT NULL,
    PRIMARY KEY (viewer_id, request_id)
);
CREATE TABLE IF NOT EXISTS exposures (
    post_id TEXT NOT NULL REFERENCES posts(post_id), viewer_id TEXT NOT NULL,
    request_id TEXT NOT NULL, stage INTEGER NOT NULL, expires_at INTEGER NOT NULL,
    delivered_at INTEGER, viewed INTEGER, dwell_ms INTEGER, engaged INTEGER,
    PRIMARY KEY (post_id, viewer_id)
);
CREATE INDEX IF NOT EXISTS stage_exposures ON exposures (post_id, stage, expires_at);
"""


class Rejected(ValueError):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def unix_time():
    return int(time.time())


def identifier(value):
    # IDs are stored as text because SQLite integers are signed.
    if type(value) is not int or not 0 < value < 2**64:
        raise Rejected("Expected a nonzero unsigned 64-bit identifier")
    return str(value)


def performance(views, impressions, elapsed_seconds):
    """Score measured unfamiliar-audience outcomes on a bounded scale."""
    rate = views / impressions
    z = 1.96
    lower = (
        rate + z*z/(2*impressions)
        - z * math.sqrt((rate*(1-rate) + z*z/(4*impressions))/impressions)
    ) / (1 + z*z/impressions)
    # Age adjustment follows Home Mixer's popular-post curve. Log compression
    # limits the influence of a burst, and the lower bound penalizes uncertainty.
    decay = math.log(2) / (8 * HOUR)
    adjusted_views = views * math.expm1(-decay * INITIAL_WINDOW) / math.expm1(
        -decay * max(HOUR / 2, elapsed_seconds)
    )
    velocity = min(1.0, math.log1p(adjusted_views) / math.log1p(impressions))
    return 0.4 * velocity + 0.6 * lower, lower


class Ledger:
    def __init__(self, path, max_initial=100, author_hourly=3, clock=unix_time):
        if max_initial < 1 or author_hourly < 1:
            raise ValueError("Admission limits must be positive")
        self.path = path
        self.max_initial = max_initial
        self.author_hourly = author_hourly
        self.clock = clock
        with self.transaction() as db:
            db.executescript(SCHEMA)

    @contextmanager
    def transaction(self, write=True):
        db = sqlite3.connect(self.path, timeout=2, isolation_level=None)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA foreign_keys = ON")
            db.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def publish(self, post_id, author_id, published_at, eligible):
        """Admit a publication from the trusted eligibility/publish stream."""
        post, author = identifier(post_id), identifier(author_id)
        now = self.clock()
        if type(published_at) is not int or type(eligible) is not bool:
            raise Rejected("Expected integer published_at and boolean eligible")
        if published_at <= 0 or published_at > now:
            raise Rejected("Invalid publication time")
        with self.transaction() as db:
            existing = db.execute("SELECT * FROM posts WHERE post_id = ?", (post,)).fetchone()
            if existing:
                if (existing["author_id"], existing["published_at"]) != (author, published_at):
                    raise Rejected("Conflicting publication metadata", 409)
                if not eligible:
                    db.execute("UPDATE posts SET state = 'withdrawn' WHERE post_id = ?", (post,))
                    return {"state": "withdrawn"}
                return {"state": self.state(existing, now)}
            if not eligible:
                # A withdrawal can arrive ahead of a retried publish event.
                state = "withdrawn"
            elif published_at + INITIAL_WINDOW <= now:
                raise Rejected("Initial delivery window has elapsed", 410)
            else:
                admitted = db.execute(
                    "SELECT count(*) FROM posts WHERE stage = 0 AND state = 'active' AND deadline > ?",
                    (now,),
                ).fetchone()[0]
                recent = db.execute(
                    """SELECT count(*) FROM posts WHERE author_id = ?
                       AND published_at > ? AND state != 'withdrawn'""",
                    (author, now - HOUR),
                ).fetchone()[0]
                if admitted >= self.max_initial or recent >= self.author_hourly:
                    raise Rejected("Admission capacity exhausted; retry with the original timestamp", 429)
                state = "active"
            db.execute(
                """INSERT INTO posts (post_id, author_id, published_at, started_at, deadline, state)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (post, author, published_at, published_at, published_at + INITIAL_WINDOW, state),
            )
            return {"state": state}

    def replace_audience(self, viewer_id, updated_at, established_author_ids):
        viewer = identifier(viewer_id)
        now = self.clock()
        if type(updated_at) is not int or not now - AUDIENCE_TTL < updated_at <= now:
            raise Rejected("Audience snapshot must be current")
        if not isinstance(established_author_ids, list):
            raise Rejected("Expected a list of established author IDs")
        authors = {identifier(author) for author in established_author_ids}
        with self.transaction() as db:
            previous = db.execute("SELECT updated_at FROM audience WHERE viewer_id = ?", (viewer,)).fetchone()
            if previous and previous[0] >= updated_at:
                return {"updated_at": previous[0]}
            db.execute(
                """INSERT INTO audience VALUES (?, ?) ON CONFLICT(viewer_id)
                   DO UPDATE SET updated_at = excluded.updated_at""",
                (viewer, updated_at),
            )
            db.execute("DELETE FROM established WHERE viewer_id = ?", (viewer,))
            db.executemany("INSERT INTO established VALUES (?, ?)", ((viewer, a) for a in authors))
            return {"updated_at": updated_at}

    @staticmethod
    def audience_known(db, viewer, now):
        return db.execute(
            "SELECT 1 FROM audience WHERE viewer_id = ? AND updated_at > ?",
            (viewer, now - AUDIENCE_TTL),
        ).fetchone() is not None

    @staticmethod
    def state(post, now):
        if post["state"] == "active" and post["deadline"] <= now:
            return "missed" if post["stage"] == 0 else "expansion_expired"
        return post["state"]

    def candidates(self, viewer_id):
        viewer = identifier(viewer_id)
        now = self.clock()
        with self.transaction(write=False) as db:
            if not self.audience_known(db, viewer, now):
                return []
            posts = db.execute(
                """SELECT p.* FROM posts p
                   WHERE state = 'active' AND deadline > ? AND author_id != ?
                   AND NOT EXISTS (SELECT 1 FROM established a
                       WHERE a.viewer_id = ? AND a.author_id = p.author_id)
                   AND NOT EXISTS (SELECT 1 FROM exposures e WHERE e.post_id = p.post_id
                       AND e.viewer_id = ? AND (e.delivered_at IS NOT NULL OR e.expires_at > ?))
                   AND (SELECT count(*) FROM exposures e WHERE e.post_id = p.post_id
                       AND e.stage = p.stage AND (e.delivered_at IS NOT NULL OR e.expires_at > ?))
                       < CASE p.stage WHEN 0 THEN ? WHEN 1 THEN ? ELSE ? END""",
                (now, viewer, viewer, viewer, now, now, *TARGETS),
            ).fetchall()

        def priority(post):
            # Initial samples lead. Within each minute of admission deadlines,
            # randomize by viewer so cohorts don't inherit one universal order.
            shuffle = hashlib.blake2b(
                f"{viewer}:{post['post_id']}".encode(), digest_size=8
            ).digest()
            if post["stage"] == 0:
                return (0, post["deadline"] // 60, shuffle)
            return (1, -post["score"], shuffle)

        return [
            {"post_id": int(post["post_id"]), "author_id": int(post["author_id"]), "stage": post["stage"]}
            for post in sorted(posts, key=priority)[:LIMIT]
        ]

    def offer(self, viewer_id, request_id, slots, posts):
        """Reserve short leases for reserved posts that survived feed assembly."""
        viewer, request = identifier(viewer_id), identifier(request_id)
        if type(slots) is not int or not 0 <= slots <= 100 or not isinstance(posts, list):
            raise Rejected("Expected organic slot count and a list of posts")
        if len(posts) > slots // 10:
            raise Rejected("Discovery offer exceeds the slot budget")
        planned = [(identifier(item["post_id"]), item["stage"]) for item in posts]
        if any(type(stage) is not int or not 0 <= stage < len(TARGETS) for _, stage in planned):
            raise Rejected("Invalid discovery stage")
        if len({post for post, _ in planned}) != len(planned):
            raise Rejected("Duplicate post in offer")
        now = self.clock()
        accepted = []
        with self.transaction() as db:
            payload = json.dumps([slots, planned])
            retry = db.execute(
                "SELECT payload, accepted FROM offers WHERE viewer_id = ? AND request_id = ?",
                (viewer, request),
            ).fetchone()
            if retry:
                if retry["payload"] != payload:
                    raise Rejected("Conflicting discovery offer retry", 409)
                return json.loads(retry["accepted"])
            available = planned if self.audience_known(db, viewer, now) else []
            for post, stage in available:
                row = db.execute("SELECT * FROM posts WHERE post_id = ?", (post,)).fetchone()
                previous = db.execute(
                    "SELECT * FROM exposures WHERE post_id = ? AND viewer_id = ?", (post, viewer)
                ).fetchone()
                if previous and (previous["delivered_at"] is not None or previous["expires_at"] > now):
                    continue
                if not row or self.state(row, now) != "active" or row["stage"] != stage:
                    continue
                if row["author_id"] == viewer or db.execute(
                    "SELECT 1 FROM established WHERE viewer_id = ? AND author_id = ?",
                    (viewer, row["author_id"]),
                ).fetchone():
                    continue
                in_flight = db.execute(
                    """SELECT count(*) FROM exposures WHERE post_id = ? AND stage = ?
                       AND (delivered_at IS NOT NULL OR expires_at > ?)""",
                    (post, stage, now),
                ).fetchone()[0]
                if in_flight >= TARGETS[stage]:
                    continue
                db.execute(
                    """INSERT INTO exposures (post_id, viewer_id, request_id, stage, expires_at)
                       VALUES (?, ?, ?, ?, ?) ON CONFLICT(post_id, viewer_id) DO UPDATE SET
                       request_id = excluded.request_id, stage = excluded.stage, expires_at = excluded.expires_at""",
                    (post, viewer, request, stage, min(now + OFFER_TTL, row["deadline"])),
                )
                accepted.append(int(post))
            db.execute(
                "INSERT INTO offers VALUES (?, ?, ?, ?)",
                (viewer, request, payload, json.dumps(accepted)),
            )
        return accepted

    def confirm(self, viewer_id, request_id, post_id, viewed, dwell_ms, engaged):
        """Count a verified viewport impression with its consolidated outcomes."""
        viewer, request, post = map(identifier, (viewer_id, request_id, post_id))
        if type(viewed) is not bool or type(engaged) is not bool or type(dwell_ms) is not int:
            raise Rejected("Expected boolean viewed/engaged and integer dwell_ms")
        if not 0 <= dwell_ms <= OFFER_TTL * 1000 or (not viewed and (dwell_ms or engaged)):
            raise Rejected("Invalid impression outcomes")
        now = self.clock()
        with self.transaction() as db:
            exposure = db.execute(
                "SELECT * FROM exposures WHERE post_id = ? AND viewer_id = ?", (post, viewer)
            ).fetchone()
            if not exposure or exposure["request_id"] != request:
                raise Rejected("No matching discovery offer", 404)
            if exposure["delivered_at"] is not None:
                return {"counted": True}
            row = db.execute("SELECT * FROM posts WHERE post_id = ?", (post,)).fetchone()
            if now >= exposure["expires_at"] or self.state(row, now) != "active":
                raise Rejected("Discovery offer is no longer active", 410)
            stage = exposure["stage"]
            if stage != row["stage"]:
                raise Rejected("Discovery offer belongs to a closed stage", 410)
            db.execute(
                """UPDATE exposures SET delivered_at = ?, viewed = ?, dwell_ms = ?, engaged = ?
                   WHERE post_id = ? AND viewer_id = ?""",
                (now, viewed, dwell_ms, engaged, post, viewer),
            )
            total, views = db.execute(
                """SELECT count(*), sum(viewed) FROM exposures
                   WHERE post_id = ? AND stage = ? AND delivered_at IS NOT NULL""",
                (post, stage),
            ).fetchone()
            if total == TARGETS[stage]:
                score, lower = performance(views, total, now - row["started_at"])
                # Expansion requires evidence in each successive unfamiliar cohort.
                can_expand = stage + 1 < len(TARGETS) and lower >= 0.20 and now < row["published_at"] + MAX_AGE
                if can_expand:
                    db.execute(
                        """UPDATE posts SET stage = stage + 1, started_at = ?,
                           deadline = ?, score = ?, completed_at = coalesce(completed_at, ?) WHERE post_id = ?""",
                        (now, row["published_at"] + MAX_AGE, score, now, post),
                    )
                else:
                    db.execute(
                        """UPDATE posts SET state = 'complete', score = ?,
                           completed_at = coalesce(completed_at, ?) WHERE post_id = ?""",
                        (score, now, post),
                    )
            return {"counted": True}

    def report(self):
        now = self.clock()
        with self.transaction(write=False) as db:
            return [
                {
                    "post_id": int(row["post_id"]),
                    "author_id": int(row["author_id"]),
                    "state": self.state(row, now),
                    "initial_fulfilled": row["completed_at"] is not None,
                    "initial_delivered": row["initial_delivered"],
                    "delivered": row["delivered"],
                    "unfamiliar": row["delivered"],
                    "views": row["views"],
                    "dwell_ms": row["dwell_ms"],
                    "engagements": row["engagements"],
                    "score": row["score"],
                }
                for row in db.execute(
                    """SELECT p.*, count(e.delivered_at) AS delivered,
                       count(CASE WHEN e.stage = 0 THEN e.delivered_at END) AS initial_delivered,
                       coalesce(sum(e.viewed), 0) AS views, coalesce(sum(e.dwell_ms), 0) AS dwell_ms,
                       coalesce(sum(e.engaged), 0) AS engagements
                       FROM posts p LEFT JOIN exposures e USING (post_id)
                       GROUP BY p.post_id ORDER BY p.published_at, p.post_id"""
                )
            ]
