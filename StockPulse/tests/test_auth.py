import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from email_validator import EmailNotValidError

# Ensure project root is on path when tests run from the Project 1 folder.
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app import authenticate_user, create_access_token, create_admin_user, create_pending_user, create_session, get_current_user, hash_password, login_for_access_token, normalize_email_address, record_product_scan, render_dashboard, render_landing_page, render_portal_page, render_supplier_dashboard, verify_password, verify_user_otp
from db import Base
from models import Batch, Product, ProductScan, Supplier, User
from schemas import AdminUserCreate, ScanCreate, UserCreate, UserLogin


class TestJwtAuth(unittest.TestCase):
    def setUp(self):
        self.tempdir = TemporaryDirectory()
        self.db_path = Path(self.tempdir.name) / "test.db"
        self.engine = create_engine(f"sqlite:///{self.db_path}", connect_args={"check_same_thread": False})
        Base.metadata.create_all(bind=self.engine)
        self.SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=self.engine)
        self.db = self.SessionLocal()

    def tearDown(self):
        self.db.close()
        self.engine.dispose()
        self.tempdir.cleanup()

    def test_password_hashing_roundtrip(self):
        hashed = hash_password("secret123")
        self.assertTrue(verify_password("secret123", hashed))
        self.assertFalse(verify_password("wrong", hashed))

    def test_register_login_and_me_flow(self):
        with patch("app.send_verification_email"):
            user, otp_code = create_pending_user(
                self.db,
                UserCreate(username="admin", email="admin@example.com", password="secret123"),
                send_email=False,
                check_deliverability=False,
            )

        self.assertEqual(user.email, "admin@example.com")
        self.assertFalse(bool(user.is_verified))

        verified_user = verify_user_otp(self.db, "admin@example.com", otp_code, check_deliverability=False)
        self.assertTrue(bool(verified_user.is_verified))

        login_token = login_for_access_token(UserLogin(identifier="admin", password="secret123"), db=self.db)
        self.assertEqual(login_token.token_type, "bearer")

        current_user = get_current_user(token=login_token.access_token, db=self.db)
        self.assertEqual(current_user.username, "admin")
        self.assertEqual(current_user.email, "admin@example.com")

    def test_authenticate_user_helper(self):
        user = User(username="ops", email="ops@example.com", hashed_password=hash_password("pw123"), role="user", is_active=1, is_verified=1)
        self.db.add(user)
        self.db.commit()

        self.assertIsNotNone(authenticate_user(self.db, "ops", "pw123"))
        self.assertIsNotNone(authenticate_user(self.db, "ops@example.com", "pw123"))
        self.assertIsNone(authenticate_user(self.db, "ops", "bad"))

    def test_create_access_token_contains_subject(self):
        user = User(username="admin", email="admin@example.com", hashed_password=hash_password("secret123"), role="admin", is_active=1, is_verified=1)
        self.db.add(user)
        self.db.commit()

        jti, _ = create_session(self.db, user)
        token = create_access_token({"sub": "admin", "jti": jti})
        current_user = get_current_user(token=token, db=self.db)
        self.assertEqual(current_user.username, "admin")

    def test_portal_pages_render(self):
        landing_html = render_landing_page()
        user_html = render_portal_page("user")
        admin_html = render_portal_page("admin")
        supplier_html = render_supplier_dashboard()

        self.assertIn("Choose your access level", landing_html)
        self.assertIn("/user", landing_html)
        self.assertIn("/admin", landing_html)
        self.assertIn("/supplier", landing_html)
        self.assertIn("Getting started", user_html)
        self.assertIn("Only the current step is shown", user_html)
        self.assertIn("I already have an account", user_html)
        self.assertIn('data-step="register"', user_html)
        self.assertIn('data-step="verify"', user_html)
        self.assertIn('data-step="login"', user_html)
        self.assertIn('data-step="dashboard"', user_html)
        self.assertIn("Register", user_html)
        self.assertNotIn("Register", admin_html)
        self.assertIn("Verify OTP", user_html)
        self.assertIn("Admin Page", admin_html)
        self.assertIn("/auth/verify-otp", admin_html)
        self.assertIn("Supplier Dashboard", supplier_html)
        self.assertIn("Provision account", admin_html)
        self.assertIn("Scan product", user_html)
        self.assertIn("Recent scans", user_html)

    def test_admin_account_creation(self):
        admin = User(username="root", email="root@example.com", hashed_password=hash_password("secret123"), role="admin", is_active=1, is_verified=1)
        self.db.add(admin)
        self.db.commit()

        supplier = Supplier(name="Acme Supplies", contact_email="sup1@example.com", lead_time_days=5)
        self.db.add(supplier)
        self.db.commit()
        self.db.refresh(supplier)

        created = create_admin_user(
            self.db,
            AdminUserCreate(username="sup1", email="sup1@example.com", password="secret123", role="supplier", supplier_id=supplier.id),
            check_deliverability=False,
        )

        self.assertEqual(created.role, "supplier")
        self.assertEqual(created.email, "sup1@example.com")

    def test_render_dashboard_escapes_content(self):
        html = render_dashboard([
            {
                "sku": "<script>alert(1)</script>",
                "name": "Widget & Co",
                "category": "<b>danger</b>",
                "total_remaining": 8,
                "safety_stock": 4,
                "batch_count": 2,
                "next_expiry": "2026-06-30",
                "status": "healthy",
            }
        ])

        self.assertNotIn("<script>", html)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", html)

    def test_scan_capture_persists_snapshot(self):
        user = User(username="scanner", email="scanner@example.com", hashed_password=hash_password("secret123"), role="user", is_active=1, is_verified=1)
        product = Product(sku="SKU-SCAN-1", name="Scan Item", category="Tools", safety_stock=3)
        self.db.add(user)
        self.db.add(product)
        self.db.commit()
        self.db.refresh(user)
        self.db.refresh(product)

        batch = Batch(product_id=product.id, batch_number="B-1", quantity_received=10, quantity_remaining=7)
        self.db.add(batch)
        self.db.commit()

        scan = record_product_scan(self.db, user, ScanCreate(scan_code="SKU-SCAN-1", quantity=2))

        stored = self.db.query(ProductScan).filter(ProductScan.id == scan.id).first()
        self.assertIsNotNone(stored)
        self.assertEqual(stored.product_sku, "SKU-SCAN-1")
        self.assertEqual(stored.quantity_remaining_snapshot, 7)
        self.assertEqual(stored.batch_count_snapshot, 1)
        self.assertEqual(stored.inventory_status_snapshot, "healthy")

    def test_email_validation_can_reject_bad_domains(self):
        with patch("app.validate_email", side_effect=EmailNotValidError("fake domain")):
            with self.assertRaises(HTTPException):
                normalize_email_address("person@fake-domain.test", check_deliverability=True)


if __name__ == "__main__":
    unittest.main()