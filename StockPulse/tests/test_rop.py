import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Ensure project root is on path when tests run from the Project 1 folder.
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from rop import compute_velocity, compute_rop


class TestRopMath(unittest.TestCase):
    def test_compute_velocity_empty(self):
        self.assertEqual(compute_velocity([], days_window=7), 0)

    def test_compute_velocity_some_sales(self):
        now = datetime.now(timezone.utc)
        sales = [(now - timedelta(days=1), 3), (now - timedelta(days=2), 4), (now - timedelta(days=10), 10)]
        v7 = compute_velocity(sales, days_window=7)
        self.assertAlmostEqual(v7, (3 + 4) / 7, places=9)

    def test_compute_rop(self):
        self.assertEqual(compute_rop(2.5, 5, 10), (2.5 * 5) + 10)


if __name__ == "__main__":
    unittest.main()
