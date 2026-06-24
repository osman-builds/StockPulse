from pydantic import BaseModel
from typing import Optional
from datetime import date


class SupplierCreate(BaseModel):
    name: str
    contact_email: Optional[str] = None
    lead_time_days: int = 7


class ProductCreate(BaseModel):
    sku: str
    name: str
    category: Optional[str] = None
    safety_stock: int = 0
    supplier_id: Optional[int] = None


class BatchCreate(BaseModel):
    product_id: int
    batch_number: str
    quantity_received: int
    expiry_date: Optional[date] = None


class SaleCreate(BaseModel):
    product_id: int
    quantity: int


class UserCreate(BaseModel):
    username: str
    email: str
    password: str
    supplier_id: Optional[int] = None


class UserLogin(BaseModel):
    identifier: str
    password: str


class Token(BaseModel):
    access_token: str
    token_type: str = "bearer"


class OtpVerifyRequest(BaseModel):
    email: str
    otp_code: str


class AuthMessage(BaseModel):
    message: str


class AdminUserCreate(BaseModel):
    username: str
    email: str
    password: str
    role: str
    supplier_id: Optional[int] = None


class ScanCreate(BaseModel):
    scan_code: str
    quantity: int = 1


class ScanCaptureResponse(BaseModel):
    message: str
    product_id: int
    product_sku: str
    quantity_remaining_snapshot: int


class ScanPreviewResponse(BaseModel):
    product_id: int
    sku: str
    name: str
    category: Optional[str] = None
    safety_stock: int = 0
    supplier_id: Optional[int] = None
    quantity_remaining_snapshot: int
    batch_count_snapshot: int
    inventory_status_snapshot: str
