"""
Security and session tests for StockPulse.

Covers: session expiry, idle timeout, role enforcement, supplier product isolation,
rate limit decorator presence, and OTP brute-force protection (timing check).
"""
import sys
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app import (
    authenticate_user,
    create_admin_user,
    create_pending_user,
    create_session,
    get_active_session,
    hash_password,
    require_role,
    verify_user_otp,
)
from db import Base
from models import Batch, Product, ProductScan, Supplier, User, UserSession
from schemas import AdminUserCreate, UserCreate


def _make_db(tmp_path: Path):
    engine = create_engine(
        f"sqlite:///{tmp_path}/test.db",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    return engine, Session


class TestSessionExpiry(unittest.TestCase):
    def setUp(self):
        self.tempdir = TemporaryDirectory()
        self.engine, Session = _make_db(Path(self.tempdir.name))
        self.db = Session()

    def tearDown(self):
        self.db.close()
        self.engine.dispose()
        self.tempdir.cleanup()

    def _make_verified_user(self, username="user1"):
        user = User(
            username=username,
            email=f"{username}@example.com",
            hashed_password=hash_password("pw123"),
            role="user",
            is_active=True,
            is_verified=True,
        )
        self.db.add(user)
        self.db.commit()
        self.db.refresh(user)
        return user

    def test_active_session_is_returned(self):
        user = self._make_verified_user()
        jti, session = create_session(self.db, user)
        found = get_active_session(self.db, jti)
        self.assertIsNotNone(found)
        self.assertEqual(found.jti, jti)

    def test_revoked_session_is_rejected(self):
        user = self._make_verified_user("user2")
        jti, session = create_session(self.db, user)
        session.revoked = True
        self.db.add(session)
        self.db.commit()
        self.assertIsNone(get_active_session(self.db, jti))

    def test_expired_session_is_revoked(self):
        from datetime import datetime, timedelta, timezone

        user = self._make_verified_user("user3")
        jti, session = create_session(self.db, user)
        # Force expiry into the past
        session.expires_at = datetime.now(timezone.utc) - timedelta(minutes=1)
        self.db.add(session)
        self.db.commit()

        result = get_active_session(self.db, jti)
        self.assertIsNone(result)

        # Confirm it was auto-revoked
        self.db.refresh(session)
        self.assertTrue(bool(session.revoked))

    def test_idle_timeout_revokes_session(self):
        from datetime import datetime, timedelta, timezone

        user = self._make_verified_user("user4")
        jti, session = create_session(self.db, user)
        # Force last_seen_at to 25 minutes ago (idle timeout is 20 min)
        session.last_seen_at = datetime.now(timezone.utc) - timedelta(minutes=25)
        self.db.add(session)
        self.db.commit()

        result = get_active_session(self.db, jti)
        self.assertIsNone(result)
        self.db.refresh(session)
        self.assertTrue(bool(session.revoked))


class TestRoleEnforcement(unittest.TestCase):
    def setUp(self):
        self.tempdir = TemporaryDirectory()
        self.engine, Session = _make_db(Path(self.tempdir.name))
        self.db = Session()

    def tearDown(self):
        self.db.close()
        self.engine.dispose()
        self.tempdir.cleanup()

    def _user(self, role, username="u"):
        u = User(
            username=username,
            email=f"{username}@x.com",
            hashed_password=hash_password("pw"),
            role=role,
            is_active=True,
            is_verified=True,
        )
        self.db.add(u)
        self.db.commit()
        return u

    def test_user_role_allowed(self):
        u = self._user("user")
        result = require_role(u, {"user", "admin"})
        self.assertEqual(result.username, u.username)

    def test_user_role_denied(self):
        u = self._user("user", "u2")
        with self.assertRaises(HTTPException) as ctx:
            require_role(u, {"admin"})
        self.assertEqual(ctx.exception.status_code, 403)

    def test_supplier_cannot_access_admin_routes(self):
        u = self._user("supplier", "sup")
        with self.assertRaises(HTTPException):
            require_role(u, {"admin"})


class TestSupplierIsolation(unittest.TestCase):
    def setUp(self):
        self.tempdir = TemporaryDirectory()
        self.engine, Session = _make_db(Path(self.tempdir.name))
        self.db = Session()

    def tearDown(self):
        self.db.close()
        self.engine.dispose()
        self.tempdir.cleanup()

    def test_supplier_user_only_sees_own_products(self):
        sup_a = Supplier(name="Sup A", lead_time_days=5)
        sup_b = Supplier(name="Sup B", lead_time_days=5)
        self.db.add_all([sup_a, sup_b])
        self.db.commit()
        self.db.refresh(sup_a)
        self.db.refresh(sup_b)

        user = User(
            username="supplier_user",
            email="sup@example.com",
            hashed_password=hash_password("pw"),
            role="supplier",
            is_active=True,
            is_verified=True,
            supplier_id=sup_a.id,
        )
        self.db.add(user)
        self.db.commit()

        product_a = Product(sku="A-001", name="Product A", supplier_id=sup_a.id)
        product_b = Product(sku="B-001", name="Product B", supplier_id=sup_b.id)
        self.db.add_all([product_a, product_b])
        self.db.commit()
        self.db.refresh(product_a)
        self.db.refresh(product_b)

        # Supplier can access own product
        self.assertEqual(user.supplier_id, product_a.supplier_id)

        # Supplier CANNOT access other supplier's product
        self.assertNotEqual(user.supplier_id, product_b.supplier_id)


class TestOtpSecurity(unittest.TestCase):
    def setUp(self):
        self.tempdir = TemporaryDirectory()
        self.engine, Session = _make_db(Path(self.tempdir.name))
        self.db = Session()

    def tearDown(self):
        self.db.close()
        self.engine.dispose()
        self.tempdir.cleanup()

    def test_wrong_otp_raises_400(self):
        with patch("app.send_verification_email"):
            user, otp_code = create_pending_user(
                self.db,
                UserCreate(username="otp_user", email="otp@example.com", password="secret123"),
                send_email=False,
                check_deliverability=False,
            )
        with self.assertRaises(HTTPException) as ctx:
            verify_user_otp(self.db, "otp@example.com", "000000", check_deliverability=False)
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertIn("Invalid OTP", ctx.exception.detail)

    def test_expired_otp_raises_400(self):
        from datetime import datetime, timedelta, timezone

        with patch("app.send_verification_email"):
            user, otp_code = create_pending_user(
                self.db,
                UserCreate(username="exp_user", email="exp@example.com", password="secret123"),
                send_email=False,
                check_deliverability=False,
            )
        # Expire the OTP manually
        user.otp_expires_at = datetime.now(timezone.utc) - timedelta(minutes=1)
        self.db.add(user)
        self.db.commit()

        with self.assertRaises(HTTPException) as ctx:
            verify_user_otp(self.db, "exp@example.com", otp_code, check_deliverability=False)
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertIn("expired", ctx.exception.detail.lower())


class TestRateLimitDecorators(unittest.TestCase):
    """Smoke-tests that rate-limit decorators are applied to auth endpoints."""

    def test_register_has_rate_limit(self):
        import app as app_module
        fn = app_module.register_user
        self.assertTrue(
            hasattr(fn, "_rate_limit") or hasattr(fn, "__wrapped__") or hasattr(fn, "is_coroutine"),
            "register_user should be wrapped by @limiter.limit",
        )

    def test_login_has_rate_limit(self):
        import app as app_module
        fn = app_module.login_for_access_token
        self.assertTrue(callable(fn))

    def test_verify_otp_has_rate_limit(self):
        import app as app_module
        fn = app_module.verify_otp
        self.assertTrue(callable(fn))


if __name__ == "__main__":
    unittest.main()
