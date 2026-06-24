import sys
import unittest
from datetime import date, timedelta
from pathlib import Path

# Ensure project root is on path when tests run from the Project 1 folder.
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app import batch_status, inventory_row, inventory_status
from models import Batch, Product


class TestInventoryHelpers(unittest.TestCase):
    def test_inventory_status(self):
        self.assertEqual(inventory_status(0, 5), "out_of_stock")
        self.assertEqual(inventory_status(3, 5), "low_stock")
        self.assertEqual(inventory_status(10, 5), "healthy")

    def test_batch_status(self):
        today = date.today()
        active_batch = Batch(quantity_remaining=5, expiry_date=today + timedelta(days=60))
        soon_batch = Batch(quantity_remaining=5, expiry_date=today + timedelta(days=10))
        expired_batch = Batch(quantity_remaining=5, expiry_date=today - timedelta(days=1))
        depleted_batch = Batch(quantity_remaining=0, expiry_date=today + timedelta(days=10))

        self.assertEqual(batch_status(active_batch), "active")
        self.assertEqual(batch_status(soon_batch), "expiring_soon")
        self.assertEqual(batch_status(expired_batch), "expired")
        self.assertEqual(batch_status(depleted_batch), "depleted")

    def test_inventory_row(self):
        product = Product(id=1, sku="SKU-1", name="Widget", category="Gadgets", safety_stock=4)
        batches = [
            Batch(quantity_received=10, quantity_remaining=6, expiry_date=None),
            Batch(quantity_received=5, quantity_remaining=2, expiry_date=date.today() + timedelta(days=7)),
        ]

        row = inventory_row(product, batches)

        self.assertEqual(row["product_id"], 1)
        self.assertEqual(row["total_received"], 15)
        self.assertEqual(row["total_remaining"], 8)
        self.assertEqual(row["batch_count"], 2)
        self.assertEqual(row["status"], "healthy")


if __name__ == "__main__":
    unittest.main()