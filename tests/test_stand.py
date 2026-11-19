import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

import journal  # noqa: E402
import stand  # noqa: E402


class StandTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.app = os.path.join(self.dir, "app")
        os.makedirs(self.app)
        with open(os.path.join(self.app, "bot.py"), "w") as fh:
            fh.write("x = 1\n")
        self.versions = os.path.join(self.dir, "versions.csv")
        self.settings = {"max_spread_pct": 0.05, "webhook_secret": "geheim", "journal_token": "t",
                         "ib_password": "pw"}

    def tearDown(self):
        journal.set_stand("")

    def test_secrets_are_not_recorded(self):
        snap = stand.snapshot(self.settings)
        self.assertEqual(snap, {"max_spread_pct": 0.05})
        stand.register(self.versions, self.app, self.settings)
        self.assertNotIn("geheim", open(self.versions).read())

    def test_same_state_is_recorded_once(self):
        a = stand.register(self.versions, self.app, self.settings)
        b = stand.register(self.versions, self.app, self.settings)
        self.assertEqual(a, b)
        self.assertEqual(len(journal.read_rows(self.versions)), 1)

    def test_setting_change_makes_a_new_stand_and_says_what(self):
        a = stand.register(self.versions, self.app, self.settings)
        b = stand.register(self.versions, self.app, {**self.settings, "max_spread_pct": 0.15})
        self.assertNotEqual(a, b)
        rows = journal.read_rows(self.versions)
        self.assertEqual(rows[1]["settings_changes"], "max_spread_pct: 0.05 -> 0.15")
        self.assertEqual(rows[1]["code_changed"], "nein")

    def test_code_change_makes_a_new_stand(self):
        a = stand.register(self.versions, self.app, self.settings)
        with open(os.path.join(self.app, "bot.py"), "a") as fh:
            fh.write("y = 2\n")
        b = stand.register(self.versions, self.app, self.settings)
        self.assertNotEqual(a, b)
        self.assertEqual(journal.read_rows(self.versions)[1]["code_changed"], "ja")

    def test_rows_get_the_current_stand(self):
        path = os.path.join(self.dir, "s.csv")
        journal.set_stand("abc12345")
        journal.append_row(path, journal.TF_SIGNAL_FIELDS, {"symbol": "NVS"})
        journal.append_row(path, journal.TF_SIGNAL_FIELDS, {"symbol": "X", "stand": "mine"})
        rows = journal.read_rows(path)
        self.assertEqual([r["stand"] for r in rows], ["abc12345", "mine"])

    def test_stats_by_stand(self):
        rows = [{"stand": "a", "r_multiple": "1", "net_pnl_usd": "10"},
                {"stand": "a", "r_multiple": "-1", "net_pnl_usd": "-10"},
                {"stand": "", "r_multiple": "2", "net_pnl_usd": "20"}]
        s = journal.stats_by_stand(rows)
        self.assertEqual(s["a"]["trades"], 2)
        self.assertEqual(s["vorher"]["sum_r"], 2.0)


if __name__ == "__main__":
    unittest.main()
