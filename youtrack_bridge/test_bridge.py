import json
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from youtrack_bridge import bridge


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = bridge.connect(self.tmp.name + "/bridge.sqlite")
        self.event = {"event": "commentAdded", "id": "2-123", "numberInProject": 123,
                      "project": {"shortName": "EDG"},
                      "comments": [{"id": "4-9", "text": "Please review", "author": {"login": "human"}}]}

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def submit(self, event=None, token="a" * 64):
        return bridge.accept(self.db, json.dumps(event or self.event).encode(),
                             "a" * 64, token, "EDG", "review-bot")

    def test_auth_project_bot_and_duplicate(self):
        self.assertEqual(self.submit(token="wrong")[0], 401)
        self.assertEqual(self.db.execute("SELECT count(*) FROM jobs").fetchone()[0], 0)
        self.assertEqual(self.submit()[0], 202)
        self.assertEqual(self.submit(), (202, "duplicate"))
        self.assertEqual(self.db.execute("SELECT count(*) FROM jobs").fetchone()[0], 1)
        other = json.loads(json.dumps(self.event))
        other["project"]["shortName"] = "OTHER"
        self.assertEqual(self.submit(other)[0], 403)
        bot = json.loads(json.dumps(self.event))
        bot["comments"][0]["id"] = "4-10"
        bot["comments"][0]["author"]["login"] = "review-bot"
        self.assertEqual(self.submit(bot), (200, "ignored bot comment"))

    def test_worker_writes_once_and_retains_result_across_retry(self):
        self.submit()
        output = type("Process", (), {"stdout": "Reviewed; looks good"})()
        with patch.object(bridge.subprocess, "run", return_value=output) as runner, \
             patch.object(bridge, "has_comment", side_effect=[False, False, True]) as lookup, \
             patch.object(bridge, "post_comment", side_effect=[RuntimeError("temporary"), None]) as post:
            self.assertTrue(bridge.run_one(self.db, "https://example.youtrack.cloud", "api-key", ["agent"]))
            self.assertEqual(self.db.execute("SELECT status FROM jobs").fetchone()[0], "failed")
            self.db.execute("UPDATE jobs SET status='queued' WHERE status='failed'")
            self.assertTrue(bridge.run_one(self.db, "https://example.youtrack.cloud", "api-key", ["agent"]))
            self.assertEqual(self.db.execute("SELECT status FROM jobs").fetchone()[0], "done")
            self.assertEqual(runner.call_count, 1)
            self.assertEqual(post.call_count, 2)
            self.assertIn(bridge.marker("comment:2-123:4-9"), post.call_args.args[-1])
            self.assertEqual(lookup.call_count, 2)

    def test_one_running_job_per_issue(self):
        self.submit()
        second = json.loads(json.dumps(self.event))
        second["comments"][0]["id"] = "4-10"
        self.submit(second)
        first = bridge.claim(self.db)
        self.assertIsNotNone(first)
        self.assertIsNone(bridge.claim(self.db))
        self.db.execute("UPDATE jobs SET status='done' WHERE event_key=?", (first[0],))
        self.assertIsNotNone(bridge.claim(self.db))


if __name__ == "__main__":
    unittest.main()
