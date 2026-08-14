"""
Unit tests for app/club_announcements.py — the server-side Discord
announcements engine (backlog #2). Everything runs against fakes: no
Firestore, no network. The FakeSource stands in for FirestoreClubs and a
recording poster stands in for the webhook HTTP call.
"""
import unittest
from datetime import datetime, timezone

from app.club_announcements import (
    FEATURE_KEY,
    MAX_EMBEDS_PER_RUN,
    baseline_entry,
    build_embed,
    club_page_url,
    detect_read_events,
    feature_enabled,
    member_position,
    next_due_milestone,
    on_track_summary,
    past_due_positions,
    plan_club,
    run,
    ts_millis,
)

DAY = 24 * 60 * 60 * 1000
NOW = 1_760_000_000_000  # fixed "now" in epoch millis


def ms(position, due=None, label=None, ch_end=None):
    m = {"id": f"m{position}", "label": label or f"Part {position + 1}", "position": position}
    if due is not None:
        m["dueAt"] = due
    if ch_end is not None:
        m["chEnd"] = ch_end
    return m


def make_read(read_id="r1", status="active", milestones=None, sched_ms=None, **extra):
    read = {
        "id": read_id,
        "bookTitle": "The Hobbit",
        "bookAuthor": "J.R.R. Tolkien",
        "status": status,
        "milestones": milestones if milestones is not None else [ms(0), ms(1)],
    }
    if sched_ms is not None:
        read["scheduleUpdatedAt"] = sched_ms
    read.update(extra)
    return read


class FakeSource:
    """In-memory stand-in for FirestoreClubs."""

    def __init__(self, clubs, webhooks=None, states=None, reads=None, progress=None):
        self.collection_name = "clubs"
        self._clubs = clubs
        self._webhooks = webhooks or {}
        self._states = states or {}
        self._reads = reads or {}
        self._progress = progress or {}
        self.saved_states = {}

    def clubs(self):
        return self._clubs

    def webhook_url(self, club_id):
        return self._webhooks.get(club_id, "")

    def state(self, club_id):
        # saved state wins so a failure-path re-read sees the update
        return self.saved_states.get(club_id, self._states.get(club_id))

    def save_state(self, club_id, state):
        self.saved_states[club_id] = state

    def reads(self, club_id):
        return self._reads.get(club_id, [])

    def progress(self, club_id, read_id):
        return self._progress.get((club_id, read_id), [])


class TestHelpers(unittest.TestCase):
    def test_ts_millis_accepts_datetime_and_numbers(self):
        dt = datetime(2026, 8, 14, tzinfo=timezone.utc)
        self.assertEqual(ts_millis(dt), int(dt.timestamp() * 1000))
        self.assertEqual(ts_millis(1234), 1234)
        self.assertEqual(ts_millis(12.9), 12)
        self.assertIsNone(ts_millis(None))
        self.assertIsNone(ts_millis(True))
        self.assertIsNone(ts_millis("2026-08-14"))

    def test_feature_defaults_off_and_key_wins(self):
        self.assertFalse(feature_enabled({}))
        self.assertFalse(feature_enabled({"features": {}}))
        self.assertFalse(feature_enabled({"features": {FEATURE_KEY: False}}))
        self.assertTrue(feature_enabled({"features": {FEATURE_KEY: True}}))
        self.assertFalse(feature_enabled({"features": {"readingSchedule": True}}))

    def test_club_page_url_lanes(self):
        self.assertEqual(
            club_page_url("abc", "", "https://audiobooks.heygabi.ai/"),
            "https://audiobooks.heygabi.ai/club.html?id=abc",
        )
        self.assertEqual(
            club_page_url("a b", "_dev", "https://audiobooks.heygabi.ai/"),
            "https://audiobooks.heygabi.ai/dev/club.html?id=a%20b",
        )

    def test_past_due_and_next_due(self):
        milestones = [ms(0, NOW - DAY), ms(1, NOW), ms(2, NOW + DAY), ms(3)]
        self.assertEqual(past_due_positions(milestones, NOW), [0, 1])
        self.assertEqual(next_due_milestone(milestones, NOW)["position"], 2)
        self.assertIsNone(next_due_milestone([ms(0)], NOW))


