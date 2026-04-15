"""TDD for Scenario D Phase γ.6 — dry-run promotion computation.

`compute_dry_run()` is the business logic behind the `/api/upgrade/promotion-dryrun`
dashboard endpoint. It returns a single dict containing:

- the currently-active threshold values
- the cooldown state
- the circuit-breaker state
- one entry per observing/promoted candidate with their stats + verdict

Read-only. Zero side effects. Used for "which candidates WOULD pass
the gate if the flag were flipped right now" visibility during the
weeks we spend tuning thresholds before flipping AUTO_DISCOVERY_AUTO_PROMOTE.
"""
import unittest

from tests.conftest_helpers import setup_temp_db, teardown_temp_db


def _ms_ago(days: float) -> str:
    from datetime import datetime, timedelta
    return (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")


class TestComputeDryRun(unittest.TestCase):
    def setUp(self):
        self.path = setup_temp_db()
        from database import db
        self.db = db

    def tearDown(self):
        teardown_temp_db(self.path)

    def _seed_candidate(self, address, username, n_wins_losses, age_days=1):
        """Seed an observing candidate with synthetic paper_trades."""
        n_wins, n_losses = n_wins_losses
        with self.db.get_connection() as conn:
            conn.execute(
                "INSERT INTO trader_candidates (address, username, status) "
                "VALUES (?, ?, 'observing')",
                (address, username),
            )
            for i in range(n_wins):
                conn.execute(
                    "INSERT INTO paper_trades "
                    "(candidate_address, condition_id, market_question, side, "
                    " entry_price, status, pnl, created_at, signature) "
                    "VALUES (?, ?, 'Q', 'YES', 0.55, 'closed', 0.10, ?, ?)",
                    (address, "cid_%s_w%d" % (username, i),
                     _ms_ago(age_days), "sig_%s_w%d" % (username, i)),
                )
            for i in range(n_losses):
                conn.execute(
                    "INSERT INTO paper_trades "
                    "(candidate_address, condition_id, market_question, side, "
                    " entry_price, status, pnl, created_at, signature) "
                    "VALUES (?, ?, 'Q', 'YES', 0.55, 'closed', -0.05, ?, ?)",
                    (address, "cid_%s_l%d" % (username, i),
                     _ms_ago(age_days), "sig_%s_l%d" % (username, i)),
                )

    def test_empty_db_returns_empty_candidates_list(self):
        from bot.promotion import compute_dry_run
        result = compute_dry_run()
        self.assertIn("candidates", result)
        self.assertEqual(result["candidates"], [])
        self.assertIn("thresholds", result)
        self.assertIn("cooldown_active", result)
        self.assertIn("circuit_breaker_halted", result)
        self.assertFalse(result["cooldown_active"])
        self.assertFalse(result["circuit_breaker_halted"])

    def test_thresholds_populated_from_config(self):
        from bot.promotion import compute_dry_run
        import config
        result = compute_dry_run()
        t = result["thresholds"]
        self.assertEqual(t["min_trades"], config.PROMOTE_MIN_PAPER_TRADES)
        self.assertEqual(t["min_wr"], config.PROMOTE_MIN_OBSERVED_WR)
        self.assertEqual(t["min_wilson_lower"], config.PROMOTE_MIN_WILSON_LOWER)

    def test_failing_candidate_has_would_promote_false_and_reason(self):
        """Candidate with 10 trades, 5 wins — way below min_trades=100."""
        self._seed_candidate("0xfail", "failer", (5, 5))

        from bot.promotion import compute_dry_run
        result = compute_dry_run()
        self.assertEqual(len(result["candidates"]), 1)
        c = result["candidates"][0]
        self.assertEqual(c["username"], "failer")
        self.assertFalse(c["would_promote"])
        self.assertTrue(c["rejection_reason"].startswith("insufficient_trades"),
                        "expected insufficient_trades, got: %s" % c["rejection_reason"])
        self.assertEqual(c["n_trades"], 10)
        self.assertEqual(c["wins"], 5)

    def test_passing_candidate_has_would_promote_true(self):
        """Candidate with 150 trades, 105 wins (70% WR), pnl=$8.25 — clean pass."""
        # 105 wins * $0.10 = $10.50, 45 losses * -$0.05 = -$2.25 → net $8.25
        self._seed_candidate("0xpass", "passer", (105, 45))

        from bot.promotion import compute_dry_run
        result = compute_dry_run()
        # Find the passer entry
        c = next(x for x in result["candidates"] if x["username"] == "passer")
        self.assertTrue(c["would_promote"],
                        "70%% WR at n=150 must pass, reason=%s" % c["rejection_reason"])
        self.assertEqual(c["rejection_reason"], "ok")
        self.assertEqual(c["n_trades"], 150)
        self.assertEqual(c["wins"], 105)

    def test_each_candidate_has_stats_fields(self):
        self._seed_candidate("0xs", "stater", (5, 5))

        from bot.promotion import compute_dry_run
        result = compute_dry_run()
        c = result["candidates"][0]
        for field in ("address", "username", "status", "n_trades", "wins",
                      "total_pnl", "winrate", "wilson_lower_bound",
                      "newest_trade_age_days", "would_promote", "rejection_reason"):
            self.assertIn(field, c, "missing field: %s" % field)

    def test_cooldown_reflected_at_top_level(self):
        with self.db.get_connection() as conn:
            conn.execute(
                "INSERT INTO activity_log (event_type, icon, title, detail, pnl, created_at) "
                "VALUES ('promotion', '', 'prev', '', 0, ?)",
                (_ms_ago(2),),
            )

        from bot.promotion import compute_dry_run
        result = compute_dry_run()
        self.assertTrue(result["cooldown_active"])

    def test_circuit_breaker_reflected_at_top_level(self):
        with self.db.get_connection() as conn:
            conn.execute(
                "INSERT INTO wallets (address, username, followed) VALUES ('0xhlt','halter',1)"
            )
            conn.execute(
                "INSERT INTO trader_candidates (address, username, status, auto_promoted_at) "
                "VALUES ('0xhlt', 'halter', 'promoted', ?)", (_ms_ago(2),),
            )
        from tests.conftest_helpers import insert_copy_trade
        insert_copy_trade(
            self.db, wallet_address="0xhlt", wallet_username="halter",
            pnl_realized=-15.0, condition_id="cid_hlt_1", status="closed",
        )
        with self.db.get_connection() as conn:
            conn.execute(
                "UPDATE copy_trades SET closed_at = ? WHERE condition_id='cid_hlt_1'",
                (_ms_ago(1),),
            )

        from bot.promotion import compute_dry_run
        result = compute_dry_run()
        self.assertTrue(result["circuit_breaker_halted"])


class TestPromoteStatsCutoff(unittest.TestCase):
    """Scenario D Phase E.1 — PROMOTE_STATS_CUTOFF env filter.

    Non-destructive filter that excludes pre-cutoff paper_trades rows
    from the promotion gate queries. Rows stay in the table (audit
    trail), but `compute_dry_run`, `get_candidate_stats`, and
    `check_promotions` ignore anything with `closed_at < cutoff`.
    Default (empty cutoff) preserves the pre-E.1 behavior exactly.
    """

    def setUp(self):
        self.path = setup_temp_db()
        from database import db
        self.db = db
        import config
        self._saved_cutoff = getattr(config, "PROMOTE_STATS_CUTOFF", "")

    def tearDown(self):
        import config
        config.PROMOTE_STATS_CUTOFF = self._saved_cutoff
        teardown_temp_db(self.path)

    def _seed_row(self, addr: str, username: str, closed_days_ago: float,
                  pnl: float = 0.10, cid_suffix: str = ""):
        """Insert one closed paper_trade with explicit closed_at."""
        cid = "cid_%s_%s_%s" % (username, closed_days_ago, cid_suffix)
        with self.db.get_connection() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO trader_candidates (address, username, status) "
                "VALUES (?, ?, 'observing')",
                (addr, username),
            )
            conn.execute(
                "INSERT INTO paper_trades "
                "(candidate_address, condition_id, market_question, side, "
                " entry_price, status, pnl, created_at, closed_at, signature) "
                "VALUES (?, ?, 'Q', 'YES', 0.55, 'closed', ?, ?, ?, ?)",
                (addr, cid, pnl, _ms_ago(closed_days_ago),
                 _ms_ago(closed_days_ago), "sig_" + cid),
            )

    def test_empty_cutoff_means_no_filter_backward_compat(self):
        import config
        config.PROMOTE_STATS_CUTOFF = ""  # default / filter off
        # Seed 15 closed rows at various ages
        for i in range(10):
            self._seed_row("0xa", "alice", closed_days_ago=10, cid_suffix="old%d" % i)
        for i in range(5):
            self._seed_row("0xa", "alice", closed_days_ago=1, cid_suffix="new%d" % i)

        from bot.promotion import compute_dry_run
        result = compute_dry_run()
        alice = next(c for c in result["candidates"] if c["username"] == "alice")
        self.assertEqual(alice["n_trades"], 15,
                         "empty cutoff must count all rows (backward compat)")

    def test_cutoff_excludes_pre_cutoff_rows_in_compute_dry_run(self):
        import config
        # Set cutoff to 5 days ago
        from datetime import datetime, timedelta
        cutoff = (datetime.now() - timedelta(days=5)).strftime("%Y-%m-%d %H:%M:%S")
        config.PROMOTE_STATS_CUTOFF = cutoff

        # Seed: 10 rows at 10 days old (pre-cutoff), 5 rows at 1 day old (post-cutoff)
        for i in range(10):
            self._seed_row("0xa", "alice", closed_days_ago=10, cid_suffix="old%d" % i)
        for i in range(5):
            self._seed_row("0xa", "alice", closed_days_ago=1, cid_suffix="new%d" % i)

        from bot.promotion import compute_dry_run
        result = compute_dry_run()
        alice = next(c for c in result["candidates"] if c["username"] == "alice")
        self.assertEqual(alice["n_trades"], 5,
                         "cutoff must exclude the 10 pre-cutoff rows, got n=%d" % alice["n_trades"])

    def test_cutoff_also_applies_to_get_candidate_stats(self):
        """`get_candidate_stats` is the production path used by
        `check_promotions`. It must honor the same filter so the gate
        and the dry-run endpoint agree on counts."""
        import config
        from datetime import datetime, timedelta
        cutoff = (datetime.now() - timedelta(days=5)).strftime("%Y-%m-%d %H:%M:%S")
        config.PROMOTE_STATS_CUTOFF = cutoff

        for i in range(10):
            self._seed_row("0xa", "alice", closed_days_ago=10, cid_suffix="old%d" % i)
        for i in range(5):
            self._seed_row("0xa", "alice", closed_days_ago=1, cid_suffix="new%d" % i)

        stats = self.db.get_candidate_stats("0xa")
        self.assertEqual(stats["total"], 5,
                         "get_candidate_stats must honor PROMOTE_STATS_CUTOFF, got %d" % stats["total"])

        # Now also check compute_dry_run returns the same count — parity
        from bot.promotion import compute_dry_run
        result = compute_dry_run()
        alice = next(c for c in result["candidates"] if c["username"] == "alice")
        self.assertEqual(alice["n_trades"], stats["total"],
                         "dry_run and get_candidate_stats must agree: %d vs %d"
                         % (alice["n_trades"], stats["total"]))


if __name__ == "__main__":
    unittest.main()
