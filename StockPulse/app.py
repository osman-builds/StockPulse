"""
StockPulse — FastAPI application entry point.

Architecture:
  - Auth helpers, session management, and route handlers live here.
  - HTML rendering delegates to presentation.py → templates/
  - Cache and inventory queries delegate to repositories/inventory_repository.py
  - Email delivery delegates to services/email_service.py
"""
from datetime import date, datetime, timedelta, timezone
import json
import os
import secrets
import warnings
from pathlib import Path

from fastapi import BackgroundTasks, FastAPI, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.security import OAuth2PasswordBearer
from email_validator import EmailNotValidError, validate_email
from jose import JWTError, jwt
from sqlalchemy.orm import Session
from passlib.context import CryptContext
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded

from db import SessionLocal, init_db
from models import Supplier, Product, Batch, Sale, User, UserSession, ProductScan
from schemas import (
    SupplierCreate, ProductCreate, BatchCreate, SaleCreate,
    UserCreate, UserLogin, Token, OtpVerifyRequest, AuthMessage,
    AdminUserCreate, ScanCreate, ScanCaptureResponse, ScanPreviewResponse,
)
from rop import compute_velocity, compute_rop

# ---------------------------------------------------------------------------
# App & rate limiter setup
# ---------------------------------------------------------------------------
app = FastAPI(title="StockPulse")
BASE_DIR = Path(__file__).resolve().parent
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")

limiter = Limiter(key_func=get_remote_address)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

# ---------------------------------------------------------------------------
# Configuration — JWT secret guard
# ---------------------------------------------------------------------------
_secret_key_raw = os.getenv("STOCKPULSE_SECRET_KEY", "")
if not _secret_key_raw:
    _debug = os.getenv("STOCKPULSE_DEBUG", "1").lower() in ("1", "true", "yes")
    if _debug:
        _secret_key_raw = "stockpulse-dev-secret"
        warnings.warn(
            "STOCKPULSE_SECRET_KEY is not set; using an insecure development default. "
            "Set STOCKPULSE_SECRET_KEY in production.",
            stacklevel=1,
        )
    else:
        raise RuntimeError(
            "STOCKPULSE_SECRET_KEY env var must be set before starting in production. "
            "Set STOCKPULSE_DEBUG=1 to allow the insecure dev fallback."
        )

SECRET_KEY = _secret_key_raw
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 60
SESSION_IDLE_TIMEOUT_MINUTES = 20
OTP_EXPIRE_MINUTES = 10
ADMIN_REGISTRATION_CODE = os.getenv("STOCKPULSE_ADMIN_CODE", "stockpulse-admin-dev")
SUPPLIER_REGISTRATION_CODE = os.getenv("STOCKPULSE_SUPPLIER_CODE", "")
SMTP_HOST = os.getenv("STOCKPULSE_SMTP_HOST", "")
SMTP_PORT = int(os.getenv("STOCKPULSE_SMTP_PORT", "587"))
SMTP_USERNAME = os.getenv("STOCKPULSE_SMTP_USERNAME", "")
SMTP_PASSWORD = os.getenv("STOCKPULSE_SMTP_PASSWORD", "")
SMTP_FROM = os.getenv("STOCKPULSE_SMTP_FROM", SMTP_USERNAME)
SMTP_USE_TLS = os.getenv("STOCKPULSE_SMTP_USE_TLS", "true").lower() == "true"
CACHE_TTL_SECONDS = int(os.getenv("STOCKPULSE_CACHE_TTL_SECONDS", "30"))
REDIS_CACHE_URL = os.getenv("STOCKPULSE_REDIS_URL", "redis://redis:6379/0")
CACHE_VERSION_KEY = "stockpulse:cache:version"
CACHE_PREFIX = "stockpulse"

pwd_context = CryptContext(schemes=["pbkdf2_sha256"], deprecated="auto")
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/token")