class TestScheduleMirrors(unittest.TestCase):
    """member_position / on_track_summary mirror club-reads.js semantics."""

    def test_member_position_milestone_based(self):
        milestones = [ms(0), ms(1), ms(2)]
        self.assertEqual(member_position(milestones, {}), -1)
        self.assertEqual(member_position(milestones, {"milestonePosition": 1}), 1)
        self.assertEqual(member_position(milestones, {"finished": True}), 2)

    def test_member_position_chaptered(self):
        milestones = [ms(0, ch_end=4), ms(1, ch_end=9)]
        self.assertEqual(member_position(milestones, {"chapterIndex": 3}), -1)
        self.assertEqual(member_position(milestones, {"chapterIndex": 4}), 0)
        self.assertEqual(member_position(milestones, {"chapterIndex": 9}), 1)

    def test_on_track_summary(self):
        milestones = [ms(0, NOW - DAY), ms(1, NOW + DAY)]
        progress = [
            {"milestonePosition": 0},          # on track (past-due section done)
            {"milestonePosition": -1},         # behind
            {"finished": True},                # done
        ]
        self.assertEqual(on_track_summary(milestones, progress, NOW), "2 of 3 readers on track")
        self.assertIsNone(on_track_summary(milestones, [], NOW))


class TestDetectReadEvents(unittest.TestCase):
    def test_new_active_read_announces_started_only(self):
        read = make_read(milestones=[ms(0, NOW - DAY), ms(1, NOW + DAY)], sched_ms=NOW - 2 * DAY)
        events, entry = detect_read_events(read, None, NOW)
        self.assertEqual([e["type"] for e in events], ["started"])
        # birth state swallows the pre-existing schedule and past due dates
        self.assertEqual(entry["scheduleAnnouncedMs"], NOW - 2 * DAY)
        self.assertEqual(entry["duePositions"], [0])

    def test_new_read_already_finished_announces_finished(self):
        events, _ = detect_read_events(make_read(status="finished"), None, NOW)
        self.assertEqual([e["type"] for e in events], ["finished"])

    def test_new_read_abandoned_is_silent(self):
        events, entry = detect_read_events(make_read(status="abandoned"), None, NOW)
        self.assertEqual(events, [])
        self.assertEqual(entry["status"], "abandoned")

    def test_finish_transition_announced_abandon_silent(self):
        prior = {"status": "active", "scheduleAnnouncedMs": 0, "duePositions": []}
        events, entry = detect_read_events(make_read(status="finished"), dict(prior), NOW)
        self.assertEqual([e["type"] for e in events], ["finished"])
        self.assertEqual(entry["status"], "finished")
        events, entry = detect_read_events(make_read(status="abandoned"), dict(prior), NOW)
        self.assertEqual(events, [])
        self.assertEqual(entry["status"], "abandoned")

    def test_schedule_change_announced_once(self):
        prior = {"status": "active", "scheduleAnnouncedMs": 100, "duePositions": []}
        read = make_read(milestones=[ms(0, NOW + DAY)], sched_ms=200)
        events, entry = detect_read_events(read, dict(prior), NOW)
        self.assertEqual([e["type"] for e in events], ["schedule"])
        self.assertEqual(entry["scheduleAnnouncedMs"], 200)
        # same scheduleUpdatedAt next run → silent
        events, _ = detect_read_events(read, dict(entry), NOW)
        self.assertEqual(events, [])

    def test_due_batches_and_marks(self):
        prior = {"status": "active", "scheduleAnnouncedMs": 300, "duePositions": [0]}
        read = make_read(
            milestones=[ms(0, NOW - 3 * DAY), ms(1, NOW - DAY), ms(2, NOW - 1), ms(3, NOW + DAY)],
            sched_ms=300,
        )
        events, entry = detect_read_events(read, dict(prior), NOW)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["type"], "due")
        self.assertEqual([m["position"] for m in events[0]["milestones"]], [1, 2])
        self.assertEqual(entry["duePositions"], [0, 1, 2])
        # nothing newly due next run
        events, _ = detect_read_events(read, dict(entry), NOW)
        self.assertEqual(events, [])

    def test_schedule_change_swallows_same_run_due(self):
        prior = {"status": "active", "scheduleAnnouncedMs": 100, "duePositions": []}
        read = make_read(milestones=[ms(0, NOW - DAY), ms(1, NOW + DAY)], sched_ms=200)
        events, entry = detect_read_events(read, dict(prior), NOW)
        self.assertEqual([e["type"] for e in events], ["schedule"])
        self.assertEqual(entry["duePositions"], [0])  # marked, not double-posted

    def test_finished_read_does_not_nudge(self):
        prior = {"status": "finished", "scheduleAnnouncedMs": 0, "duePositions": []}
        read = make_read(status="finished", milestones=[ms(0, NOW - DAY)], sched_ms=100)
        events, _ = detect_read_events(read, dict(prior), NOW)
        self.assertEqual(events, [])


