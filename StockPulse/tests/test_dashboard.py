import sys
import unittest
from pathlib import Path

# Ensure project root is on path when tests run from the Project 1 folder.
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app import render_dashboard


class TestDashboardRender(unittest.TestCase):
    def test_render_dashboard_contains_summary_and_rows(self):
        html = render_dashboard(
            [
                {
                    "sku": "SKU-1",
                    "name": "Widget",
                    "category": "Gadgets",
                    "total_remaining": 8,
                    "safety_stock": 4,
                    "batch_count": 2,
                    "next_expiry": "2026-06-30",
                    "status": "healthy",
                }
            ]
        )

        self.assertIn("StockPulse Dashboard", html)
        self.assertIn("Products tracked", html)
        self.assertIn("Widget", html)
        self.assertIn("status-healthy", html)


if __name__ == "__main__":
    unittest.main()