# ---------------------------------------------------------------------------
# Security headers middleware
# ---------------------------------------------------------------------------
@app.middleware("http")
async def security_headers(request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Cache-Control"] = "no-store"
    response.headers["Permissions-Policy"] = "camera=(self), microphone=(), geolocation=()"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        "img-src 'self' data:; "
        "style-src 'self' 'unsafe-inline'; "
        "script-src 'self' 'unsafe-inline'; "
        "connect-src 'self'; "
        "frame-ancestors 'none'"
    )
    return response


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@app.on_event("startup")
def startup():
    init_db()


# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------
def hash_password(password: str) -> str:
    return pwd_context.hash(password)


def verify_password(plain_password: str, hashed_password: str) -> bool:
    return pwd_context.verify(plain_password, hashed_password)


def get_user_by_username(db: Session, username: str) -> User | None:
    return db.query(User).filter(User.username == username).first()


def get_user_by_email(db: Session, email: str) -> User | None:
    return db.query(User).filter(User.email == email).first()


def get_user_by_identifier(db: Session, identifier: str) -> User | None:
    return get_user_by_username(db, identifier) or get_user_by_email(db, identifier)


def generate_jti() -> str:
    return secrets.token_urlsafe(24)


def normalize_email_address(email: str, check_deliverability: bool = True) -> str:
    try:
        validated = validate_email(email, check_deliverability=check_deliverability)
    except EmailNotValidError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return validated.normalized


def generate_otp_code() -> str:
    return f"{secrets.randbelow(1_000_000):06d}"


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def issue_otp_for_user(db: Session, user: User) -> str:
    otp_code = generate_otp_code()
    user.otp_code_hash = hash_password(otp_code)
    user.otp_expires_at = utc_now() + timedelta(minutes=OTP_EXPIRE_MINUTES)
    db.add(user)
    db.commit()
    db.refresh(user)
    return otp_code


def create_pending_user(
    db: Session,
    payload: UserCreate,
    send_email: bool = True,
    check_deliverability: bool = True,
) -> tuple[User, str]:
    email = normalize_email_address(payload.email, check_deliverability=check_deliverability)

    if get_user_by_username(db, payload.username):
        raise HTTPException(status_code=400, detail="Username already exists")
    if get_user_by_email(db, email):
        raise HTTPException(status_code=400, detail="Email already exists")

    user = User(
        username=payload.username,
        email=email,
        hashed_password=hash_password(payload.password),
        role="user",
        is_active=True,
        is_verified=False,
        supplier_id=None,
    )
    db.add(user)
    db.commit()
    db.refresh(user)

    otp_code = issue_otp_for_user(db, user)
    if send_email:
        send_verification_email(email, otp_code)
    return user, otp_code


