from sqlalchemy import Column, Integer, String, DateTime, Date, Float, ForeignKey
from sqlalchemy.orm import relationship
from datetime import datetime, timezone
from db import Base


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class Supplier(Base):
    __tablename__ = "suppliers"
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, nullable=False)
    contact_email = Column(String, nullable=True)
    lead_time_days = Column(Integer, default=7)


class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True, index=True)
    username = Column(String, unique=True, index=True, nullable=False)
    email = Column(String, unique=True, index=True, nullable=False)
    hashed_password = Column(String, nullable=False)
    role = Column(String, default="user")
    is_active = Column(Integer, default=1)
    is_verified = Column(Integer, default=0)
    supplier_id = Column(Integer, ForeignKey("suppliers.id"), nullable=True)
    otp_code_hash = Column(String, nullable=True)
    otp_expires_at = Column(DateTime(timezone=True), nullable=True)
    supplier = relationship("Supplier")


class Product(Base):
    __tablename__ = "products"
    id = Column(Integer, primary_key=True, index=True)
    sku = Column(String, unique=True, index=True, nullable=False)
    name = Column(String, nullable=False)
    category = Column(String, nullable=True)
    safety_stock = Column(Integer, default=0)
    supplier_id = Column(Integer, ForeignKey("suppliers.id"), nullable=True)
    supplier = relationship("Supplier")


class Batch(Base):
    __tablename__ = "batches"
    id = Column(Integer, primary_key=True, index=True)
    product_id = Column(Integer, ForeignKey("products.id"), nullable=False)
    batch_number = Column(String, nullable=False)
    quantity_received = Column(Integer, nullable=False)
    quantity_remaining = Column(Integer, nullable=False)
    expiry_date = Column(Date, nullable=True)
    created_at = Column(DateTime(timezone=True), default=_utc_now)
    product = relationship("Product")


class Sale(Base):
    __tablename__ = "sales"
    id = Column(Integer, primary_key=True, index=True)
    product_id = Column(Integer, ForeignKey("products.id"), nullable=False)
    quantity = Column(Integer, nullable=False)
    timestamp = Column(DateTime(timezone=True), default=_utc_now)
    product = relationship("Product")


class ProductScan(Base):
    __tablename__ = "product_scans"
    id = Column(Integer, primary_key=True, index=True)
    scan_code = Column(String, index=True, nullable=False)
    product_id = Column(Integer, ForeignKey("products.id"), nullable=False)
    scanned_by_user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    quantity = Column(Integer, default=1)
    product_sku = Column(String, nullable=False)
    product_name = Column(String, nullable=False)
    product_category = Column(String, nullable=True)
    supplier_id = Column(Integer, ForeignKey("suppliers.id"), nullable=True)
    quantity_remaining_snapshot = Column(Integer, nullable=False)
    batch_count_snapshot = Column(Integer, nullable=False)
    inventory_status_snapshot = Column(String, nullable=False)
    captured_at = Column(DateTime(timezone=True), default=_utc_now)
    product = relationship("Product")
    scanned_by = relationship("User")


class UserSession(Base):
    __tablename__ = "user_sessions"
    id = Column(Integer, primary_key=True, index=True)
    jti = Column(String, unique=True, index=True, nullable=False)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    created_at = Column(DateTime(timezone=True), default=_utc_now)
    last_seen_at = Column(DateTime(timezone=True), default=_utc_now)
    expires_at = Column(DateTime(timezone=True), nullable=False)
    revoked = Column(Integer, default=0)
    user = relationship("User")
