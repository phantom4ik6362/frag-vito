import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import app
import chat_statistics as stats


class StatisticsTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        db_patch = patch.object(app, "DB", Path(temp.name) / "queue.sqlite3")
        env_patch = patch.dict(os.environ, {"DRY_RUN": "false", "REPLY_DELAY_SECONDS": "0"}, clear=True)
        db_patch.start()
        env_patch.start()
        self.addCleanup(db_patch.stop)
        self.addCleanup(env_patch.stop)
        app.init_db(recover=False)
        self.now = datetime(2026, 11, 28, 12, tzinfo=stats.MOSCOW)
        self.index = 0

    def incoming(self, chat, status, *, text="Текст", created=None, action=None):
        self.index += 1
        created = created or self.now - timedelta(minutes=1)
        with patch.object(app.time, "time", return_value=created.timestamp()):
            with app.db() as conn:
                app.enqueue(conn, str(self.index), chat, text, status, action)
        return str(self.index)

    def test_chats_not_messages_and_human_after_bot_is_still_answered(self):
        for _ in range(15):
            self.incoming("manual", "skipped", action="operator")
            self.incoming("bot", "skipped", action="operator")
        self.incoming("bot", "sent", action="street_overview")
        report = stats.collect(app.db, "today", now=self.now)
        self.assertEqual((report.total, report.answered, report.handed_off, report.pending), (2, 1, 1, 0))
        self.assertEqual(report.total, report.answered + report.handed_off + report.pending)

    def test_cohort_uses_first_ever_record_and_moscow_midnight(self):
        midnight = self.now.replace(hour=0, minute=0)
        self.incoming("old", "sent", created=midnight - timedelta(days=40))
        self.incoming("old", "sent")
        self.incoming("yesterday", "skipped", created=midnight - timedelta(seconds=1))
        self.incoming("today", "pending", created=midnight)
        today = stats.collect(app.db, "today", now=self.now)
        yesterday = stats.collect(app.db, "yesterday", now=self.now)
        self.assertEqual((today.total, today.pending), (1, 1))
        self.assertEqual((yesterday.total, yesterday.handed_off), (1, 1))
        self.assertEqual(stats.collect(app.db, "all", now=self.now).total, 3)

    def test_seven_and_thirty_days_include_today(self):
        midnight = self.now.replace(hour=0, minute=0)
        for name, days, shift in (("six", 6, 0), ("seven", 7, -1), ("twentynine", 29, 0), ("thirty", 30, -1)):
            self.incoming(name, "sent", created=midnight - timedelta(days=days, seconds=-shift))
        self.assertEqual(stats.collect(app.db, "7d", now=self.now).total, 1)
        self.assertEqual(stats.collect(app.db, "30d", now=self.now).total, 3)

    def test_contact_counts_first_message_and_after_human_only_once(self):
        self.incoming("contact-first", "skipped", text="+7 (900) 000-00-01 Антон")
        self.incoming("contact-first", "skipped", text="@coffee_owner", action="operator")
        self.incoming("contact-later", "sent", action="street_overview")
        self.incoming("contact-later", "skipped", action="operator")
        self.incoming("contact-later", "skipped", text="мой никнейм CoffeeOwner")
        self.incoming("budget", "skipped", text="мой бюджет 9000000001 рублей")
        self.incoming("negative", "skipped", text="Не звоните на +7 (900) 000-00-01")
        report = stats.collect(app.db, "today", now=self.now)
        self.assertEqual((report.total, report.contacts, report.answered, report.handed_off), (4, 2, 1, 3))

    def test_contact_acknowledgement_recovers_nickname_without_at(self):
        self.incoming("nickname", "sent", text="CoffeeOwner88", action="contact_received")
        self.assertEqual(stats.collect(app.db, "today", now=self.now).contacts, 1)

    def test_split_number_is_not_invented_from_two_messages(self):
        self.assertFalse(stats.contact_supplied("900\n0000001"))
        self.assertFalse(stats.contact_supplied("900"))
        self.assertFalse(stats.contact_supplied("0000001"))

    def test_dry_run_is_excluded_even_when_media_or_merged(self):
        with patch.dict(os.environ, {"DRY_RUN": "true"}):
            self.incoming("preview", "media")
            self.incoming("preview", "merged")
            self.incoming("preview", "dry_run", text="@preview_only")
        self.assertEqual(stats.collect(app.db, "all", now=self.now).total, 0)

    def test_duplicate_webhook_and_restart_do_not_add_chat(self):
        ident = self.incoming("chat", "sent", text="@coffee_owner")
        with app.db() as conn:
            self.assertFalse(app.enqueue(conn, ident, "chat", "@coffee_owner", "pending"))
        app.init_db(recover=False)
        report = stats.collect(app.db, "all", now=self.now)
        self.assertEqual((report.total, report.contacts), (1, 1))

    def test_outcome_updates_after_a_pending_job_finishes(self):
        ident = self.incoming("chat", "pending")
        self.assertEqual(stats.collect(app.db, "today", now=self.now).pending, 1)
        with app.db() as conn:
            conn.execute("UPDATE jobs SET status='sent',template_id='clarify_intent' WHERE id=?", (ident,))
        report = stats.collect(app.db, "today", now=self.now)
        self.assertEqual((report.answered, report.pending), (1, 0))

    def test_resume_does_not_reset_cohort_and_retained_snapshot_survives_deletion(self):
        ident = self.incoming("chat", "sent")
        stats.collect(app.db, "all", now=self.now)
        with app.db() as conn:
            conn.execute("DELETE FROM chat_dialogue")
            conn.execute("DELETE FROM selector_turns")
            conn.execute("DELETE FROM jobs WHERE id=?", (ident,))
        app.init_db(recover=False)
        self.assertEqual(stats.collect(app.db, "today", now=self.now).answered, 1)

    def test_backfill_old_schema_and_repeat_migration(self):
        with app.db() as conn:
            conn.execute("DELETE FROM runtime_state WHERE key='statistics_backfilled_v1'")
            conn.execute("DROP TABLE statistics_events")
            conn.execute(
                """INSERT INTO jobs (id,chat_id,text,status,created,due,template_id)
                   VALUES ('historical','past','@coffee_owner','sent',?,0,'street_overview')""",
                ((self.now - timedelta(days=2)).timestamp(),),
            )
            conn.execute(
                """INSERT INTO jobs (id,chat_id,text,status,created,due)
                   VALUES ('preview','preview','@test_user','dry_run',?,0)""",
                (self.now.timestamp(),),
            )
        app.init_db(recover=False)
        app.init_db(recover=False)
        report = stats.collect(app.db, "7d", now=self.now)
        self.assertEqual((report.total, report.answered, report.contacts), (1, 1, 1))
        with app.db() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM statistics_events").fetchone()[0], 1)

    def test_format_is_plain_and_pending_is_not_a_handoff(self):
        self.incoming("pending", "pending")
        report = stats.collect(app.db, "today", now=self.now)
        text = stats.format_report(report)
        self.assertIn("Новых чатов — 1", text)
        self.assertIn("Передано администратору без ответа бота — 0", text)
        self.assertIn("Ожидают обработки — 1", text)
        self.assertIn("28.11.2026", text)
        self.assertTrue(all(ord(c) < 0x1F000 for c in text))