def create_admin_user(
    db: Session,
    payload: AdminUserCreate,
    check_deliverability: bool = True,
) -> User:
    email = normalize_email_address(payload.email, check_deliverability=check_deliverability)
    if get_user_by_username(db, payload.username):
        raise HTTPException(status_code=400, detail="Username already exists")
    if get_user_by_email(db, email):
        raise HTTPException(status_code=400, detail="Email already exists")
    if payload.role not in {"admin", "supplier"}:
        raise HTTPException(status_code=400, detail="Invalid role")
    if payload.role == "supplier" and payload.supplier_id is None:
        raise HTTPException(status_code=400, detail="supplier_id is required for supplier accounts")

    user = User(
        username=payload.username,
        email=email,
        hashed_password=hash_password(payload.password),
        role=payload.role,
        is_active=True,
        is_verified=True,
        supplier_id=payload.supplier_id,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def verify_user_otp(
    db: Session,
    email: str,
    otp_code: str,
    check_deliverability: bool = True,
) -> User:
    normalized_email = normalize_email_address(email, check_deliverability=check_deliverability)
    user = get_user_by_email(db, normalized_email)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    if not user.otp_code_hash or not user.otp_expires_at:
        raise HTTPException(status_code=400, detail="OTP not requested")
    otp_expires_at = as_utc(user.otp_expires_at)
    if otp_expires_at is None or otp_expires_at < utc_now():
        raise HTTPException(status_code=400, detail="OTP expired")
    if not verify_password(otp_code, user.otp_code_hash):
        raise HTTPException(status_code=400, detail="Invalid OTP code")

    user.is_verified = True
    user.otp_code_hash = None
    user.otp_expires_at = None
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def create_session(db: Session, user: User) -> tuple[str, UserSession]:
    jti = generate_jti()
    expires_at = utc_now() + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    session = UserSession(
        jti=jti,
        user_id=user.id,
        expires_at=expires_at,
        last_seen_at=utc_now(),
        revoked=False,
    )
    db.add(session)
    db.commit()
    db.refresh(session)
    return jti, session


def update_session_activity(db: Session, session: UserSession):
    session.last_seen_at = utc_now()
    db.add(session)
    db.commit()


def get_active_session(db: Session, jti: str) -> UserSession | None:
    session = db.query(UserSession).filter(UserSession.jti == jti).first()
    if not session or session.revoked:
        return None
    if as_utc(session.expires_at) is None or as_utc(session.expires_at) < utc_now():
        session.revoked = True
        db.add(session)
        db.commit()
        return None
    last_seen = as_utc(session.last_seen_at)
    if last_seen is None or last_seen + timedelta(minutes=SESSION_IDLE_TIMEOUT_MINUTES) < utc_now():
        session.revoked = True
        db.add(session)
        db.commit()
        return None
    return session


def require_admin(current_user: User) -> User:
    if current_user.role != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")
    return current_user


def authenticate_user(db: Session, username: str, password: str) -> User | None:
    user = get_user_by_identifier(db, username)
    if not user or not verify_password(password, user.hashed_password):
        return None
    if not user.is_verified:
        return None
    return user


def create_access_token(data: dict, expires_delta: timedelta | None = None) -> str:
    payload = data.copy()
    expire = utc_now() + (expires_delta or timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES))
    payload.update({"exp": int(expire.timestamp())})
    return jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)


def create_user_token(db: Session, user: User) -> Token:
    jti, _ = create_session(db, user)
    access_token = create_access_token({"sub": user.username, "role": user.role, "jti": jti})
    return Token(access_token=access_token)