class TestEmbeds(unittest.TestCase):
    CLUB = {"id": "c1", "name": "Night Readers"}
    LINK = "https://audiobooks.heygabi.ai/club.html?id=c1"

    def test_due_embed_contents(self):
        read = make_read(
            milestones=[ms(0, NOW - DAY, label="Ch 1-5"), ms(1, NOW + DAY, label="Ch 6-10")],
            coverHref="https://covers.heygabi.ai/x.jpg",
        )
        event = {"type": "due", "read": read, "milestones": [read["milestones"][0]]}
        embed = build_embed(event, self.CLUB, self.LINK, NOW, [{"milestonePosition": 0}])
        self.assertIn("The Hobbit", embed["title"])
        self.assertIn("Ch 1-5", embed["description"])
        self.assertIn("Ch 6-10", embed["description"])  # next-up line
        self.assertEqual(embed["thumbnail"], {"url": "https://covers.heygabi.ai/x.jpg"})
        field_names = [f["name"] for f in embed["fields"]]
        self.assertIn("Club", field_names)
        self.assertIn("Pace", field_names)
        self.assertIn(self.LINK, embed["fields"][0]["value"])

    def test_started_and_finished_and_schedule_embeds(self):
        read = make_read(milestones=[ms(0, NOW + DAY)])
        for kind in ("started", "finished", "schedule"):
            event = {"type": kind, "read": read}
            embed = build_embed(event, self.CLUB, self.LINK, NOW)
            self.assertTrue(embed["title"])
            self.assertIn(self.LINK, embed["fields"][0]["value"])
            self.assertNotIn("thumbnail", embed)  # no coverHref on this read


