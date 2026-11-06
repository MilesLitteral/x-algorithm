# Guaranteed Discovery

Every admitted post gets an initial target of **200 distinct, confirmed viewport impressions within 24 hours of publication**. Home Mixer reserves one of every ten eligible organic recommendation slots, rounded down. The scheduler uses no follower-count input and allocates every discovery impression to a viewer outside the author's established audience, exceeding the requested 80% minimum.

This ZIP is based on fork commit `2e2295b0df598eee5aa05f9c9b458f4490c7fcbe`. The experiment is opt-in on the For You feed. Ordinary ranking continues alongside the discovery lane. A local SQLite service holds the publication queue, audience snapshots, short-lived offers, confirmed impressions, and expansion stages across restarts.

## Run the experiment locally

From the checkout root, with Python 3.10 or newer:

```sh
cd experiments/guaranteed-discovery
python3 -m unittest -v
python3 simulate.py
```

The simulation compares a synthetic audience-affinity control and the discovery treatment on the same requests and outcomes. It runs the actual allocation ledger with fixture impressions. It also runs a low-capacity case.

| Synthetic measurement | Control | Discovery |
|---|---:|---:|
| Unfamiliar feed impressions for the 20-follower fixture | 23 | 810 |
| Unfamiliar feed impressions for the 800-follower fixture | 17 | 205 |
| Unfamiliar feed impressions for the 2-million-follower fixture | 444 | 639 |
| Feed view rate | 35.53% | 36.22% |
| Posts reaching 200 unfamiliar feed impressions | 1 of 3 | 3 of 3 |

All three treatment posts received exactly 200 confirmed initial discovery impressions, including the weaker post that stopped after its initial sample. With 100 requests, all three missed the deadline. These results describe the supplied fixture. Production evaluation requires a matched control using real viewport, dwell, engagement, and eligibility events.

## Run the local service

```sh
export GUARANTEED_DISCOVERY_TOKEN='<replace-with-a-local-secret>'
python3 experiments/guaranteed-discovery/serve.py --db /path/to/discovery.sqlite --port 8765
```

In a build-capable Home Mixer environment, set `GUARANTEED_DISCOVERY_PORT=8765` and the same `GUARANTEED_DISCOVERY_TOKEN` before starting Home Mixer. The service listens on loopback. Omitting the port runs the stock feed configuration. The experiment bypasses the scored-candidate cache on enrolled requests so current discovery inventory goes through fresh hydration and filters. It skips Following, Ranked Following, topic-only, preview, diagnostic, polling, and shadow requests.

The public fork omits Home Mixer's Cargo manifest and private service dependencies. The local service and simulation run independently. Building and exercising the Home Mixer integration requires those upstream dependencies and access to the normal safety and relationship services.

## Event adapter contract

All endpoints accept JSON via authenticated POST. IDs are nonzero unsigned 64-bit integers. Use a lossless serializer for large IDs. Times use Unix seconds. Each viewer's snapshot should contain the authors they follow and the authors with whom they regularly engage. For an initial experiment, define regular engagement as two or more meaningful actions on different posts over the previous 30 days. Refresh on relationship changes and at least every 24 hours. Snapshots represent complete viewer lists. Empty lists are valid after an authoritative lookup. Missing or stale snapshots yield no discovery candidates.

| Endpoint | Example body | Caller and result |
|---|---|---|
| `/publish` | `{"post_id":1,"author_id":50,"published_at":1800000000,"eligible":true}` | Trusted publication and policy stream. Admit public, eligible original posts. Send `eligible:false` to withdraw removed or newly ineligible posts. |
| `/audience` | `{"viewer_id":2,"updated_at":1800000000,"established_author_ids":[51]}` | Trusted relationship and engagement snapshot stream. Replaces the previous list. |
| `/candidates` | `{"viewer_id":2}` | Home Mixer. Returns queued post IDs, authors, and stages in scheduling order. |
| `/offer` | `{"viewer_id":2,"request_id":3,"slots":20,"posts":[{"post_id":1,"stage":0}]}` | Home Mixer after feed assembly. Returns accepted post IDs. The slot count covers surviving organic recommendations. An offer holds capacity for up to 15 minutes. |
| `/confirm` | `{"viewer_id":2,"request_id":3,"post_id":1,"viewed":true,"dwell_ms":1200,"engaged":false}` | Trusted viewport telemetry adapter. Send a verified viewport event with consolidated outcomes during the offer window. Correlate the actual For You response request ID, viewer ID, and post ID. A valid event contributes at most once. |
| `/report` | `{}` | Experiment operator. Lists initial fulfillment, deadline misses, confirmed views and impressions, dwell, engagement and the latest expansion score. |

The ZIP provides the HTTP receivers and Home Mixer calls. Connect publication, audience, and viewport events from your environment to these endpoints. A feed response creates an offer. A verified viewport event counts as an impression, and its `viewed` field is a separate qualified-view signal such as an open or a view that met a dwell threshold. Use a consistent definition across cohorts. Browser clients should send their events to your existing trusted telemetry collector. Keep the service token on the server side.

## Allocation and expansion

- Initial queue order favors earlier deadlines. A deterministic viewer-specific shuffle breaks ties within a deadline minute, giving different viewers different samples. Each post can reach a viewer only once through the experiment, including across expansion stages.
- The source retrieves up to 32 candidates. Home Mixer runs the normal content, blocking, muting, subscription and visibility filters. Discovery placement also requires a successful relationship lookup, confirmed unfamiliarity, a non-blocked result, and a positive visibility decision. Discovery candidates can reach the visibility stage independently of personalized top-K score. Minimum-engagement ranking gates permit the reserved lane to sample unproven posts.
- The reservation uses the organic inventory after visibility checks. Ads, deduplication, and feed truncation can consume capacity. Home Mixer registers only surviving posts and caps creditable offers at the final organic slot budget. A race or an unavailable sidecar can leave a displayed candidate without an offer. Those displays earn zero quota credit.
- At 200 confirmations, a Wilson 95% lower bound of at least 20% on the qualified-view rate unlocks a fresh 200-viewer stage. Meeting the threshold again unlocks up to 400 more. A post can earn 800 confirmed discovery impressions in total. Stages end at 48 hours from publication at the latest, matching Home Mixer's post age filter.
- Expansion candidates are ordered by a 40/60 blend of age-adjusted view velocity and the Wilson lower bound. The velocity term follows the existing popular-post age curve, with log compression and a cap. Initial sampling occupies reserved slots ahead of expansion, so the attachment's illustrative exploration weight is represented by explicit allocation. Dwell and engagement are recorded for evaluation. Personalized ranking remains active for ordinary inventory.
- Admission defaults to 100 unfinished initial posts and three eligible posts per author per hour. Capacity rejections return HTTP 429 for retry with the original publication time. A post that still lacks 200 confirmations at its deadline is reported as missed. Withdrawn posts stop receiving offers. Expired unconfirmed offers release capacity, and concurrent workers share a transactional quota.

At 200 impressions per admitted post, a day with 100 admissions needs at least 20,000 distinct qualifying impressions, or at least 200,000 organic recommendation slots at the 10% share before drop-off and filtering. Size admission against actual demand, audience coverage, viewport confirmation rates, and deadline fulfillment. The SQLite service is intended for a local experiment with bounded traffic and retains its ledger history for review.