def get_current_user(
    token: str = Depends(oauth2_scheme),
    db: Session = Depends(get_db),
) -> User:
    credentials_error = HTTPException(
        status_code=401,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        username = payload.get("sub")
        jti = payload.get("jti")
        if not username:
            raise credentials_error
    except JWTError as exc:
        raise credentials_error from exc

    user = get_user_by_username(db, username)
    if not user:
        raise credentials_error
    if not user.is_verified:
        raise HTTPException(status_code=403, detail="Email not verified")
    if not jti:
        raise credentials_error
    session = get_active_session(db, jti)
    if not session or session.user_id != user.id:
        raise credentials_error
    update_session_activity(db, session)
    return user


def require_role(current_user: User, allowed_roles: set[str]) -> User:
    if current_user.role not in allowed_roles:
        raise HTTPException(status_code=403, detail="Insufficient access rights")
    return current_user


# ---------------------------------------------------------------------------
# Presentation layer — delegates to presentation.py → templates/
# ---------------------------------------------------------------------------
def render_dashboard(items: list[dict]) -> str:
    from presentation import render_dashboard as _impl
    return _impl(items)


def render_landing_page() -> str:
    from presentation import render_landing_page as _impl
    return _impl()


def render_portal_page(page_role: str) -> str:
    from presentation import render_portal_page as _impl
    return _impl(page_role)


def render_supplier_dashboard() -> str:
    from presentation import render_supplier_dashboard as _impl
    return _impl()


def render_quality_dashboard(items: list[dict]) -> str:
    from presentation import render_quality_dashboard as _impl
    return _impl(items)


# ---------------------------------------------------------------------------
# Cache & inventory layer — delegates to repositories/inventory_repository.py
# ---------------------------------------------------------------------------
def cache_get_json(key: str):
    from repositories.inventory_repository import cache_get_json as _impl
    return _impl(key)


def cache_set_json(key: str, value, ttl_seconds: int = CACHE_TTL_SECONDS):
    from repositories.inventory_repository import cache_set_json as _impl
    return _impl(key, value, ttl_seconds=ttl_seconds)


def cache_version() -> str:
    from repositories.inventory_repository import cache_version as _impl
    return _impl()


def bump_cache_version():
    from repositories.inventory_repository import bump_cache_version as _impl
    return _impl()


def batch_status(batch: Batch) -> str:
    from repositories.inventory_repository import batch_status as _impl
    return _impl(batch)


def inventory_status(total_remaining: int, safety_stock: int) -> str:
    from repositories.inventory_repository import inventory_status as _impl
    return _impl(total_remaining, safety_stock)


def inventory_row(product: Product, batches: list[Batch]) -> dict:
    from repositories.inventory_repository import inventory_row as _impl
    return _impl(product, batches)


def get_inventory_items(db: Session) -> list[dict]:
    from repositories.inventory_repository import get_inventory_items as _impl
    return _impl(db)


# ---------------------------------------------------------------------------
# Email service — delegates to services/email_service.py
# ---------------------------------------------------------------------------
def send_verification_email(recipient_email: str, otp_code: str):
    from services.email_service import send_verification_email as _impl
    return _impl(
        recipient_email,
        otp_code,
        smtp_host=SMTP_HOST,
        smtp_port=SMTP_PORT,
        smtp_username=SMTP_USERNAME,
        smtp_password=SMTP_PASSWORD,
        smtp_from=SMTP_FROM,
        smtp_use_tls=SMTP_USE_TLS,
        otp_expires_minutes=OTP_EXPIRE_MINUTES,
    )


# ---------------------------------------------------------------------------
# HTML page routes
# ---------------------------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
def landing_page():
    return HTMLResponse(render_landing_page())


@app.get("/user", response_class=HTMLResponse)
def user_page():
    return HTMLResponse(render_portal_page("user"))


@app.get("/admin", response_class=HTMLResponse)
def admin_page():
    return HTMLResponse(render_portal_page("admin"))


@app.get("/supplier", response_class=HTMLResponse)
def supplier_page():
    return HTMLResponse(render_supplier_dashboard())


@app.get("/supplier/dashboard", response_class=HTMLResponse)
def supplier_dashboard_page():
    return HTMLResponse(render_supplier_dashboard())


@app.get("/dashboard", response_class=HTMLResponse)
def dashboard_page(db: Session = Depends(get_db)):
    return HTMLResponse(render_dashboard(get_inventory_items(db)))


@app.get("/qa", response_class=HTMLResponse)
def qa_page(db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    require_admin(current_user)
    return HTMLResponse(render_quality_dashboard(get_inventory_items(db)))


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------
@app.get("/health")
def health_check():
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# Auth API routes (rate-limited)
# ---------------------------------------------------------------------------
@app.post("/auth/register", response_model=AuthMessage)
@limiter.limit("5/minute")
def register_user(
    request: Request,
    payload: UserCreate,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
):
    user, otp_code = create_pending_user(db, payload, send_email=False)
    background_tasks.add_task(send_verification_email, user.email, otp_code)
    return AuthMessage(message=f"Verification code sent to {user.email}")


@app.post("/auth/token", response_model=Token)
@limiter.limit("10/minute")
def login_for_access_token(
    request: Request,
    payload: UserLogin,
    db: Session = Depends(get_db),
):
    user = authenticate_user(db, payload.identifier, payload.password)
    if not user:
        raise HTTPException(
            status_code=401,
            detail="Incorrect username or password",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return create_user_token(db, user)


@app.post("/auth/verify-otp", response_model=Token)
@limiter.limit("5/minute")
def verify_otp(
    request: Request,
    payload: OtpVerifyRequest,
    db: Session = Depends(get_db),
):
    user = verify_user_otp(db, payload.email, payload.otp_code)
    return create_user_token(db, user)


@app.get("/auth/me")
def read_current_user(current_user: User = Depends(get_current_user)):
    return {
        "id": current_user.id,
        "username": current_user.username,
        "email": current_user.email,
        "role": current_user.role,
        "is_active": bool(current_user.is_active),
        "is_verified": bool(current_user.is_verified),
    }


# ---------------------------------------------------------------------------
# Admin routes
# ---------------------------------------------------------------------------
@app.post("/admin/users", response_model=AuthMessage)
def create_admin_account(
    payload: AdminUserCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    require_admin(current_user)
    user = create_admin_user(db, payload)
    return AuthMessage(message=f"Created {user.role} account for {user.email}")


@app.get("/admin/users")
def list_users(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    page: int = 1,
    limit: int = 50,
):
    require_admin(current_user)
    offset = (page - 1) * limit
    total = db.query(User).count()
    users = db.query(User).order_by(User.id.asc()).offset(offset).limit(limit).all()
    return {
        "total": total,
        "page": page,
        "limit": limit,
        "count": len(users),
        "items": [
            {
                "id": user.id,
                "username": user.username,
                "email": user.email,
                "role": user.role,
                "is_active": bool(user.is_active),
                "is_verified": bool(user.is_verified),
            }
            for user in users
        ],
    }


@app.get("/admin/scans")
def all_scans(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    page: int = 1,
    limit: int = 50,
):
    require_admin(current_user)
    offset = (page - 1) * limit
    total = db.query(ProductScan).count()
    scans = (
        db.query(ProductScan)
        .order_by(ProductScan.captured_at.desc())
        .offset(offset)
        .limit(limit)
        .all()
    )
    return {
        "total": total,
        "page": page,
        "limit": limit,
        "count": len(scans),
        "items": [serialize_scan(scan) for scan in scans],
    }


# ---------------------------------------------------------------------------
# Scan helpers and routes
# ---------------------------------------------------------------------------
def serialize_scan(scan: ProductScan) -> dict:
    return {
        "id": scan.id,
        "scan_code": scan.scan_code,
        "product_id": scan.product_id,
        "product_sku": scan.product_sku,
        "product_name": scan.product_name,
        "product_category": scan.product_category,
        "quantity": scan.quantity,
        "quantity_remaining_snapshot": scan.quantity_remaining_snapshot,
        "batch_count_snapshot": scan.batch_count_snapshot,
        "inventory_status_snapshot": scan.inventory_status_snapshot,
        "captured_at": scan.captured_at.isoformat() if scan.captured_at else None,
    }


def resolve_scan_product(db: Session, scan_code: str) -> Product:
    cleaned = scan_code.strip()
    product = None
    if cleaned.isdigit():
        product = db.query(Product).filter(Product.id == int(cleaned)).first()
    if not product:
        product = db.query(Product).filter(Product.sku == cleaned).first()
    if not product:
        raise HTTPException(status_code=404, detail="Product not found for scan code")
    return product


def build_scan_preview(db: Session, product: Product) -> dict:
    cache_key = f"{CACHE_PREFIX}:{cache_version()}:scan-preview:{product.sku.strip().lower()}"
    cached_preview = cache_get_json(cache_key)
    if cached_preview is not None:
        return cached_preview

    batches = db.query(Batch).filter(Batch.product_id == product.id).all()
    total_remaining = sum(batch.quantity_remaining for batch in batches)
    preview = {
        "product_id": product.id,
        "sku": product.sku,
        "name": product.name,
        "category": product.category,
        "safety_stock": product.safety_stock or 0,
        "supplier_id": product.supplier_id,
        "quantity_remaining_snapshot": total_remaining,
        "batch_count_snapshot": len(batches),
        "inventory_status_snapshot": inventory_status(total_remaining, product.safety_stock or 0),
    }
    cache_set_json(cache_key, preview)
    return preview


def record_product_scan(db: Session, current_user: User, payload: ScanCreate) -> ProductScan:
    require_role(current_user, {"user", "admin", "supplier"})
    product = resolve_scan_product(db, payload.scan_code)
    if current_user.role == "supplier" and current_user.supplier_id != product.supplier_id:
        raise HTTPException(status_code=403, detail="Supplier can only scan own products")

    batches = db.query(Batch).filter(Batch.product_id == product.id).all()
    total_remaining = sum(batch.quantity_remaining for batch in batches)
    scan = ProductScan(
        scan_code=payload.scan_code.strip(),
        product_id=product.id,
        scanned_by_user_id=current_user.id,
        quantity=max(1, payload.quantity),
        product_sku=product.sku,
        product_name=product.name,
        product_category=product.category,
        supplier_id=product.supplier_id,
        quantity_remaining_snapshot=total_remaining,
        batch_count_snapshot=len(batches),
        inventory_status_snapshot=inventory_status(total_remaining, product.safety_stock or 0),
    )
    db.add(scan)
    db.commit()
    db.refresh(scan)
    return scan


@app.get("/scans/preview", response_model=ScanPreviewResponse)
def preview_scan(
    scan_code: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    require_role(current_user, {"user", "admin", "supplier"})
    product = resolve_scan_product(db, scan_code)
    if current_user.role == "supplier" and current_user.supplier_id != product.supplier_id:
        raise HTTPException(status_code=403, detail="Supplier can only preview own products")
    return build_scan_preview(db, product)


@app.post("/scans", response_model=ScanCaptureResponse)
def capture_scan(
    payload: ScanCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    scan = record_product_scan(db, current_user, payload)
    return ScanCaptureResponse(
        message=f"Captured scan for {scan.product_sku}",
        product_id=scan.product_id,
        product_sku=scan.product_sku,
        quantity_remaining_snapshot=scan.quantity_remaining_snapshot,
    )


@app.get("/scans/me")
def my_scans(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    page: int = 1,
    limit: int = 20,
):
    offset = (page - 1) * limit
    total = db.query(ProductScan).filter(ProductScan.scanned_by_user_id == current_user.id).count()
    scans = (
        db.query(ProductScan)
        .filter(ProductScan.scanned_by_user_id == current_user.id)
        .order_by(ProductScan.captured_at.desc())
        .offset(offset)
        .limit(limit)
        .all()
    )
    return {
        "total": total,
        "page": page,
        "limit": limit,
        "count": len(scans),
        "items": [serialize_scan(scan) for scan in scans],
    }


# ---------------------------------------------------------------------------
# Inventory & product routes
# ---------------------------------------------------------------------------
@app.get("/inventory")
def inventory(db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    require_admin(current_user)
    items = get_inventory_items(db)
    return {"count": len(items), "items": items}


@app.post("/suppliers")
def create_supplier(
    s: SupplierCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    require_admin(current_user)
    supplier = Supplier(name=s.name, contact_email=s.contact_email, lead_time_days=s.lead_time_days)
    db.add(supplier)
    db.commit()
    db.refresh(supplier)
    bump_cache_version()
    return supplier


@app.post("/products")
def create_product(
    p: ProductCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    require_admin(current_user)
    product = Product(
        sku=p.sku,
        name=p.name,
        category=p.category,
        safety_stock=p.safety_stock,
        supplier_id=p.supplier_id,
    )
    db.add(product)
    db.commit()
    db.refresh(product)
    bump_cache_version()
    return product


@app.post("/batches")
def create_batch(
    b: BatchCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    require_admin(current_user)
    batch = Batch(
        product_id=b.product_id,
        batch_number=b.batch_number,
        quantity_received=b.quantity_received,
        quantity_remaining=b.quantity_received,
        expiry_date=b.expiry_date,
    )
    db.add(batch)
    db.commit()
    db.refresh(batch)
    bump_cache_version()
    return batch


@app.post("/sales")
def record_sale(
    s: SaleCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    qty_to_deduct = s.quantity
    batches = (
        db.query(Batch)
        .filter(Batch.product_id == s.product_id, Batch.quantity_remaining > 0)
        .order_by(Batch.expiry_date.asc().nulls_last())
        .with_for_update()
        .all()
    )
    if not batches:
        raise HTTPException(status_code=400, detail="No stock available for this product")

    for batch in batches:
        if qty_to_deduct <= 0:
            break
        take = min(batch.quantity_remaining, qty_to_deduct)
        batch.quantity_remaining -= take
        qty_to_deduct -= take
        db.add(batch)

    if qty_to_deduct > 0:
        db.rollback()
        raise HTTPException(status_code=400, detail="Not enough stock to fulfill sale")

    sale = Sale(product_id=s.product_id, quantity=s.quantity, timestamp=utc_now())
    db.add(sale)
    db.commit()
    db.refresh(sale)
    bump_cache_version()
    return {"sale_id": sale.id}


@app.get("/products/{product_id}/rop")
def product_rop(
    product_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    require_role(current_user, {"admin", "supplier"})
    product = db.query(Product).filter(Product.id == product_id).first()
    if not product:
        raise HTTPException(status_code=404, detail="Product not found")
    if current_user.role == "supplier" and current_user.supplier_id != product.supplier_id:
        raise HTTPException(status_code=403, detail="Supplier can only access own products")

    days_window = 14
    cutoff = utc_now() - timedelta(days=days_window)
    sales = db.query(Sale).filter(Sale.product_id == product_id, Sale.timestamp >= cutoff).all()
    sales_tuples = [(s.timestamp, s.quantity) for s in sales]
    d = compute_velocity(sales_tuples, days_window=days_window)

    L = 7
    if product.supplier_id:
        supplier = db.query(Supplier).filter(Supplier.id == product.supplier_id).first()
        if supplier:
            L = supplier.lead_time_days
    SS = product.safety_stock or 0
    rop = compute_rop(d, L, SS)
    return {"d": d, "L": L, "SS": SS, "ROP": rop}


@app.get("/products/{product_id}/batches")
def product_batches(
    product_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    require_admin(current_user)
    product = db.query(Product).filter(Product.id == product_id).first()
    if not product:
        raise HTTPException(status_code=404, detail="Product not found")

    batches = (
        db.query(Batch)
        .filter(Batch.product_id == product_id)
        .order_by(Batch.expiry_date.asc().nulls_last())
        .all()
    )

    return {
        "product_id": product_id,
        "sku": product.sku,
        "batches": [
            {
                "batch_id": batch.id,
                "batch_number": batch.batch_number,
                "quantity_received": batch.quantity_received,
                "quantity_remaining": batch.quantity_remaining,
                "expiry_date": batch.expiry_date,
                "status": batch_status(batch),
            }
            for batch in batches
        ],
    }


# ---------------------------------------------------------------------------
# Supplier routes
# ---------------------------------------------------------------------------
@app.get("/supplier/movement")
def supplier_movement(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    require_role(current_user, {"supplier"})
    if current_user.supplier_id is None:
        raise HTTPException(status_code=403, detail="Supplier account is not linked to a supplier")

    from sqlalchemy import func

    supplier_id = current_user.supplier_id
    window_start = utc_now() - timedelta(days=14)

    products = db.query(Product).filter(Product.supplier_id == supplier_id).all()
    if not products:
        return {"count": 0, "items": []}

    product_ids = [p.id for p in products]

    # Aggregate remaining stock — single query instead of N per product
    batch_totals = (
        db.query(Batch.product_id, func.sum(Batch.quantity_remaining).label("total_remaining"))
        .filter(Batch.product_id.in_(product_ids))
        .group_by(Batch.product_id)
        .all()
    )
    batch_map = {row.product_id: row.total_remaining or 0 for row in batch_totals}

    # Aggregate sales in window — single query instead of N per product
    sales_totals = (
        db.query(Sale.product_id, func.sum(Sale.quantity).label("total_sold"))
        .filter(Sale.product_id.in_(product_ids), Sale.timestamp >= window_start)
        .group_by(Sale.product_id)
        .all()
    )
    sales_map = {row.product_id: row.total_sold or 0 for row in sales_totals}

    items = []
    for product in products:
        total_remaining = batch_map.get(product.id, 0)
        items.append(
            {
                "product_id": product.id,
                "sku": product.sku,
                "name": product.name,
                "total_remaining": total_remaining,
                "sales_last_14_days": sales_map.get(product.id, 0),
                "status": inventory_status(total_remaining, product.safety_stock or 0),
            }
        )

    return {"count": len(items), "items": items}