class TestPlanAndRun(unittest.TestCase):
    def enabled_club(self, club_id="c1"):
        return {"id": club_id, "name": "Night Readers", "features": {FEATURE_KEY: True}}

    def test_first_run_is_silent_baseline(self):
        club = self.enabled_club()
        source = FakeSource(
            [club],
            webhooks={"c1": "https://discord.com/api/webhooks/1/tok"},
            reads={"c1": [make_read(milestones=[ms(0, NOW - DAY)], sched_ms=123)]},
        )
        posts = []
        stats = run(source, now_ms=NOW, poster=lambda url, embeds: posts.append((url, embeds)))
        self.assertEqual(posts, [])
        self.assertEqual(stats["baselined"], 1)
        state = source.saved_states["c1"]
        self.assertEqual(state["reads"]["r1"], baseline_entry(make_read(milestones=[ms(0, NOW - DAY)], sched_ms=123), NOW))
        self.assertEqual(state["consecutiveFailures"], 0)

    def test_disabled_or_webhookless_clubs_are_skipped(self):
        clubs = [
            {"id": "off", "name": "Off"},                       # feature default OFF
            self.enabled_club("nohook"),                        # on, but no webhook
        ]
        source = FakeSource(clubs, reads={"nohook": [make_read()]})
        posts = []
        stats = run(source, now_ms=NOW, poster=lambda url, embeds: posts.append(1))
        self.assertEqual(posts, [])
        self.assertEqual(stats["skipped_no_webhook"], 1)
        self.assertEqual(source.saved_states, {})  # no state churn for skipped clubs

    def test_events_post_once_and_advance_state(self):
        club = self.enabled_club()
        prior = {
            "lastRunAt": "x",
            "reads": {"r1": {"status": "active", "scheduleAnnouncedMs": 0, "duePositions": []}},
            "consecutiveFailures": 0,
            "lastError": None,
        }
        read = make_read(milestones=[ms(0, NOW - DAY), ms(1, NOW + DAY)])
        source = FakeSource(
            [club],
            webhooks={"c1": "https://discord.com/api/webhooks/1/tok"},
            states={"c1": prior},
            reads={"c1": [read]},
            progress={("c1", "r1"): [{"finished": True}]},
        )
        posts = []
        stats = run(source, now_ms=NOW, poster=lambda url, embeds: posts.append((url, embeds)))
        self.assertEqual(len(posts), 1)
        url, embeds = posts[0]
        self.assertEqual(url, "https://discord.com/api/webhooks/1/tok")
        self.assertEqual(len(embeds), 1)
        self.assertIn("check-in", embeds[0]["title"])
        self.assertEqual(stats["posted"], 1)
        self.assertEqual(source.saved_states["c1"]["reads"]["r1"]["duePositions"], [0])

        # Second run from the saved state: silence.
        source2 = FakeSource(
            [club],
            webhooks={"c1": "https://discord.com/api/webhooks/1/tok"},
            states={"c1": source.saved_states["c1"]},
            reads={"c1": [read]},
        )
        posts2 = []
        run(source2, now_ms=NOW, poster=lambda url, embeds: posts2.append(1))
        self.assertEqual(posts2, [])

    def test_webhook_failure_records_and_retries(self):
        club = self.enabled_club()
        prior = {
            "lastRunAt": "x",
            "reads": {"r1": {"status": "active", "scheduleAnnouncedMs": 0, "duePositions": []}},
            "consecutiveFailures": 1,
            "lastError": None,
        }
        read = make_read(milestones=[ms(0, NOW - DAY)])
        source = FakeSource(
            [club],
            webhooks={"c1": "https://discord.com/api/webhooks/1/dead"},
            states={"c1": prior},
            reads={"c1": [read]},
        )

        def dead(url, embeds):
            raise RuntimeError("webhook returned HTTP 404")

        stats = run(source, now_ms=NOW, poster=dead)
        self.assertEqual(stats["failed"], 1)
        self.assertEqual(stats["posted"], 0)
        state = source.saved_states["c1"]
        self.assertEqual(state["consecutiveFailures"], 2)
        self.assertIn("404", state["lastError"])
        # crucial: the due marker did NOT advance, so next run retries
        self.assertEqual(state["reads"]["r1"]["duePositions"], [])

    def test_dry_run_posts_and_saves_nothing(self):
        club = self.enabled_club()
        prior = {
            "lastRunAt": "x",
            "reads": {"r1": {"status": "active", "scheduleAnnouncedMs": 0, "duePositions": []}},
            "consecutiveFailures": 0,
            "lastError": None,
        }
        source = FakeSource(
            [club],
            webhooks={"c1": "https://discord.com/api/webhooks/1/tok"},
            states={"c1": prior},
            reads={"c1": [make_read(milestones=[ms(0, NOW - DAY)])]},
        )
        posts = []
        run(source, now_ms=NOW, dry_run=True, poster=lambda url, embeds: posts.append(1))
        self.assertEqual(posts, [])
        self.assertEqual(source.saved_states, {})

    def test_embed_cap(self):
        club = self.enabled_club()
        prior_reads = {}
        reads = []
        for i in range(MAX_EMBEDS_PER_RUN + 2):
            rid = f"r{i}"
            prior_reads[rid] = {"status": "active", "scheduleAnnouncedMs": 0, "duePositions": []}
            reads.append(make_read(read_id=rid, milestones=[ms(0, NOW - DAY)]))
        source = FakeSource(
            [club],
            webhooks={"c1": "https://discord.com/api/webhooks/1/tok"},
            states={"c1": {"lastRunAt": "x", "reads": prior_reads, "consecutiveFailures": 0, "lastError": None}},
            reads={"c1": reads},
        )
        embeds, _, _ = plan_club(source, club, NOW, "")
        self.assertEqual(len(embeds), MAX_EMBEDS_PER_RUN)

    def test_one_broken_club_does_not_stop_the_sweep(self):
        good = self.enabled_club("good")

        class Exploding(FakeSource):
            def reads(self, club_id):
                if club_id == "bad":
                    raise RuntimeError("boom")
                return super().reads(club_id)

        source = Exploding(
            [self.enabled_club("bad"), good],
            webhooks={"bad": "https://discord.com/api/webhooks/1/a", "good": "https://discord.com/api/webhooks/1/b"},
            states={"good": {"lastRunAt": "x", "reads": {}, "consecutiveFailures": 0, "lastError": None}},
            reads={"good": [make_read()]},
        )
        posts = []
        stats = run(source, now_ms=NOW, poster=lambda url, embeds: posts.append(url))
        self.assertEqual(stats["failed"], 1)
        self.assertEqual(posts, ["https://discord.com/api/webhooks/1/b"])  # 'started' for the new read


if __name__ == "__main__":
    unittest.main()
