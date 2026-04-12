import unittest
from tests.conftest_helpers import setup_temp_db, teardown_temp_db


class TestSmoke(unittest.TestCase):
    def setUp(self):
        self.db_path = setup_temp_db()

    def tearDown(self):
        teardown_temp_db(self.db_path)

    def test_db_initialized(self):
        from database import db
        with db.get_connection() as conn:
            tables = [r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )]
        self.assertIn("copy_trades", tables)
        self.assertIn("trade_scores", tables)
        self.assertIn("trader_lifecycle", tables)


if __name__ == "__main__":
    unittest.main()
