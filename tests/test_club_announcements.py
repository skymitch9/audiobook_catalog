"""
Unit tests for app/club_announcements.py — the server-side Discord
announcements engine (backlog #2). Everything runs against fakes: no
Firestore, no network. The FakeSource stands in for FirestoreClubs and a
recording poster stands in for the webhook HTTP call.
"""
import os
import unittest
from datetime import datetime, timezone
from unittest import mock

from app.club_announcements import (
    EVENT_PRIORITY,
    FEATURE_KEY,
    MAX_EMBEDS_PER_RUN,
    POLL_FEATURE_KEY,
    POLL_SYNC_URL_DEFAULT,
    REMINDER_WINDOW_MS,
    baseline_entry,
    build_embed,
    build_meeting_embed,
    build_poll_embed,
    build_ratings_embed,
    build_tbr_embed,
    club_page_url,
    detect_meeting_events,
    detect_poll_events,
    detect_read_events,
    detect_tbr_event,
    feature_enabled,
    member_position,
    next_due_milestone,
    on_track_summary,
    past_due_positions,
    plan_club,
    poll_winners,
    run,
    sync_poll_messages,
    sync_question_messages,
    QUESTION_SYNC_URL_DEFAULT,
    tally_poll_votes,
    tally_ratings,
    tbr_leader,
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

    def __init__(self, clubs, webhooks=None, states=None, reads=None, progress=None,
                 polls=None, poll_votes=None, ratings=None, tbr=None):
        self.collection_name = "clubs"
        self._clubs = clubs
        self._webhooks = webhooks or {}
        self._states = states or {}
        self._reads = reads or {}
        self._progress = progress or {}
        self._polls = polls or {}
        self._poll_votes = poll_votes or {}
        self._ratings = ratings or {}
        self._tbr = tbr or {}
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

    def polls(self, club_id):
        return self._polls.get(club_id, [])

    def poll_votes(self, club_id, poll_id):
        return self._poll_votes.get((club_id, poll_id), [])

    def ratings(self, club_id, read_id):
        return self._ratings.get((club_id, read_id), [])

    def tbr(self, club_id):
        return self._tbr.get(club_id, [])


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

    def test_poll_feature_key_defaults_off_independent_of_master(self):
        """backlog #2c: the poll sub-toggle defaults OFF and does not follow
        the master discordAnnouncements flag either way."""
        self.assertFalse(feature_enabled({}, POLL_FEATURE_KEY))
        self.assertFalse(feature_enabled({"features": {FEATURE_KEY: True}}, POLL_FEATURE_KEY))
        self.assertFalse(feature_enabled({"features": {POLL_FEATURE_KEY: False}}, POLL_FEATURE_KEY))
        self.assertTrue(feature_enabled(
            {"features": {FEATURE_KEY: True, POLL_FEATURE_KEY: True}}, POLL_FEATURE_KEY
        ))

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


class TestRatingsReveal(unittest.TestCase):
    """detect_read_events' piggybacked ratings-reveal branch (backlog #2b)."""

    def test_reveal_announced_once(self):
        prior = {"status": "active", "scheduleAnnouncedMs": 0, "duePositions": [], "ratingsRevealedSeen": False}
        read = make_read(ratingsRevealed=True)
        events, entry = detect_read_events(read, dict(prior), NOW)
        self.assertEqual([e["type"] for e in events], ["ratings_revealed"])
        self.assertTrue(entry["ratingsRevealedSeen"])
        # same run's state next time round: silent
        events, _ = detect_read_events(read, dict(entry), NOW)
        self.assertEqual(events, [])

    def test_missing_marker_backfills_silently(self):
        """Additive-state compat: an OLDER state doc's read entry has no
        ratingsRevealedSeen key at all. A read already revealed before this
        engine upgrade must NOT suddenly announce on the first run after."""
        prior = {"status": "active", "scheduleAnnouncedMs": 0, "duePositions": []}  # pre-#2b shape
        read = make_read(ratingsRevealed=True)
        events, entry = detect_read_events(read, dict(prior), NOW)
        self.assertEqual(events, [])
        self.assertTrue(entry["ratingsRevealedSeen"])  # adopted silently
        # next run: still true, still silent (no false->true edge left)
        events, _ = detect_read_events(read, dict(entry), NOW)
        self.assertEqual(events, [])

    def test_not_revealed_yet_no_event(self):
        prior = {"status": "active", "scheduleAnnouncedMs": 0, "duePositions": [], "ratingsRevealedSeen": False}
        events, entry = detect_read_events(make_read(ratingsRevealed=False), dict(prior), NOW)
        self.assertEqual(events, [])
        self.assertFalse(entry["ratingsRevealedSeen"])

    def test_tally_ratings(self):
        self.assertEqual(tally_ratings([{"rating": 4}, {"rating": 5}, {"rating": 3}]), (4.0, 3))
        self.assertEqual(tally_ratings([]), (0.0, 0))
        self.assertEqual(tally_ratings([{"rating": "oops"}, {"comment": "no rating field"}]), (0.0, 0))


class TestPollEvents(unittest.TestCase):
    def test_open_to_closed_announces(self):
        poll = {"id": "p1", "question": "Best?", "options": ["A", "B"], "status": "closed"}
        events, entry = detect_poll_events(poll, {"status": "open"}, True)
        self.assertEqual([e["type"] for e in events], ["poll_closed"])
        self.assertEqual(entry, {"status": "closed"})

    def test_still_open_is_silent(self):
        poll = {"id": "p1", "status": "open"}
        events, _ = detect_poll_events(poll, {"status": "open"}, True)
        self.assertEqual(events, [])

    def test_already_closed_next_run_is_silent(self):
        poll = {"id": "p1", "status": "closed"}
        events, _ = detect_poll_events(poll, {"status": "closed"}, True)
        self.assertEqual(events, [])

    def test_new_poll_born_already_closed_announces(self):
        """Mirrors detect_read_events' 'appeared AND finished between runs'
        case: a poll created and closed within one run interval still gets
        its one announcement, PROVIDED this club's poll-tracking already
        existed (a genuinely new poll id, not an upgrade backfill)."""
        poll = {"id": "p1", "status": "closed"}
        events, entry = detect_poll_events(poll, None, True)
        self.assertEqual([e["type"] for e in events], ["poll_closed"])
        self.assertEqual(entry, {"status": "closed"})

    def test_upgrade_backfill_of_already_closed_poll_is_silent(self):
        """Additive-state compat: this club's `polls` dict is being seen for
        the first time (older announceState doc, pre-#2b) — every current
        poll, even one closed long ago, must NOT suddenly announce."""
        poll = {"id": "p1", "status": "closed"}
        events, entry = detect_poll_events(poll, None, False)
        self.assertEqual(events, [])
        self.assertEqual(entry, {"status": "closed"})

    def test_tally_and_winners(self):
        options = ["A", "B", "C"]
        votes = [{"optionIndex": 0}, {"optionIndex": 1}, {"optionIndex": 1}]
        self.assertEqual(tally_poll_votes(options, votes), [1, 2, 0])
        winners, counts = poll_winners(options, votes)
        self.assertEqual(winners, [1])
        self.assertEqual(counts, [1, 2, 0])

    def test_winners_tie(self):
        winners, counts = poll_winners(["A", "B", "C"], [{"optionIndex": 0}, {"optionIndex": 1}])
        self.assertEqual(winners, [0, 1])

    def test_no_votes_no_winners(self):
        winners, counts = poll_winners(["A", "B"], [])
        self.assertEqual(winners, [])
        self.assertEqual(counts, [0, 0])

    def test_bad_option_index_ignored(self):
        # out-of-range / non-numeric / bool indices never crash or count
        votes = [{"optionIndex": 99}, {"optionIndex": "nope"}, {"optionIndex": True}, {}]
        self.assertEqual(tally_poll_votes(["A", "B"], votes), [0, 0])


class TestPollEmbeds(unittest.TestCase):
    CLUB = {"id": "c1", "name": "Night Readers"}
    LINK = "https://audiobooks.heygabi.ai/club.html?id=c1"

    def test_string_option_shape(self):
        poll = {"question": "Next read?", "options": ["The Hobbit", "Mistborn"]}
        event = {"poll": poll, "votes": [{"optionIndex": 1}, {"optionIndex": 1}, {"optionIndex": 0}]}
        embed = build_poll_embed(event, self.CLUB, self.LINK)
        self.assertIn("Mistborn", embed["description"])
        self.assertIn("Winner:", embed["description"])
        self.assertIn("3 votes total", embed["description"])
        self.assertNotIn("thumbnail", embed)  # plain strings carry no cover

    def test_book_ref_option_shape(self):
        """backlog #3b (next-book polls) not shipped yet — code defensively
        handles a book-ref option object anyway."""
        poll = {
            "question": "Next read?",
            "options": [
                {"title": "Dune", "author": "Frank Herbert", "cover": "https://covers.heygabi.ai/dune.jpg"},
                {"bookTitle": "Foundation", "bookAuthor": "Isaac Asimov"},
            ],
        }
        event = {"poll": poll, "votes": [{"optionIndex": 0}]}
        embed = build_poll_embed(event, self.CLUB, self.LINK)
        self.assertIn("Dune", embed["description"])
        self.assertIn("Frank Herbert", embed["description"])
        self.assertEqual(embed["thumbnail"], {"url": "https://covers.heygabi.ai/dune.jpg"})

    def test_tie_shows_no_thumbnail(self):
        poll = {"question": "?", "options": [{"title": "A"}, {"title": "B"}]}
        event = {"poll": poll, "votes": [{"optionIndex": 0}, {"optionIndex": 1}]}
        embed = build_poll_embed(event, self.CLUB, self.LINK)
        self.assertIn("Tied at the top", embed["description"])
        self.assertNotIn("thumbnail", embed)

    def test_no_votes(self):
        poll = {"question": "?", "options": ["A", "B"]}
        embed = build_poll_embed({"poll": poll, "votes": []}, self.CLUB, self.LINK)
        self.assertIn("No votes were cast", embed["description"])


class TestMeetingEvents(unittest.TestCase):
    def test_missing_marker_backfills_silently(self):
        club = {"nextMeetingAt": NOW + DAY}
        events, state = detect_meeting_events(club, {}, NOW)
        self.assertEqual(events, [])
        self.assertEqual(state, {"meetingAnnouncedMs": NOW + DAY, "meetingReminderSentFor": None})

    def test_unchanged_meeting_is_silent(self):
        club = {"nextMeetingAt": NOW + DAY}
        prior = {"meetingAnnouncedMs": NOW + DAY, "meetingReminderSentFor": None}
        events, _ = detect_meeting_events(club, prior, NOW)
        self.assertEqual(events, [])

    def test_new_or_changed_meeting_announces_and_resets_reminder(self):
        club = {"nextMeetingAt": NOW + 2 * DAY}
        prior = {"meetingAnnouncedMs": NOW + DAY, "meetingReminderSentFor": NOW + DAY}
        events, state = detect_meeting_events(club, prior, NOW)
        self.assertEqual([e["type"] for e in events], ["meeting"])
        self.assertEqual(state["meetingAnnouncedMs"], NOW + 2 * DAY)
        self.assertIsNone(state["meetingReminderSentFor"])

    def test_reminder_fires_once_within_window(self):
        soon = NOW + REMINDER_WINDOW_MS // 2  # halfway into the window
        club = {"nextMeetingAt": soon}
        prior = {"meetingAnnouncedMs": soon, "meetingReminderSentFor": None}
        events, state = detect_meeting_events(club, prior, NOW)
        self.assertEqual([e["type"] for e in events], ["meeting_reminder"])
        self.assertEqual(state["meetingReminderSentFor"], soon)
        # same meeting instant, reminder already sent: silent next run
        events2, state2 = detect_meeting_events(club, state, NOW)
        self.assertEqual(events2, [])
        self.assertEqual(state2["meetingReminderSentFor"], soon)

    def test_reminder_outside_window_is_silent(self):
        club = {"nextMeetingAt": NOW + DAY}  # 24h out, beyond the ~8h window
        prior = {"meetingAnnouncedMs": NOW + DAY, "meetingReminderSentFor": None}
        events, _ = detect_meeting_events(club, prior, NOW)
        self.assertEqual(events, [])

    def test_cleared_meeting_resets_marker(self):
        club = {"nextMeetingAt": None}
        prior = {"meetingAnnouncedMs": NOW + DAY, "meetingReminderSentFor": None}
        events, state = detect_meeting_events(club, prior, NOW)
        self.assertEqual(events, [])
        self.assertEqual(state["meetingAnnouncedMs"], 0)


class TestMeetingEmbeds(unittest.TestCase):
    CLUB = {"id": "c1", "name": "Night Readers", "nextMeetingNotes": "Bring snacks"}
    LINK = "https://audiobooks.heygabi.ai/club.html?id=c1"

    def test_scheduled_and_reminder_embeds(self):
        for kind in ("meeting", "meeting_reminder"):
            event = {"type": kind, "meetingMs": NOW}
            embed = build_meeting_embed(event, self.CLUB, self.LINK)
            self.assertTrue(embed["title"])
            self.assertIn("Bring snacks", embed["description"])
            self.assertIn(self.LINK, embed["fields"][0]["value"])


class TestTbrEvents(unittest.TestCase):
    def test_strict_leader(self):
        items = [{"id": "a", "voterSlugs": ["x", "y"]}, {"id": "b", "voterSlugs": ["x"]}]
        self.assertEqual(tbr_leader(items)["id"], "a")

    def test_tie_has_no_leader(self):
        items = [{"id": "a", "voterSlugs": ["x"]}, {"id": "b", "voterSlugs": ["y"]}]
        self.assertIsNone(tbr_leader(items))

    def test_empty_or_unvoted_has_no_leader(self):
        self.assertIsNone(tbr_leader([]))
        self.assertIsNone(tbr_leader([{"id": "a", "voterSlugs": []}]))

    def test_missing_marker_backfills_silently(self):
        items = [{"id": "a", "voterSlugs": ["x"]}]
        events, state = detect_tbr_event(items, {})
        self.assertEqual(events, [])
        self.assertEqual(state, {"tbrLeaderId": "a"})

    def test_leader_change_announces(self):
        items = [{"id": "a", "voterSlugs": ["x"]}, {"id": "b", "voterSlugs": ["x", "y", "z"]}]
        events, state = detect_tbr_event(items, {"tbrLeaderId": "a"})
        self.assertEqual([e["type"] for e in events], ["tbr_leader"])
        self.assertEqual(events[0]["item"]["id"], "b")
        self.assertEqual(state, {"tbrLeaderId": "b"})

    def test_unchanged_leader_is_silent(self):
        items = [{"id": "a", "voterSlugs": ["x", "y"]}, {"id": "b", "voterSlugs": ["x"]}]
        events, state = detect_tbr_event(items, {"tbrLeaderId": "a"})
        self.assertEqual(events, [])
        self.assertEqual(state, {"tbrLeaderId": "a"})

    def test_transient_tie_does_not_erase_marker(self):
        """A tie must not overwrite the marker with None — otherwise the tie
        resolving back to the same book would look like a 'new' leader."""
        items = [{"id": "a", "voterSlugs": ["x"]}, {"id": "b", "voterSlugs": ["y"]}]
        events, state = detect_tbr_event(items, {"tbrLeaderId": "a"})
        self.assertEqual(events, [])
        self.assertEqual(state, {"tbrLeaderId": "a"})


class TestTbrEmbed(unittest.TestCase):
    def test_embed_contents(self):
        item = {"bookTitle": "Dune", "bookAuthor": "Frank Herbert", "voterSlugs": ["x", "y"],
                "coverHref": "https://covers.heygabi.ai/dune.jpg"}
        embed = build_tbr_embed({"item": item}, {"id": "c1", "name": "Night Readers"},
                                "https://audiobooks.heygabi.ai/club.html?id=c1")
        self.assertIn("Dune", embed["description"])
        self.assertIn("2 votes", embed["description"])
        self.assertEqual(embed["thumbnail"], {"url": "https://covers.heygabi.ai/dune.jpg"})


class TestPlanAndRunNewEvents(unittest.TestCase):
    """Integration coverage for #2b events through plan_club/run, in the same
    all-fakes style as TestPlanAndRun."""

    def enabled_club(self, club_id="c1", **extra):
        club = {"id": club_id, "name": "Night Readers", "features": {FEATURE_KEY: True}}
        club.update(extra)
        return club

    def base_state(self, **overrides):
        state = {
            "lastRunAt": "x",
            "reads": {},
            "polls": {},
            "meetingAnnouncedMs": 0,
            "meetingReminderSentFor": None,
            "tbrLeaderId": None,
            "consecutiveFailures": 0,
            "lastError": None,
        }
        state.update(overrides)
        return state

    def test_baseline_records_polls_meeting_and_tbr(self):
        club = self.enabled_club(nextMeetingAt=NOW + DAY)
        source = FakeSource(
            [club],
            webhooks={"c1": "https://discord.com/api/webhooks/1/tok"},
            polls={"c1": [{"id": "p1", "status": "closed"}]},
            tbr={"c1": [{"id": "t1", "voterSlugs": ["x"]}]},
        )
        posts = []
        run(source, now_ms=NOW, poster=lambda url, embeds: posts.append(1))
        self.assertEqual(posts, [])  # silent baseline
        state = source.saved_states["c1"]
        self.assertEqual(state["polls"], {"p1": {"status": "closed"}})
        self.assertEqual(state["meetingAnnouncedMs"], NOW + DAY)
        self.assertEqual(state["tbrLeaderId"], "t1")

    def test_poll_closed_posts_and_advances_marker(self):
        """Both flags on (backlog #2c: the master toggle alone is not enough
        — see test_poll_closed_suppressed_without_poll_flag below)."""
        club = self.enabled_club(features={FEATURE_KEY: True, POLL_FEATURE_KEY: True})
        source = FakeSource(
            [club],
            webhooks={"c1": "https://discord.com/api/webhooks/1/tok"},
            states={"c1": self.base_state(polls={"p1": {"status": "open"}})},
            polls={"c1": [{"id": "p1", "question": "Next?", "options": ["A", "B"], "status": "closed"}]},
            poll_votes={("c1", "p1"): [{"optionIndex": 0}]},
        )
        posts = []
        run(source, now_ms=NOW, poster=lambda url, embeds: posts.append((url, embeds)))
        self.assertEqual(len(posts), 1)
        self.assertEqual(len(posts[0][1]), 1)
        self.assertIn("Poll closed", posts[0][1][0]["title"])
        self.assertEqual(source.saved_states["c1"]["polls"]["p1"]["status"], "closed")

    def test_poll_closed_suppressed_without_poll_flag(self):
        """backlog #2c: discordAnnouncements alone (no discordPollAnnouncements)
        must NOT announce a poll closing — the marker still advances so a
        later flag flip doesn't retroactively spam this transition."""
        club = self.enabled_club()  # master flag only, poll sub-toggle absent
        source = FakeSource(
            [club],
            webhooks={"c1": "https://discord.com/api/webhooks/1/tok"},
            states={"c1": self.base_state(polls={"p1": {"status": "open"}})},
            polls={"c1": [{"id": "p1", "question": "Next?", "options": ["A", "B"], "status": "closed"}]},
            poll_votes={("c1", "p1"): [{"optionIndex": 0}]},
        )
        posts = []
        run(source, now_ms=NOW, poster=lambda url, embeds: posts.append((url, embeds)))
        self.assertEqual(posts, [])
        self.assertEqual(source.saved_states["c1"]["polls"]["p1"]["status"], "closed")

    def test_poll_closed_suppressed_when_poll_flag_explicitly_off(self):
        club = self.enabled_club(features={FEATURE_KEY: True, POLL_FEATURE_KEY: False})
        source = FakeSource(
            [club],
            webhooks={"c1": "https://discord.com/api/webhooks/1/tok"},
            states={"c1": self.base_state(polls={"p1": {"status": "open"}})},
            polls={"c1": [{"id": "p1", "question": "Next?", "options": ["A", "B"], "status": "closed"}]},
            poll_votes={("c1", "p1"): [{"optionIndex": 0}]},
        )
        posts = []
        run(source, now_ms=NOW, poster=lambda url, embeds: posts.append((url, embeds)))
        self.assertEqual(posts, [])

    def test_poll_flag_flipped_on_later_does_not_replay_old_closes(self):
        """The marker already recorded 'closed' while the sub-toggle was off
        (previous run); enabling it now must not retroactively announce."""
        club = self.enabled_club(features={FEATURE_KEY: True, POLL_FEATURE_KEY: True})
        source = FakeSource(
            [club],
            webhooks={"c1": "https://discord.com/api/webhooks/1/tok"},
            states={"c1": self.base_state(polls={"p1": {"status": "closed"}})},
            polls={"c1": [{"id": "p1", "question": "Next?", "options": ["A", "B"], "status": "closed"}]},
            poll_votes={("c1", "p1"): [{"optionIndex": 0}]},
        )
        posts = []
        run(source, now_ms=NOW, poster=lambda url, embeds: posts.append((url, embeds)))
        self.assertEqual(posts, [])

    def test_ratings_revealed_posts(self):
        club = self.enabled_club()
        read = make_read(ratingsRevealed=True)
        source = FakeSource(
            [club],
            webhooks={"c1": "https://discord.com/api/webhooks/1/tok"},
            states={"c1": self.base_state(reads={"r1": {
                "status": "active", "scheduleAnnouncedMs": 0, "duePositions": [], "ratingsRevealedSeen": False,
            }})},
            reads={"c1": [read]},
            ratings={("c1", "r1"): [{"rating": 4}, {"rating": 5}]},
        )
        posts = []
        run(source, now_ms=NOW, poster=lambda url, embeds: posts.append((url, embeds)))
        self.assertEqual(len(posts), 1)
        embed = posts[0][1][0]
        self.assertIn("Ratings revealed", embed["title"])
        self.assertIn("4.5", embed["description"])

    def test_meeting_reminder_posts(self):
        soon = NOW + 4 * 60 * 60 * 1000
        club = self.enabled_club(nextMeetingAt=soon)
        source = FakeSource(
            [club],
            webhooks={"c1": "https://discord.com/api/webhooks/1/tok"},
            states={"c1": self.base_state(meetingAnnouncedMs=soon)},
        )
        posts = []
        run(source, now_ms=NOW, poster=lambda url, embeds: posts.append((url, embeds)))
        self.assertEqual(len(posts), 1)
        self.assertIn("reminder", posts[0][1][0]["title"].lower())
        self.assertEqual(source.saved_states["c1"]["meetingReminderSentFor"], soon)

        # second run: single-fire, no repeat post
        source2 = FakeSource(
            [club],
            webhooks={"c1": "https://discord.com/api/webhooks/1/tok"},
            states={"c1": source.saved_states["c1"]},
        )
        posts2 = []
        run(source2, now_ms=NOW, poster=lambda url, embeds: posts2.append(1))
        self.assertEqual(posts2, [])

    def test_tbr_leader_change_posts(self):
        club = self.enabled_club()
        source = FakeSource(
            [club],
            webhooks={"c1": "https://discord.com/api/webhooks/1/tok"},
            states={"c1": self.base_state(tbrLeaderId="a")},
            tbr={"c1": [{"id": "a", "voterSlugs": ["x"]}, {"id": "b", "voterSlugs": ["x", "y", "z"]}]},
        )
        posts = []
        run(source, now_ms=NOW, poster=lambda url, embeds: posts.append((url, embeds)))
        self.assertEqual(len(posts), 1)
        self.assertIn("TBR leader", posts[0][1][0]["title"])
        self.assertEqual(source.saved_states["c1"]["tbrLeaderId"], "b")

    def test_additive_state_compat_old_doc_shape(self):
        """A pre-#2b state doc (only reads/consecutiveFailures/lastError,
        no polls/meeting/tbr keys at all) must not crash and must not spam
        anything already true/existing at upgrade time."""
        club = self.enabled_club(nextMeetingAt=NOW + DAY)
        old_shape_state = {
            "lastRunAt": "x",
            "reads": {"r1": {"status": "active", "scheduleAnnouncedMs": 0, "duePositions": []}},
            "consecutiveFailures": 0,
            "lastError": None,
        }
        source = FakeSource(
            [club],
            webhooks={"c1": "https://discord.com/api/webhooks/1/tok"},
            states={"c1": old_shape_state},
            reads={"c1": [make_read(ratingsRevealed=True)]},  # already revealed pre-upgrade
            polls={"c1": [{"id": "p1", "status": "closed"}]},  # already closed pre-upgrade
            tbr={"c1": [{"id": "t1", "voterSlugs": ["x"]}]},
        )
        posts = []
        run(source, now_ms=NOW, poster=lambda url, embeds: posts.append(1))
        self.assertEqual(posts, [])  # nothing pre-existing gets announced
        state = source.saved_states["c1"]
        self.assertTrue(state["reads"]["r1"]["ratingsRevealedSeen"])
        self.assertEqual(state["polls"], {"p1": {"status": "closed"}})
        self.assertEqual(state["meetingAnnouncedMs"], NOW + DAY)
        self.assertEqual(state["tbrLeaderId"], "t1")

    def test_priority_cap_keeps_higher_tiers(self):
        """due > ratings_revealed > poll_closed > meeting > (started/finished/
        schedule/tbr) when more events fire than MAX_EMBEDS_PER_RUN allows."""
        club = self.enabled_club(
            nextMeetingAt=NOW + 2 * DAY, features={FEATURE_KEY: True, POLL_FEATURE_KEY: True}
        )
        reads = [make_read(read_id="rdue", milestones=[ms(0, NOW - DAY)]),
                 make_read(read_id="rnew", status="active")]  # 'started' — lowest tier
        prior_reads = {
            "rdue": {"status": "active", "scheduleAnnouncedMs": 0, "duePositions": []},
            # 'rnew' absent -> new read -> 'started'
        }
        source = FakeSource(
            [club],
            webhooks={"c1": "https://discord.com/api/webhooks/1/tok"},
            states={"c1": self.base_state(
                reads=prior_reads,
                polls={"p1": {"status": "open"}},
                meetingAnnouncedMs=NOW,  # will change -> 'meeting' tier 3
                tbrLeaderId="a",
            )},
            reads={"c1": reads},
            polls={"c1": [{"id": "p1", "question": "?", "options": ["A", "B"], "status": "closed"}]},
            tbr={"c1": [{"id": "a", "voterSlugs": ["x"]}, {"id": "b", "voterSlugs": ["x", "y"]}]},
        )
        embeds, _, _ = plan_club(source, club, NOW, "")
        self.assertLessEqual(len(embeds), MAX_EMBEDS_PER_RUN)
        titles = [e["title"] for e in embeds]
        # due (tier 0) and poll closed (tier 2) must survive the cap ahead of
        # the tier-4 'started' embed, given only 5 total events and cap 6 —
        # nothing should actually be dropped here, so assert all made it and
        # in priority order.
        self.assertTrue(any("check-in" in t for t in titles))
        self.assertTrue(any("Poll closed" in t for t in titles))
        priorities = []
        for e in embeds:
            if "check-in" in e["title"]:
                priorities.append(EVENT_PRIORITY["due"])
            elif "Poll closed" in e["title"]:
                priorities.append(EVENT_PRIORITY["poll_closed"])
            elif "Meeting scheduled" in e["title"]:
                priorities.append(EVENT_PRIORITY["meeting"])
            elif "TBR leader" in e["title"]:
                priorities.append(EVENT_PRIORITY["tbr_leader"])
            elif "New club read" in e["title"]:
                priorities.append(EVENT_PRIORITY["started"])
        self.assertEqual(priorities, sorted(priorities))


class TestPollMessageSync(unittest.TestCase):
    """
    The GABI poll-message sync poke (2026-08-17). The contract worth pinning is
    almost entirely about what it must NOT do: it must not post when unset, and
    it must not raise, ever — the announcements have already gone out by the
    time it runs, and a sync endpoint that is down must cost them nothing.
    """

    def _post(self, response=None, side_effect=None):
        calls = []

        def fake_post(url, **kwargs):
            calls.append((url, kwargs))
            if side_effect is not None:
                raise side_effect
            return response

        return fake_post, calls

    def test_unset_token_skips_entirely(self):
        fake_post, calls = self._post()
        with mock.patch.dict(os.environ, {"POLL_SYNC_TOKEN": ""}, clear=False), \
                mock.patch("requests.post", fake_post):
            self.assertIsNone(sync_poll_messages())
        self.assertEqual(calls, [])

    def test_posts_lane_and_bearer_token_to_the_default_endpoint(self):
        resp = mock.Mock(status_code=200)
        resp.json.return_value = {"ok": True, "posted": 1, "notes": []}
        fake_post, calls = self._post(response=resp)
        with mock.patch.dict(os.environ, {"POLL_SYNC_TOKEN": "s3cret"}, clear=False), \
                mock.patch.dict(os.environ, {"DISCORD_POLL_SYNC_URL": ""}, clear=False), \
                mock.patch("requests.post", fake_post):
            stats = sync_poll_messages(lane_suffix="")
        self.assertEqual(stats["posted"], 1)
        url, kwargs = calls[0]
        self.assertEqual(url, POLL_SYNC_URL_DEFAULT)
        self.assertEqual(kwargs["json"], {"lane": "prod"})
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer s3cret")

    def test_dev_lane_is_sent_as_dev(self):
        resp = mock.Mock(status_code=200)
        resp.json.return_value = {"ok": True}
        fake_post, calls = self._post(response=resp)
        with mock.patch.dict(os.environ, {"POLL_SYNC_TOKEN": "s3cret"}, clear=False), \
                mock.patch("requests.post", fake_post):
            sync_poll_messages(lane_suffix="_dev")
        self.assertEqual(calls[0][1]["json"], {"lane": "dev"})

    def test_a_refusal_is_reported_not_raised(self):
        resp = mock.Mock(status_code=503)
        resp.json.return_value = {"ok": False, "message": "not switched on yet"}
        fake_post, _ = self._post(response=resp)
        with mock.patch.dict(os.environ, {"POLL_SYNC_TOKEN": "s3cret"}, clear=False), \
                mock.patch("requests.post", fake_post):
            self.assertIsNone(sync_poll_messages())

    def test_a_dead_endpoint_never_raises(self):
        # ⚠️ THE contract: announcements have already been posted when this
        # runs. An unreachable Worker is a log line, never an exception that
        # turns a good run into a failed one.
        fake_post, _ = self._post(side_effect=RuntimeError("connection refused"))
        with mock.patch.dict(os.environ, {"POLL_SYNC_TOKEN": "s3cret"}, clear=False), \
                mock.patch("requests.post", fake_post):
            self.assertIsNone(sync_poll_messages())


class TestQuestionMessageSync(unittest.TestCase):
    """
    The GABI club-QUESTION sync poke (2026-08-18) — the sibling of the poll
    poke above. It shares `_poke_sync`, so the contract worth pinning here is
    what makes it a DIFFERENT call: its own endpoint, its own log label, and
    the fact that neither poke can break the other.
    """

    def _post(self, response=None, side_effect=None):
        calls = []

        def fake_post(url, **kwargs):
            calls.append((url, kwargs))
            if side_effect is not None:
                raise side_effect
            return response

        return fake_post, calls

    def test_unset_token_skips_entirely(self):
        fake_post, calls = self._post()
        with mock.patch.dict(os.environ, {"POLL_SYNC_TOKEN": ""}, clear=False), \
                mock.patch("requests.post", fake_post):
            self.assertIsNone(sync_question_messages())
        self.assertEqual(calls, [])

    def test_posts_lane_and_bearer_token_to_the_question_endpoint(self):
        resp = mock.Mock(status_code=200)
        resp.json.return_value = {"ok": True, "posted": 2, "baselined": 0, "notes": []}
        fake_post, calls = self._post(response=resp)
        with mock.patch.dict(os.environ, {"POLL_SYNC_TOKEN": "s3cret"}, clear=False), \
                mock.patch.dict(os.environ, {"DISCORD_QUESTION_SYNC_URL": ""}, clear=False), \
                mock.patch("requests.post", fake_post):
            stats = sync_question_messages(lane_suffix="")
        self.assertEqual(stats["posted"], 2)
        url, kwargs = calls[0]
        self.assertEqual(url, QUESTION_SYNC_URL_DEFAULT)
        self.assertEqual(kwargs["json"], {"lane": "prod"})
        # ⚠️ The SAME shared secret as the poll poke — one pipeline token, two
        # routes. A separate secret here would be a second thing to mint, to
        # rotate and to get out of step, for no extra containment.
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer s3cret")

    def test_it_is_a_different_endpoint_from_the_poll_sync(self):
        # The whole point of the second route: independent failure domains.
        # If these two URLs ever collide, a question sweep and a poll sweep are
        # the same request and the isolation claim is false.
        self.assertNotEqual(QUESTION_SYNC_URL_DEFAULT, POLL_SYNC_URL_DEFAULT)
        self.assertTrue(QUESTION_SYNC_URL_DEFAULT.endswith("/questions/sync"))

    def test_dev_lane_is_sent_as_dev(self):
        resp = mock.Mock(status_code=200)
        resp.json.return_value = {"ok": True}
        fake_post, calls = self._post(response=resp)
        with mock.patch.dict(os.environ, {"POLL_SYNC_TOKEN": "s3cret"}, clear=False), \
                mock.patch("requests.post", fake_post):
            sync_question_messages(lane_suffix="_dev")
        self.assertEqual(calls[0][1]["json"], {"lane": "dev"})

    def test_the_url_can_be_overridden_for_a_local_worker(self):
        resp = mock.Mock(status_code=200)
        resp.json.return_value = {"ok": True}
        fake_post, calls = self._post(response=resp)
        with mock.patch.dict(
            os.environ,
            {"POLL_SYNC_TOKEN": "s3cret",
             "DISCORD_QUESTION_SYNC_URL": "http://127.0.0.1:8797/questions/sync"},
            clear=False,
        ), mock.patch("requests.post", fake_post):
            sync_question_messages()
        self.assertEqual(calls[0][0], "http://127.0.0.1:8797/questions/sync")

    def test_a_refusal_is_reported_not_raised(self):
        resp = mock.Mock(status_code=503)
        resp.json.return_value = {"ok": False, "message": "not switched on yet"}
        fake_post, _ = self._post(response=resp)
        with mock.patch.dict(os.environ, {"POLL_SYNC_TOKEN": "s3cret"}, clear=False), \
                mock.patch("requests.post", fake_post):
            self.assertIsNone(sync_question_messages())

    def test_a_dead_endpoint_never_raises(self):
        fake_post, _ = self._post(side_effect=RuntimeError("connection refused"))
        with mock.patch.dict(os.environ, {"POLL_SYNC_TOKEN": "s3cret"}, clear=False), \
                mock.patch("requests.post", fake_post):
            self.assertIsNone(sync_question_messages())

    def test_a_dead_question_endpoint_cannot_break_the_poll_sync(self):
        # ⚠️ THE isolation claim, exercised rather than asserted. The question
        # poke blows up; the poll poke, called after it, still returns its
        # stats. Both orderings matter, so both are run.
        poll_resp = mock.Mock(status_code=200)
        poll_resp.json.return_value = {"ok": True, "posted": 1}

        def fake_post(url, **kwargs):
            if url == QUESTION_SYNC_URL_DEFAULT:
                raise RuntimeError("questions endpoint is down")
            return poll_resp

        with mock.patch.dict(os.environ, {"POLL_SYNC_TOKEN": "s3cret"}, clear=False), \
                mock.patch("requests.post", fake_post):
            self.assertIsNone(sync_question_messages())
            self.assertEqual(sync_poll_messages()["posted"], 1)

    def test_a_dead_poll_endpoint_cannot_break_the_question_sync(self):
        q_resp = mock.Mock(status_code=200)
        q_resp.json.return_value = {"ok": True, "posted": 3}

        def fake_post(url, **kwargs):
            if url == POLL_SYNC_URL_DEFAULT:
                raise RuntimeError("polls endpoint is down")
            return q_resp

        with mock.patch.dict(os.environ, {"POLL_SYNC_TOKEN": "s3cret"}, clear=False), \
                mock.patch("requests.post", fake_post):
            self.assertIsNone(sync_poll_messages())
            self.assertEqual(sync_question_messages()["posted"], 3)


if __name__ == "__main__":
    unittest.main()
