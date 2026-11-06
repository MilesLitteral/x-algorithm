"""Compare synthetic personalized ranking and reserved discovery on the same viewers."""
import hashlib
import json
import tempfile
from collections import Counter
from pathlib import Path

from ledger import INITIAL_WINDOW, Ledger

POSTS = {
    1: (101, 20, 0.65),
    2: (102, 800, 0.12),
    3: (103, 2_000_000, 0.43),
    **{post: (200 + post, 25_000, 0.34) for post in range(4, 32)},
}
EXPERIMENT_POSTS = (1, 2, 3)
PAGE_SIZE = 20


def chance(*parts):
    raw = ":".join(map(str, parts)).encode()
    return int.from_bytes(hashlib.blake2b(raw, digest_size=8).digest(), "big") / 2**64


def simulate(requests, discovery):
    now = 1_800_000_000
    with tempfile.TemporaryDirectory() as tmp:
        ledger = Ledger(Path(tmp) / "experiment.sqlite", clock=lambda: now)
        if discovery:
            for post in EXPERIMENT_POSTS:
                ledger.publish(post, POSTS[post][0], now, True)
        views = 0
        impressions = 0
        unfamiliar = Counter()
        for request in range(1, requests + 1):
            # Replay identical chronological demand, relationships and outcomes.
            viewer = 10_000 + request
            established = {
                author for author, followers, _ in POSTS.values()
                if chance("follow", viewer, author) < min(0.6, followers / 500_000)
            }
            ordinary = sorted(POSTS, key=lambda post: (
                POSTS[post][0] in established,
                chance("affinity", viewer, post) + (0.1 if POSTS[post][1] > 20_000 else -0.6),
            ), reverse=True)
            offers = []
            if discovery:
                ledger.replace_audience(viewer, now, list(established))
                plans = ledger.candidates(viewer)[:PAGE_SIZE // 10]
                offers = ledger.offer(viewer, request, PAGE_SIZE, [
                    {"post_id": plan["post_id"], "stage": plan["stage"]} for plan in plans
                ])
            page = offers + [post for post in ordinary if post not in offers][:PAGE_SIZE - len(offers)]
            for post in page:
                author, _, appeal = POSTS[post]
                is_unfamiliar = author not in established
                viewed = chance("view", viewer, post) < appeal + (0 if is_unfamiliar else 0.12)
                if is_unfamiliar:
                    unfamiliar[post] += 1
                if post in offers:
                    ledger.confirm(viewer, request, post, viewed, 1200 if viewed else 0, False)
                views += viewed
                impressions += 1
            now += 40
        now = max(now, 1_800_000_000 + INITIAL_WINDOW)
        report = ledger.report()
        return {
            "requests": requests,
            "synthetic_view_rate": round(views / impressions, 4),
            "unfamiliar_feed_impressions": {post: unfamiliar[post] for post in EXPERIMENT_POSTS},
            "initial_discovery_confirmed": {row["post_id"]: row["initial_delivered"] for row in report},
            "discovery_total_confirmed": {row["post_id"]: row["delivered"] for row in report},
            "initial_deadline_misses": [row["post_id"] for row in report if row["state"] == "missed"],
        }


if __name__ == "__main__":
    print(json.dumps({
        "synthetic_control": simulate(1600, False),
        "synthetic_discovery": simulate(1600, True),
        "synthetic_discovery_low_capacity": simulate(100, True),
    }, indent=2))
