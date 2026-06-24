from datetime import date, datetime, timedelta, timezone
from html import escape as html_escape
import json
import os
import secrets
import smtplib
from email.message import EmailMessage
from pathlib import Path

from fastapi import BackgroundTasks, FastAPI, Depends, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.security import OAuth2PasswordBearer
from email_validator import EmailNotValidError, validate_email
from jose import JWTError, jwt
from sqlalchemy.orm import Session
from passlib.context import CryptContext
import redis

from db import SessionLocal, init_db
from models import Supplier, Product, Batch, Sale, User, UserSession, ProductScan
from schemas import SupplierCreate, ProductCreate, BatchCreate, SaleCreate, UserCreate, UserLogin, Token, OtpVerifyRequest, AuthMessage, AdminUserCreate, ScanCreate, ScanCaptureResponse, ScanPreviewResponse
from rop import compute_velocity, compute_rop

app = FastAPI(title="StockPulse")
BASE_DIR = Path(__file__).resolve().parent
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")

SECRET_KEY = os.getenv("STOCKPULSE_SECRET_KEY", "stockpulse-dev-secret")
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
_redis_client: object | None | bool = None


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


def send_verification_email(recipient_email: str, otp_code: str):
    if not SMTP_HOST:
        raise HTTPException(status_code=503, detail="Email service is not configured")

    message = EmailMessage()
    message["Subject"] = "StockPulse verification code"
    message["From"] = SMTP_FROM or SMTP_USERNAME
    message["To"] = recipient_email
    message.set_content(
        f"Your StockPulse verification code is {otp_code}. It expires in {OTP_EXPIRE_MINUTES} minutes."
    )

    with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=15) as client:
        if SMTP_USE_TLS:
            client.starttls()
        if SMTP_USERNAME:
            client.login(SMTP_USERNAME, SMTP_PASSWORD)
        client.send_message(message)


def issue_otp_for_user(db: Session, user: User) -> str:
    otp_code = generate_otp_code()
    user.otp_code_hash = hash_password(otp_code)
    user.otp_expires_at = utc_now() + timedelta(minutes=OTP_EXPIRE_MINUTES)
    db.add(user)
    db.commit()
    db.refresh(user)
    return otp_code


def create_pending_user(db: Session, payload: UserCreate, send_email: bool = True, check_deliverability: bool = True) -> tuple[User, str]:
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
        is_active=1,
        is_verified=0,
        supplier_id=None,
    )
    db.add(user)
    db.commit()
    db.refresh(user)

    otp_code = issue_otp_for_user(db, user)
    if send_email:
        send_verification_email(email, otp_code)
    return user, otp_code


def create_admin_user(db: Session, payload: AdminUserCreate, check_deliverability: bool = True) -> User:
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
        is_active=1,
        is_verified=1,
        supplier_id=payload.supplier_id,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def verify_user_otp(db: Session, email: str, otp_code: str, check_deliverability: bool = True) -> User:
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

    user.is_verified = 1
    user.otp_code_hash = None
    user.otp_expires_at = None
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def create_session(db: Session, user: User) -> tuple[str, UserSession]:
    jti = generate_jti()
    expires_at = utc_now() + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    session = UserSession(jti=jti, user_id=user.id, expires_at=expires_at, last_seen_at=utc_now(), revoked=0)
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
        session.revoked = 1
        db.add(session)
        db.commit()
        return None
    last_seen = as_utc(session.last_seen_at)
    if last_seen is None or last_seen + timedelta(minutes=SESSION_IDLE_TIMEOUT_MINUTES) < utc_now():
        session.revoked = 1
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


def get_current_user(token: str = Depends(oauth2_scheme), db: Session = Depends(get_db)) -> User:
    credentials_error = HTTPException(status_code=401, detail="Could not validate credentials", headers={"WWW-Authenticate": "Bearer"})
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


def batch_status(batch: Batch) -> str:
    if batch.quantity_remaining <= 0:
        return "depleted"
    if batch.expiry_date is None:
        return "active"

    today = date.today()
    if batch.expiry_date < today:
        return "expired"
    if batch.expiry_date <= today + timedelta(days=30):
        return "expiring_soon"
    return "active"


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def get_cache_client():
    global _redis_client
    if _redis_client is False:
        return None
    if _redis_client is None:
        try:
            client = redis.Redis.from_url(
                REDIS_CACHE_URL,
                decode_responses=True,
                socket_connect_timeout=1,
                socket_timeout=1,
            )
            client.ping()
            _redis_client = client
        except Exception:
            _redis_client = False
            return None
    return _redis_client if isinstance(_redis_client, redis.Redis) else None


def cache_get_json(key: str):
    client = get_cache_client()
    if not client:
        return None
    try:
        cached = client.get(key)
        return json.loads(cached) if cached else None
    except Exception:
        return None


def cache_set_json(key: str, value, ttl_seconds: int = CACHE_TTL_SECONDS):
    client = get_cache_client()
    if not client:
        return
    try:
        client.setex(key, ttl_seconds, json.dumps(value, default=str))
    except Exception:
        return


def cache_version() -> str:
    client = get_cache_client()
    if not client:
        return "0"
    try:
        current = client.get(CACHE_VERSION_KEY)
        if current is None:
            client.set(CACHE_VERSION_KEY, "1")
            return "1"
        return str(current)
    except Exception:
        return "0"


def bump_cache_version():
    client = get_cache_client()
    if not client:
        return
    try:
        client.incr(CACHE_VERSION_KEY)
    except Exception:
        return


def inventory_status(total_remaining: int, safety_stock: int) -> str:
    if total_remaining <= 0:
        return "out_of_stock"
    if total_remaining <= safety_stock:
        return "low_stock"
    return "healthy"


def inventory_row(product: Product, batches: list[Batch]) -> dict:
    total_received = sum(batch.quantity_received for batch in batches)
    total_remaining = sum(batch.quantity_remaining for batch in batches)
    next_expiry = min((batch.expiry_date for batch in batches if batch.expiry_date is not None), default=None)

    return {
        "product_id": product.id,
        "sku": product.sku,
        "name": product.name,
        "category": product.category,
        "safety_stock": product.safety_stock,
        "total_received": total_received,
        "total_remaining": total_remaining,
        "batch_count": len(batches),
        "next_expiry": next_expiry,
        "status": inventory_status(total_remaining, product.safety_stock or 0),
    }


def get_inventory_items(db: Session) -> list[dict]:
    cache_key = f"{CACHE_PREFIX}:{cache_version()}:inventory"
    cached_items = cache_get_json(cache_key)
    if cached_items is not None:
        return cached_items

    products = db.query(Product).all()
    items = []

    for product in products:
        batches = (
            db.query(Batch)
            .filter(Batch.product_id == product.id)
            .order_by(Batch.expiry_date.asc().nulls_last())
            .all()
        )
        items.append(inventory_row(product, batches))

    cache_set_json(cache_key, items)
    return items


def render_dashboard(items: list[dict]) -> str:
    total_products = len(items)
    total_units = sum(item["total_remaining"] for item in items)
    low_stock = sum(1 for item in items if item["status"] == "low_stock")
    out_of_stock = sum(1 for item in items if item["status"] == "out_of_stock")

    rows = []
    for item in items:
        next_expiry = item["next_expiry"] or "-"
        rows.append(
            f"""
            <tr>
                <td>{html_escape(str(item['sku']))}</td>
                <td>{html_escape(str(item['name']))}</td>
                <td>{html_escape(str(item['category'] or '-'))}</td>
                <td>{html_escape(str(item['total_remaining']))}</td>
                <td>{html_escape(str(item['safety_stock']))}</td>
                <td>{html_escape(str(item['batch_count']))}</td>
                <td>{html_escape(str(next_expiry))}</td>
                <td><span class='status status-{html_escape(str(item['status']))}'>{html_escape(str(item['status']))}</span></td>
            </tr>
            """
        )

    table_rows = "\n".join(rows) if rows else """
        <tr>
            <td colspan='8' class='empty'>No products yet. Create suppliers, products, and batches to populate the dashboard.</td>
        </tr>
    """

    return f"""
    <!doctype html>
    <html lang="en">
    <head>
        <meta charset="utf-8" />
        <meta name="viewport" content="width=device-width, initial-scale=1" />
        <title>StockPulse Dashboard</title>
        <link rel="icon" type="image/svg+xml" href="/static/stockpulse-icon.svg" />
        <style>
            :root {{
                color-scheme: light;
                --bg: #0f172a;
                --panel: #111827;
                --card: #1f2937;
                --text: #e5e7eb;
                --muted: #9ca3af;
                --accent: #38bdf8;
                --good: #10b981;
                --warn: #f59e0b;
                --bad: #ef4444;
            }}
            * {{ box-sizing: border-box; }}
            body {{ margin: 0; font-family: Arial, Helvetica, sans-serif; background: linear-gradient(180deg, #020617 0%, #0f172a 55%, #111827 100%); color: var(--text); }}
            .shell {{ max-width: 1200px; margin: 0 auto; padding: 32px 20px 56px; }}
            .hero {{ display: grid; gap: 16px; margin-bottom: 24px; }}
            .eyebrow {{ color: var(--accent); text-transform: uppercase; letter-spacing: 0.16em; font-size: 12px; font-weight: 700; }}
            h1 {{ margin: 0; font-size: clamp(2rem, 4vw, 3.6rem); line-height: 1.05; }}
            .sub {{ max-width: 780px; color: var(--muted); font-size: 1rem; line-height: 1.6; }}
            .stats {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 14px; margin: 24px 0; }}
            .stat {{ background: rgba(31, 41, 55, 0.88); border: 1px solid rgba(148, 163, 184, 0.14); border-radius: 18px; padding: 18px; }}
            .stat-label {{ color: var(--muted); font-size: 13px; margin-bottom: 8px; }}
            .stat-value {{ font-size: 2rem; font-weight: 800; }}
            .panel {{ background: rgba(17, 24, 39, 0.92); border: 1px solid rgba(148, 163, 184, 0.14); border-radius: 20px; overflow: hidden; box-shadow: 0 24px 80px rgba(0, 0, 0, 0.28); }}
            .panel-head {{ display: flex; justify-content: space-between; align-items: center; padding: 18px 20px; border-bottom: 1px solid rgba(148, 163, 184, 0.14); }}
            .panel-head h2 {{ margin: 0; font-size: 1.1rem; }}
            table {{ width: 100%; border-collapse: collapse; }}
            th, td {{ padding: 14px 16px; text-align: left; border-bottom: 1px solid rgba(148, 163, 184, 0.1); }}
            th {{ color: var(--muted); font-size: 12px; letter-spacing: 0.08em; text-transform: uppercase; }}
            tr:hover td {{ background: rgba(255, 255, 255, 0.02); }}
            .status {{ display: inline-flex; align-items: center; padding: 6px 10px; border-radius: 999px; font-size: 12px; font-weight: 700; text-transform: uppercase; letter-spacing: 0.04em; }}
            .status-healthy {{ background: rgba(16, 185, 129, 0.14); color: #6ee7b7; }}
            .status-low_stock, .status-expiring_soon {{ background: rgba(245, 158, 11, 0.14); color: #fcd34d; }}
            .status-out_of_stock, .status-expired, .status-depleted {{ background: rgba(239, 68, 68, 0.14); color: #fca5a5; }}
            .empty {{ text-align: center; color: var(--muted); padding: 36px 16px; }}
            .footer {{ margin-top: 18px; color: var(--muted); font-size: 13px; }}
            @media (max-width: 720px) {{
                .panel-head {{ flex-direction: column; align-items: flex-start; gap: 8px; }}
                th:nth-child(3), td:nth-child(3), th:nth-child(6), td:nth-child(6), th:nth-child(7), td:nth-child(7) {{ display: none; }}
            }}
        </style>
    </head>
    <body>
        <main class="shell">
            <section class="hero">
                <div class="eyebrow">StockPulse AI</div>
                <h1>Inventory dashboard for FEFO stock and replenishment.</h1>
                <p class="sub">Track products, remaining units, batch expiry risk, and low-stock signals from the same Python prototype that powers the API.</p>
            </section>

            <section class="stats">
                <div class="stat"><div class="stat-label">Products tracked</div><div class="stat-value">{total_products}</div></div>
                <div class="stat"><div class="stat-label">Units remaining</div><div class="stat-value">{total_units}</div></div>
                <div class="stat"><div class="stat-label">Low stock</div><div class="stat-value">{low_stock}</div></div>
                <div class="stat"><div class="stat-label">Out of stock</div><div class="stat-value">{out_of_stock}</div></div>
            </section>

            <section class="panel">
                <div class="panel-head">
                    <h2>Current inventory</h2>
                    <div class="footer">Use the API at /inventory and /products/{{id}}/batches for programmatic access.</div>
                </div>
                <table>
                    <thead>
                        <tr>
                            <th>SKU</th>
                            <th>Name</th>
                            <th>Category</th>
                            <th>Remaining</th>
                            <th>Safety stock</th>
                            <th>Batches</th>
                            <th>Next expiry</th>
                            <th>Status</th>
                        </tr>
                    </thead>
                    <tbody>
                        {table_rows}
                    </tbody>
                </table>
            </section>
        </main>
    </body>
    </html>
    """


def render_landing_page() -> str:
        return """
        <!doctype html>
        <html lang="en">
        <head>
                <meta charset="utf-8" />
                <meta name="viewport" content="width=device-width, initial-scale=1" />
                <title>StockPulse Roles</title>
                <link rel="icon" type="image/svg+xml" href="/static/stockpulse-icon.svg" />
                <style>
                        body { margin: 0; font-family: Arial, Helvetica, sans-serif; background: radial-gradient(circle at top, #1e293b, #020617 60%); color: #e5e7eb; }
                        .wrap { max-width: 1120px; margin: 0 auto; padding: 32px 18px 56px; }
                        .hero { display: grid; gap: 14px; margin-bottom: 28px; }
                    .brand { display: inline-flex; align-items: center; gap: 10px; padding: 10px 14px; width: fit-content; border-radius: 999px; background: rgba(15,23,42,.72); border: 1px solid rgba(148,163,184,.18); box-shadow: 0 20px 50px rgba(0,0,0,.18); }
                    .brand img { width: 30px; height: 30px; }
                    .brand span { font-weight: 800; letter-spacing: .02em; }
                        .eyebrow { color: #38bdf8; text-transform: uppercase; letter-spacing: .16em; font-size: 12px; font-weight: 700; }
                        h1 { margin: 0; font-size: clamp(2.3rem, 5vw, 4rem); line-height: 1.02; }
                        .sub { color: #cbd5e1; max-width: 760px; line-height: 1.7; }
                        .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(240px, 1fr)); gap: 16px; }
                        .card { background: rgba(17, 24, 39, .92); border: 1px solid rgba(148, 163, 184, .16); border-radius: 20px; padding: 20px; box-shadow: 0 24px 60px rgba(0,0,0,.22); }
                        .card h2 { margin: 0 0 10px; font-size: 1.1rem; }
                        .card p { color: #9ca3af; line-height: 1.6; margin: 0 0 16px; }
                        .btn { display: inline-flex; align-items: center; justify-content: center; min-width: 140px; padding: 12px 16px; border-radius: 12px; text-decoration: none; font-weight: 800; color: #0f172a; background: linear-gradient(135deg, #38bdf8, #22c55e); }
                        .btn.secondary { background: transparent; color: #93c5fd; border: 1px solid rgba(147,197,253,.25); }
                </style>
        </head>
        <body>
            <main class="wrap">
                <section class="hero">
                    <div class="brand"><img src="/static/stockpulse-icon.svg" alt="StockPulse icon" /><span>StockPulse</span></div>
                    <div class="eyebrow">StockPulse AI</div>
                    <h1>Choose your access level.</h1>
                    <p class="sub">Public users can register as plain users only. Admin and supplier accounts are provisioned separately, and each portal keeps the experience scoped to that role.</p>
                </section>

                <section class="grid">
                    <article class="card">
                        <h2>User</h2>
                        <p>Register, verify OTP, capture scans, and view your personal history.</p>
                        <a class="btn" href="/user">Open user page</a>
                    </article>
                    <article class="card">
                        <h2>Admin</h2>
                        <p>Sign in to provision accounts, review scans, and manage inventory data.</p>
                        <a class="btn" href="/admin">Open admin page</a>
                    </article>
                    <article class="card">
                        <h2>Supplier</h2>
                        <p>View supplier-only movement, scans, and replenishment signals for your linked products.</p>
                        <a class="btn" href="/supplier">Open supplier page</a>
                    </article>
                </section>
            </main>
        </body>
        </html>
        """


def render_portal_page(page_role: str) -> str:
    page_title = "Admin Page - StockPulse" if page_role == "admin" else "User Page - StockPulse"
    page_subtitle = "Admin controls, provisioning, and inventory access in one place." if page_role == "admin" else "Start with registration, verify OTP, then sign in to continue."
    user_register_card = "" if page_role != "user" else """
                    <section class="card flow-card" data-step="register">
                        <h2>Register</h2>
                        <form id="register-form" class="row">
                            <label>Username<input id="reg_username" required></label>
                            <label>Email<input id="reg_email" type="email" required></label>
                            <label>Password<input id="reg_password" type="password" minlength="8" required></label>
                            <button type="submit">Create account and send OTP</button>
                        </form>
                        <button type="button" class="stepbtn secondary flow-link" data-flow-action="login">I already have an account</button>
                    </section>
"""
    admin_inventory_cards = "" if page_role != "admin" else """
                    <section class="card">
                        <h2>Inventory</h2>
                        <div id="inventory" class="list"></div>
                    </section>

                    <section class="card">
                        <h2>Admin users</h2>
                        <div id="admin-users" class="list"></div>
                    </section>
"""
    user_flow_nav = "" if page_role != "user" else """
                <section class="card">
                    <h2>Getting started</h2>
                    <div class="flow-track" aria-label="Onboarding progress">
                        <span class="flow-step is-active" data-step-target="register">1. Register</span>
                        <span class="flow-step" data-step-target="verify">2. Verify OTP</span>
                        <span class="flow-step" data-step-target="login">3. Login</span>
                        <span class="flow-step" data-step-target="dashboard" hidden>4. Dashboard</span>
                    </div>
                    <p class="muted flow-help">Finish one step at a time. Only the current step is shown. If you already have an account, jump straight to login.</p>
                </section>
"""
    user_verify_card = "" if page_role != "user" else """
                    <section class="card flow-card hidden" data-step="verify">
                        <h2>Verify OTP</h2>
                        <form id="verify-form" class="row">
                            <label>Email<input id="otp_email" type="email" required></label>
                            <label>OTP code<input id="otp_code" inputmode="numeric" maxlength="6" required></label>
                            <button type="submit">Verify and continue</button>
                        </form>
                    </section>
"""
    user_login_card = "" if page_role != "user" else """
                    <section class="card flow-card hidden" data-step="login">
                        <h2>Login</h2>
                        <form id="login-form" class="row">
                            <label>Email or username<input id="login_identifier" required></label>
                            <label>Password<input id="login_password" type="password" required></label>
                            <button type="submit">Sign in</button>
                        </form>
                    </section>
"""
    user_dashboard_cards = "" if page_role != "user" else """
                    <section class="card flow-card hidden" data-step="dashboard">
                        <h2>Profile</h2>
                        <div id="profile" class="status">Load a token to view profile details.</div>
                    </section>

                    <section class="card flow-card hidden" data-step="dashboard">
                        <h2>Camera barcode capture</h2>
                        <p class="muted">Start the camera, point it at a barcode, and StockPulse will preview the item name, SKU, category, and inventory snapshot.</p>
                        <div class="camera-shell">
                            <video id="barcode-video" class="camera-view" playsinline muted></video>
                            <div class="camera-actions">
                                <button type="button" class="stepbtn" id="start-camera">Start camera scan</button>
                                <button type="button" class="stepbtn secondary" id="stop-camera">Stop camera</button>
                            </div>
                            <div id="camera-status" class="status">Camera is idle.</div>
                            <div id="scan-preview" class="item muted">No barcode scanned yet.</div>
                        </div>
                    </section>

                    <section class="card flow-card hidden" data-step="dashboard">
                        <h2>Scan product</h2>
                        <form id="scan-form" class="row">
                            <label>Barcode or SKU<input id="scan_code" placeholder="Scan with camera or type manually" required></label>
                            <label>Quantity<input id="scan_qty" type="number" min="1" value="1" required></label>
                            <button type="submit">Capture scan</button>
                        </form>
                    </section>

                    <section class="card flow-card hidden" data-step="dashboard">
                        <h2>Recent scans</h2>
                        <div id="scan-history" class="list"></div>
                    </section>
"""
    admin_form = "" if page_role != "admin" else """
                    <section class="card">
                        <h2>Provision account</h2>
                        <form id="admin-create-form" class="row">
                            <label>Username<input id="admin_new_username" required></label>
                            <label>Email<input id="admin_new_email" type="email" required></label>
                            <label>Password<input id="admin_new_password" type="password" minlength="8" required></label>
                            <label>Role<select id="admin_new_role"><option value="admin">Admin</option><option value="supplier">Supplier</option></select></label>
                            <label>Supplier ID (for supplier accounts)<input id="admin_new_supplier_id" inputmode="numeric" placeholder="Optional"></label>
                            <button type="submit">Create account</button>
                        </form>
                    </section>
"""
    scan_scope = "admin" if page_role == "admin" else "me"

    return f"""
        <!doctype html>
        <html lang="en">
        <head>
            <meta charset="utf-8" />
            <meta name="viewport" content="width=device-width, initial-scale=1" />
            <title>{page_title}</title>
            <link rel="icon" type="image/svg+xml" href="/static/stockpulse-icon.svg" />
            <style>
                :root {{ color-scheme: dark; }}
                body {{ margin: 0; font-family: "Segoe UI", Inter, Arial, sans-serif; background:
                    radial-gradient(circle at top left, rgba(56,189,248,.18), transparent 34%),
                    radial-gradient(circle at top right, rgba(34,197,94,.14), transparent 28%),
                    linear-gradient(180deg, #020617 0%, #0f172a 50%, #111827 100%); color: #e5e7eb; }}
                body::before {{ content: ""; position: fixed; inset: 0; pointer-events: none; background-image: linear-gradient(rgba(255,255,255,.035) 1px, transparent 1px), linear-gradient(90deg, rgba(255,255,255,.035) 1px, transparent 1px); background-size: 42px 42px; mask-image: linear-gradient(180deg, rgba(0,0,0,.9), transparent 78%); }}
                .wrap {{ max-width: 1240px; margin: 0 auto; padding: 28px 18px 64px; position: relative; z-index: 1; }}
                .hero {{ display: grid; gap: 14px; margin-bottom: 26px; padding: 22px 22px 6px; border: 1px solid rgba(148,163,184,.12); border-radius: 28px; background: rgba(15, 23, 42, .55); backdrop-filter: blur(18px); box-shadow: 0 26px 80px rgba(0,0,0,.24); }}
                .eyebrow {{ color: #67e8f9; text-transform: uppercase; letter-spacing: .18em; font-size: 11px; font-weight: 800; }}
                h1 {{ margin: 0; font-size: clamp(2.2rem, 4vw, 3.8rem); line-height: 1; letter-spacing: -0.04em; }}
                .sub {{ color: #cbd5e1; max-width: 820px; line-height: 1.65; margin: 0; font-size: 1.03rem; }}
                .grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(300px, 1fr)); gap: 16px; align-items: start; }}
                .card {{ background: linear-gradient(180deg, rgba(15, 23, 42, .88), rgba(15, 23, 42, .74)); border: 1px solid rgba(148, 163, 184, .14); border-radius: 24px; padding: 20px; box-shadow: 0 24px 60px rgba(0,0,0,.22); }}
                .card h2 {{ margin: 0 0 12px; font-size: 1.05rem; letter-spacing: -0.02em; }}
                .camera-shell {{ display: grid; gap: 12px; }}
                .camera-view {{ width: 100%; aspect-ratio: 4 / 3; border-radius: 18px; background: #020617; border: 1px solid rgba(148,163,184,.14); object-fit: cover; min-height: 240px; }}
                .camera-actions {{ display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 10px; }}
                .flow-track {{ display: grid; gap: 10px; padding: 4px 0 12px; }}
                .flow-step {{ position: relative; padding-left: 16px; color: #94a3b8; font-size: 13px; letter-spacing: .01em; }}
                .flow-step {{ cursor: pointer; }}
                .flow-step::before {{ content: ""; position: absolute; left: 0; top: .55em; width: 9px; height: 9px; border-radius: 999px; background: rgba(148,163,184,.35); box-shadow: 0 0 0 5px rgba(148,163,184,.08); }}
                .flow-step.is-active {{ color: #e2e8f0; font-weight: 700; }}
                .flow-step.is-active::before {{ background: linear-gradient(135deg, #38bdf8, #22c55e); box-shadow: 0 0 0 5px rgba(56,189,248,.12); }}
                .flow-help {{ margin: 0; line-height: 1.5; }}
                .flow-card.hidden {{ display: none; }}
                .flow-link {{ margin-top: 12px; background: transparent; color: #93c5fd; border: 1px solid rgba(147,197,253,.22); }}
                .stepbtn {{ background: rgba(15, 23, 42, .68); color: #e2e8f0; border: 1px solid rgba(147,197,253,.22); }}
                .stepbtn.secondary {{ background: transparent; color: #93c5fd; }}
                .stepbtn.active {{ background: linear-gradient(135deg, rgba(56,189,248,.24), rgba(34,197,94,.22)); border-color: rgba(103, 232, 249, .42); }}
                label {{ display: grid; gap: 6px; font-size: 13px; color: #cbd5e1; margin-bottom: 12px; }}
                input, select, button {{ width: 100%; border-radius: 14px; border: 1px solid rgba(148, 163, 184, .2); background: #0f172a; color: #e5e7eb; padding: 12px 14px; font: inherit; }}
                input::placeholder {{ color: #64748b; }}
                button {{ cursor: pointer; background: linear-gradient(135deg, #38bdf8, #22c55e); color: #0f172a; font-weight: 800; border: none; transition: transform .15s ease, filter .15s ease; }}
                button:hover {{ transform: translateY(-1px); filter: brightness(1.02); }}
                .row {{ display: grid; gap: 10px; }}
                .status {{ padding: 12px 14px; border-radius: 14px; background: rgba(148, 163, 184, .09); color: #dbeafe; min-height: 44px; white-space: pre-wrap; }}
                .muted {{ color: #9ca3af; }}
                .list {{ display: grid; gap: 10px; margin-top: 10px; }}
                .item {{ background: rgba(255,255,255,.03); border: 1px solid rgba(148, 163, 184, .12); border-radius: 16px; padding: 12px; }}
                .topbar {{ display:flex; justify-content: space-between; align-items:center; gap:12px; margin-bottom: 16px; flex-wrap: wrap; }}
                .session-pill {{ display:inline-flex; align-items:center; gap:8px; padding: 10px 14px; border-radius: 999px; background: rgba(15,23,42,.78); border: 1px solid rgba(148,163,184,.18); color: #cbd5e1; }}
                .session-pill::before {{ content: ""; width: 8px; height: 8px; border-radius: 999px; background: #22c55e; box-shadow: 0 0 0 5px rgba(34,197,94,.12); }}
                .linkbtn {{ background: transparent; color: #93c5fd; border: 1px solid rgba(147,197,253,.25); padding: 10px 14px; width: auto; border-radius: 999px; }}
                .linkbtn[hidden] {{ display: none; }}
            </style>
        </head>
        <body>
            <main class="wrap">
                <section class="hero">
                    <div class="brand" style="display:inline-flex;align-items:center;gap:10px;padding:10px 14px;width:fit-content;border-radius:999px;background:rgba(15,23,42,.72);border:1px solid rgba(148,163,184,.18);box-shadow:0 20px 50px rgba(0,0,0,.18);">
                        <img src="/static/stockpulse-icon.svg" alt="StockPulse icon" style="width:30px;height:30px;" />
                        <span style="font-weight:800;letter-spacing:.02em;">StockPulse</span>
                    </div>
                    <div class="eyebrow">StockPulse AI</div>
                    <h1>{page_title}</h1>
                    <p class="sub">{page_subtitle}</p>
                </section>

                <div class="topbar">
                    <div id="session" class="session-pill">Guest mode</div>
                    <button class="linkbtn" id="auth-action" hidden>Logout</button>
                </div>

                <div class="grid">
                    {user_flow_nav}
                    {user_register_card}

                    {user_verify_card}

                    {user_login_card}

                    {user_dashboard_cards}

                    {admin_inventory_cards}
                    {admin_form}
                </div>
            </main>

            <script>
                const pageRole = {page_role!r};
                const tokenKey = "stockpulse_token";

                const sessionEl = document.getElementById("session");
                const authActionEl = document.getElementById("auth-action");
                const profileEl = document.getElementById("profile");
                const inventoryEl = document.getElementById("inventory");
                const adminUsersEl = document.getElementById("admin-users");
                const scanHistoryEl = document.getElementById("scan-history");
                const flowSteps = Array.from(document.querySelectorAll(".flow-step[data-step-target]"));
                const stepButtons = flowSteps;
                const flowCards = Array.from(document.querySelectorAll(".flow-card[data-step]"));
                const dashboardStep = document.querySelector('[data-step-target="dashboard"]');
                const registerShortcut = document.querySelector('[data-flow-action="login"]');
                const cameraVideo = document.getElementById("barcode-video");
                const startCameraButton = document.getElementById("start-camera");
                const stopCameraButton = document.getElementById("stop-camera");
                const cameraStatusEl = document.getElementById("camera-status");
                const scanPreviewEl = document.getElementById("scan-preview");
                let cameraStream = null;
                let cameraActive = false;
                let barcodeDetector = null;
                let barcodeFrame = 0;

                if (authActionEl) {{
                    authActionEl.addEventListener("click", () => {{
                        localStorage.removeItem(tokenKey);
                        authActionEl.hidden = true;
                        refreshSession();
                    }});
                }}

                function setFlowStep(step) {{
                    if (pageRole !== "user") return;
                    flowCards.forEach(card => {{
                        card.classList.toggle("hidden", card.dataset.step !== step);
                    }});
                    flowSteps.forEach(stepEl => {{
                        const target = stepEl.dataset.stepTarget;
                        stepEl.classList.toggle("is-active", target === step || (step === "dashboard" && target === "dashboard"));
                    }});
                    if (dashboardStep) dashboardStep.hidden = step !== "dashboard";
                }}

                function token() {{ return localStorage.getItem(tokenKey) || ""; }}
                function authHeaders() {{ return token() ? {{ Authorization: `Bearer ${{token()}}` }} : {{}}; }}
                function setStatus(message) {{ sessionEl.textContent = message; }}
                function saveToken(nextToken) {{ localStorage.setItem(tokenKey, nextToken); refreshSession(); }}

                async function fetchJson(url, options = {{}}) {{
                    const response = await fetch(url, options);
                    const payload = await response.json().catch(() => ({{}}));
                    if (!response.ok) throw new Error(payload.detail || "Request failed");
                    return payload;
                }}

                function renderInventory(items) {{
                    if (!inventoryEl) return;
                    inventoryEl.innerHTML = items.length ? items.map(item => `
                        <div class="item">
                            <strong>${{item.sku}}</strong> - ${{item.name}}<br>
                            Remaining: ${{item.total_remaining}} | Safety stock: ${{item.safety_stock}} | Status: ${{item.status}}
                        </div>
                    `).join("") : '<div class="muted">No inventory data yet.</div>';
                }}

                function renderUsers(items) {{
                    if (!adminUsersEl) return;
                    adminUsersEl.innerHTML = items.length ? items.map(item => `
                        <div class="item">
                            <strong>${{item.username}}</strong> - ${{item.email}}<br>
                            Role: ${{item.role}} | Verified: ${{item.is_verified}} | Active: ${{item.is_active}}
                        </div>
                    `).join("") : '<div class="muted">No users found.</div>';
                }}

                function escapeHtml(value) {{
                    return String(value)
                        .replaceAll("&", "&amp;")
                        .replaceAll("<", "&lt;")
                        .replaceAll(">", "&gt;")
                        .replaceAll('"', "&quot;")
                        .replaceAll("'", "&#39;");
                }}

                function renderScans(items) {{
                    scanHistoryEl.innerHTML = items.length ? items.map(item => `
                        <div class="item">
                            <strong>${{escapeHtml(item.product_sku)}}</strong> - ${{escapeHtml(item.product_name)}}<br>
                            Scan: ${{escapeHtml(item.scan_code)}} | Qty: ${{escapeHtml(item.quantity)}} | Remaining: ${{escapeHtml(item.quantity_remaining_snapshot)}} | At: ${{escapeHtml(item.captured_at)}}
                        </div>
                    `).join("") : '<div class="muted">No scans captured yet.</div>';
                }}

                async function previewScan(code) {{
                    if (!scanPreviewEl) return;
                    try {{
                        const preview = await fetchJson(`/scans/preview?scan_code=${{encodeURIComponent(code)}}`, {{ headers: authHeaders() }});
                        scanPreviewEl.className = "item";
                        scanPreviewEl.innerHTML = `
                            <strong>${{escapeHtml(preview.name)}}</strong><br>
                            SKU: ${{escapeHtml(preview.sku)}}<br>
                            Category: ${{escapeHtml(preview.category || "-")}}<br>
                            Remaining: ${{escapeHtml(preview.quantity_remaining_snapshot)}} | Safety stock: ${{escapeHtml(preview.safety_stock)}} | Status: ${{escapeHtml(preview.inventory_status_snapshot)}}
                        `;
                    }} catch (error) {{
                        scanPreviewEl.className = "item muted";
                        scanPreviewEl.textContent = error.message;
                    }}
                }}

                async function stopCamera() {{
                    cameraActive = false;
                    if (barcodeFrame) cancelAnimationFrame(barcodeFrame);
                    barcodeFrame = 0;
                    if (cameraStream) {{
                        cameraStream.getTracks().forEach(track => track.stop());
                        cameraStream = null;
                    }}
                    if (cameraVideo) cameraVideo.srcObject = null;
                    if (cameraStatusEl) cameraStatusEl.textContent = "Camera is idle.";
                    if (startCameraButton) startCameraButton.disabled = false;
                }}

                async function handleBarcode(code) {{
                    const scanInput = document.getElementById("scan_code");
                    if (scanInput) scanInput.value = code;
                    if (cameraStatusEl) cameraStatusEl.textContent = `Detected barcode: ${{code}}`;
                    await previewScan(code);
                    await stopCamera();
                    if (pageRole === "user") setFlowStep("dashboard");
                }}

                async function startCamera() {{
                    if (!cameraVideo || !cameraStatusEl || !startCameraButton) return;
                    if (!navigator.mediaDevices?.getUserMedia) {{
                        cameraStatusEl.textContent = "Camera capture is not supported in this browser.";
                        return;
                    }}
                    try {{
                        cameraStream = await navigator.mediaDevices.getUserMedia({{ video: {{ facingMode: {{ ideal: "environment" }} }}, audio: false }});
                        cameraVideo.srcObject = cameraStream;
                        await cameraVideo.play();
                        cameraActive = true;
                        startCameraButton.disabled = true;
                        cameraStatusEl.textContent = "Point the camera at a barcode.";

                        if ("BarcodeDetector" in window) {{
                            barcodeDetector = new BarcodeDetector({{ formats: ["code_128", "ean_13", "ean_8", "qr_code", "upc_a", "upc_e"] }});
                            const scanFrame = async () => {{
                                if (!cameraActive || !barcodeDetector || !cameraVideo) return;
                                try {{
                                    const codes = await barcodeDetector.detect(cameraVideo);
                                    if (codes.length) {{
                                        await handleBarcode(codes[0].rawValue);
                                        return;
                                    }}
                                }} catch (error) {{
                                    if (cameraStatusEl) cameraStatusEl.textContent = error.message;
                                }}
                                barcodeFrame = requestAnimationFrame(scanFrame);
                            }};
                            barcodeFrame = requestAnimationFrame(scanFrame);
                        }} else {{
                            cameraStatusEl.textContent = "BarcodeDetector is not available here. You can still type the code manually after pointing the camera at the barcode.";
                        }}
                    }} catch (error) {{
                        cameraStatusEl.textContent = error.message;
                        await stopCamera();
                    }}
                }}

                async function refreshSession() {{
                    const currentToken = token();
                    if (!currentToken) {{
                        sessionEl.textContent = "Guest mode";
                        if (authActionEl) {{
                            authActionEl.hidden = true;
                            authActionEl.textContent = "Logout";
                        }}
                        if (profileEl) profileEl.textContent = "Load a token to view profile details.";
                        if (inventoryEl) inventoryEl.innerHTML = "";
                        if (adminUsersEl) adminUsersEl.innerHTML = "";
                        if (scanHistoryEl) scanHistoryEl.innerHTML = "";
                        if (dashboardStep) dashboardStep.hidden = true;
                        if (pageRole === "user") setFlowStep("register");
                        return;
                    }}

                    try {{
                        const me = await fetchJson("/auth/me", {{ headers: authHeaders() }});
                        setStatus(`Signed in as ${{me.username}} (${{me.role}}${{me.is_verified ? ", verified" : ", unverified"}})`);
                        if (authActionEl) {{
                            authActionEl.hidden = false;
                            authActionEl.textContent = "Logout";
                        }}
                        if (dashboardStep) dashboardStep.hidden = false;
                        if (profileEl) profileEl.textContent = JSON.stringify(me, null, 2);
                        if (pageRole === "admin" && me.role === "admin") {{
                            const inventory = await fetchJson("/inventory", {{ headers: authHeaders() }});
                            renderInventory(inventory.items || []);
                            const users = await fetchJson("/admin/users", {{ headers: authHeaders() }});
                            renderUsers(users.items || []);
                            const scans = await fetchJson("/admin/scans", {{ headers: authHeaders() }});
                            renderScans(scans.items || []);
                        }} else if (pageRole === "admin") {{
                            if (adminUsersEl) adminUsersEl.innerHTML = '<div class="muted">Admin access required.</div>';
                            if (scanHistoryEl) scanHistoryEl.innerHTML = '<div class="muted">Admin access required.</div>';
                        }} else {{
                            setFlowStep("dashboard");
                            if (inventoryEl) inventoryEl.innerHTML = '<div class="muted">Inventory data is restricted to admins.</div>';
                            const scans = await fetchJson("/scans/me", {{ headers: authHeaders() }});
                            renderScans(scans.items || []);
                        }}
                    }} catch (error) {{
                        setStatus(error.message);
                        profileEl.textContent = error.message;
                    }}
                }}

                document.getElementById("verify-form").addEventListener("submit", async (event) => {{
                    event.preventDefault();
                    try {{
                        const result = await fetchJson("/auth/verify-otp", {{
                            method: "POST",
                            headers: {{ "Content-Type": "application/json" }},
                            body: JSON.stringify({{
                                email: document.getElementById("otp_email").value,
                                otp_code: document.getElementById("otp_code").value,
                            }}),
                        }});
                        saveToken(result.access_token);
                        setStatus("OTP verified.");
                    }} catch (error) {{ setStatus(error.message); }}
                }});

                document.getElementById("login-form").addEventListener("submit", async (event) => {{
                    event.preventDefault();
                    try {{
                        const result = await fetchJson("/auth/token", {{
                            method: "POST",
                            headers: {{ "Content-Type": "application/json" }},
                            body: JSON.stringify({{
                                identifier: document.getElementById("login_identifier").value,
                                password: document.getElementById("login_password").value,
                            }}),
                        }});
                        saveToken(result.access_token);
                        setStatus("Signed in.");
                    }} catch (error) {{ setStatus(error.message); }}
                }});

                if (registerShortcut) {{
                    registerShortcut.addEventListener("click", () => setFlowStep("login"));
                }}

                const scanForm = document.getElementById("scan-form");
                if (scanForm) {{
                    scanForm.addEventListener("submit", async (event) => {{
                        event.preventDefault();
                        try {{
                            const result = await fetchJson("/scans", {{
                                method: "POST",
                                headers: {{ "Content-Type": "application/json", ...authHeaders() }},
                                body: JSON.stringify({{
                                    scan_code: document.getElementById("scan_code").value,
                                    quantity: Number(document.getElementById("scan_qty").value || 1),
                                }}),
                            }});
                            setStatus(result.message || "Scan captured.");
                            await previewScan(document.getElementById("scan_code").value);
                            refreshSession();
                        }} catch (error) {{ setStatus(error.message); }}
                    }});
                }}

                const registerForm = document.getElementById("register-form");
                if (registerForm) {{
                    registerForm.addEventListener("submit", async (event) => {{
                        event.preventDefault();
                        try {{
                            const body = {{
                                username: document.getElementById("reg_username").value,
                                email: document.getElementById("reg_email").value,
                                password: document.getElementById("reg_password").value,
                                supplier_id: null,
                            }};

                            const result = await fetchJson("/auth/register", {{
                                method: "POST",
                                headers: {{ "Content-Type": "application/json" }},
                                body: JSON.stringify(body),
                            }});
                            setStatus(result.message || "OTP sent. Check your email.");
                            setFlowStep("verify");
                        }} catch (error) {{ setStatus(error.message); }}
                    }});
                }}

                stepButtons.forEach(button => {{
                    button.addEventListener("click", () => setFlowStep(button.dataset.stepTarget));
                }});

                const adminCreateForm = document.getElementById("admin-create-form");
                if (adminCreateForm) {{
                    adminCreateForm.addEventListener("submit", async (event) => {{
                        event.preventDefault();
                        try {{
                            const result = await fetchJson("/admin/users", {{
                                method: "POST",
                                headers: {{ "Content-Type": "application/json", ...authHeaders() }},
                                body: JSON.stringify({{
                                    username: document.getElementById("admin_new_username").value,
                                    email: document.getElementById("admin_new_email").value,
                                    password: document.getElementById("admin_new_password").value,
                                    role: document.getElementById("admin_new_role").value,
                                    supplier_id: document.getElementById("admin_new_supplier_id").value || null,
                                }}),
                            }});
                            setStatus(result.message || "Account created.");
                        }} catch (error) {{ setStatus(error.message); }}
                    }});
                }}

                if (pageRole === "user") setFlowStep(token() ? "dashboard" : "register");
                if (startCameraButton) startCameraButton.addEventListener("click", startCamera);
                if (stopCameraButton) stopCameraButton.addEventListener("click", stopCamera);
                refreshSession();
            </script>
        </body>
        </html>
        """


@app.get("/", response_class=HTMLResponse)
def landing_page():
    return HTMLResponse(render_landing_page())


def render_supplier_dashboard() -> str:
        return """
        <!doctype html>
        <html lang="en">
        <head>
            <meta charset="utf-8" />
            <meta name="viewport" content="width=device-width, initial-scale=1" />
            <title>Supplier Dashboard</title>
            <link rel="icon" type="image/svg+xml" href="/static/stockpulse-icon.svg" />
            <style>
                body { margin: 0; font-family: Arial, Helvetica, sans-serif; background: linear-gradient(180deg, #020617 0%, #111827 100%); color: #e5e7eb; }
                .wrap { max-width: 1120px; margin: 0 auto; padding: 28px 18px 56px; }
                .hero { display: grid; gap: 12px; margin-bottom: 24px; }
                .brand { display: inline-flex; align-items: center; gap: 10px; padding: 10px 14px; width: fit-content; border-radius: 999px; background: rgba(15,23,42,.72); border: 1px solid rgba(148,163,184,.18); box-shadow: 0 20px 50px rgba(0,0,0,.18); }
                .brand img { width: 30px; height: 30px; }
                .brand span { font-weight: 800; letter-spacing: .02em; }
                .eyebrow { color: #38bdf8; text-transform: uppercase; letter-spacing: .16em; font-size: 12px; font-weight: 700; }
                h1 { margin: 0; font-size: clamp(2rem, 4vw, 3.2rem); line-height: 1.05; }
                .sub { color: #9ca3af; max-width: 760px; line-height: 1.6; }
                .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap: 16px; }
                .card { background: rgba(17, 24, 39, .92); border: 1px solid rgba(148, 163, 184, .14); border-radius: 18px; padding: 18px; box-shadow: 0 24px 60px rgba(0,0,0,.22); }
                .card h2 { margin: 0 0 12px; font-size: 1.05rem; }
                .status { padding: 10px 12px; border-radius: 12px; background: rgba(148, 163, 184, .08); color: #cbd5e1; min-height: 44px; white-space: pre-wrap; }
                .item { background: rgba(255,255,255,.03); border: 1px solid rgba(148, 163, 184, .12); border-radius: 14px; padding: 12px; margin-top: 10px; }
                .topbar { display:flex; justify-content: space-between; align-items:center; gap:12px; margin-bottom: 16px; }
                .linkbtn { background: transparent; color: #93c5fd; border: 1px solid rgba(147,197,253,.25); padding: 10px 12px; width: auto; border-radius: 12px; }
                input, button { width: 100%; border-radius: 12px; border: 1px solid rgba(148, 163, 184, .2); background: #0f172a; color: #e5e7eb; padding: 12px 14px; font: inherit; }
                button { cursor: pointer; background: linear-gradient(135deg, #38bdf8, #22c55e); color: #0f172a; font-weight: 800; border: none; }
                label { display:grid; gap:6px; font-size:13px; color:#cbd5e1; margin-bottom: 12px; }
            </style>
        </head>
        <body>
            <main class="wrap">
                <section class="hero">
                    <div class="brand"><img src="/static/stockpulse-icon.svg" alt="StockPulse icon" /><span>StockPulse</span></div>
                    <div class="eyebrow">StockPulse AI</div>
                    <h1>Supplier Dashboard</h1>
                    <p class="sub">Suppliers only see their own stock movement and replenishment signals. Accounts are provisioned by an admin.</p>
                </section>

                <div class="topbar">
                    <div id="session" class="status">No active session.</div>
                    <button class="linkbtn" id="logout">Logout</button>
                </div>

                <div class="grid">
                    <section class="card">
                        <h2>Login</h2>
                        <form id="login-form">
                            <label>Email or username<input id="login_identifier" required></label>
                            <label>Password<input id="login_password" type="password" required></label>
                            <button type="submit">Sign in</button>
                        </form>
                    </section>

                    <section class="card">
                        <h2>Profile</h2>
                        <div id="profile" class="status">Load a token to view profile details.</div>
                    </section>

                    <section class="card">
                        <h2>Supplier movement</h2>
                        <div id="movement"></div>
                    </section>

                    <section class="card">
                        <h2>Scan product</h2>
                        <form id="scan-form">
                            <label>Scan code or SKU<input id="scan_code" required></label>
                            <label>Quantity<input id="scan_qty" type="number" min="1" value="1" required></label>
                            <button type="submit">Capture scan</button>
                        </form>
                    </section>

                    <section class="card">
                        <h2>Recent scans</h2>
                        <div id="scans"></div>
                    </section>
                </div>
            </main>

            <script>
                const tokenKey = "stockpulse_token";
                const sessionEl = document.getElementById("session");
                const profileEl = document.getElementById("profile");
                const movementEl = document.getElementById("movement");
                const scansEl = document.getElementById("scans");

                function token() { return localStorage.getItem(tokenKey) || ""; }
                function authHeaders() { return token() ? { Authorization: `Bearer ${token()}` } : {}; }
                function saveToken(nextToken) { localStorage.setItem(tokenKey, nextToken); refreshSession(); }
                function escapeHtml(value) {
                    return String(value)
                        .replaceAll("&", "&amp;")
                        .replaceAll("<", "&lt;")
                        .replaceAll(">", "&gt;")
                        .replaceAll('"', "&quot;")
                        .replaceAll("'", "&#39;");
                }
                async function fetchJson(url, options = {}) {
                    const response = await fetch(url, options);
                    const payload = await response.json().catch(() => ({}));
                    if (!response.ok) throw new Error(payload.detail || "Request failed");
                    return payload;
                }

                function renderMovement(items) {
                    movementEl.innerHTML = items.length ? items.map(item => `
                        <div class="item">
                            <strong>${item.sku}</strong> - ${item.name}<br>
                            Remaining: ${item.total_remaining} | Movement 14d: ${item.sales_last_14_days} | Status: ${item.status}
                        </div>
                    `).join("") : '<div class="status">No supplier movement available.</div>';
                }

                function renderScans(items) {
                    scansEl.innerHTML = items.length ? items.map(item => `
                        <div class="item">
                            <strong>${escapeHtml(item.product_sku)}</strong> - ${escapeHtml(item.product_name)}<br>
                            Scan: ${escapeHtml(item.scan_code)} | Qty: ${escapeHtml(item.quantity)} | Remaining: ${escapeHtml(item.quantity_remaining_snapshot)}
                        </div>
                    `).join("") : '<div class="status">No scans captured yet.</div>';
                }

                async function refreshSession() {
                    const currentToken = token();
                    if (!currentToken) {
                        sessionEl.textContent = "No active session.";
                        profileEl.textContent = "Load a token to view profile details.";
                        movementEl.innerHTML = "";
                        return;
                    }

                    try {
                        const me = await fetchJson("/auth/me", { headers: authHeaders() });
                        sessionEl.textContent = `Signed in as ${me.username} (${me.role}${me.is_verified ? ", verified" : ", unverified"})`;
                        profileEl.textContent = JSON.stringify(me, null, 2);
                        if (me.role === "supplier") {
                            const movement = await fetchJson("/supplier/movement", { headers: authHeaders() });
                            renderMovement(movement.items || []);
                            const scans = await fetchJson("/scans/me", { headers: authHeaders() });
                            renderScans(scans.items || []);
                        } else {
                            movementEl.innerHTML = '<div class="status">Supplier access required.</div>';
                        }
                    } catch (error) {
                        sessionEl.textContent = error.message;
                        profileEl.textContent = error.message;
                    }
                }

                document.getElementById("login-form").addEventListener("submit", async (event) => {
                    event.preventDefault();
                    try {
                        const result = await fetchJson("/auth/token", {
                            method: "POST",
                            headers: { "Content-Type": "application/json" },
                            body: JSON.stringify({
                                identifier: document.getElementById("login_identifier").value,
                                password: document.getElementById("login_password").value,
                            }),
                        });
                        saveToken(result.access_token);
                    } catch (error) {
                        sessionEl.textContent = error.message;
                    }
                });

                document.getElementById("logout").addEventListener("click", () => {
                    localStorage.removeItem(tokenKey);
                    refreshSession();
                });

                document.getElementById("scan-form").addEventListener("submit", async (event) => {
                    event.preventDefault();
                    try {
                        const result = await fetchJson("/scans", {
                            method: "POST",
                            headers: { "Content-Type": "application/json", ...authHeaders() },
                            body: JSON.stringify({
                                scan_code: document.getElementById("scan_code").value,
                                quantity: Number(document.getElementById("scan_qty").value || 1),
                            }),
                        });
                        sessionEl.textContent = result.message || "Scan captured.";
                        refreshSession();
                    } catch (error) {
                        sessionEl.textContent = error.message;
                    }
                });

                refreshSession();
            </script>
        </body>
        </html>
        """


def render_dashboard(items: list[dict]) -> str:
    from presentation import render_dashboard as render_dashboard_impl

    return render_dashboard_impl(items)


def render_landing_page() -> str:
    from presentation import render_landing_page as render_landing_page_impl

    return render_landing_page_impl()


def render_portal_page(page_role: str) -> str:
    from presentation import render_portal_page as render_portal_page_impl

    return render_portal_page_impl(page_role)


def render_supplier_dashboard() -> str:
    from presentation import render_supplier_dashboard as render_supplier_dashboard_impl

    return render_supplier_dashboard_impl()


def render_quality_dashboard(items: list[dict]) -> str:
    from presentation import render_quality_dashboard as render_quality_dashboard_impl

    return render_quality_dashboard_impl(items)


def cache_get_json(key: str):
    from repositories.inventory_repository import cache_get_json as cache_get_json_impl

    return cache_get_json_impl(key)


def cache_set_json(key: str, value, ttl_seconds: int = CACHE_TTL_SECONDS):
    from repositories.inventory_repository import cache_set_json as cache_set_json_impl

    return cache_set_json_impl(key, value, ttl_seconds=ttl_seconds)


def cache_version() -> str:
    from repositories.inventory_repository import cache_version as cache_version_impl

    return cache_version_impl()


def bump_cache_version():
    from repositories.inventory_repository import bump_cache_version as bump_cache_version_impl

    return bump_cache_version_impl()


def batch_status(batch: Batch) -> str:
    from repositories.inventory_repository import batch_status as batch_status_impl

    return batch_status_impl(batch)


def inventory_status(total_remaining: int, safety_stock: int) -> str:
    from repositories.inventory_repository import inventory_status as inventory_status_impl

    return inventory_status_impl(total_remaining, safety_stock)


def inventory_row(product: Product, batches: list[Batch]) -> dict:
    from repositories.inventory_repository import inventory_row as inventory_row_impl

    return inventory_row_impl(product, batches)


def get_inventory_items(db: Session) -> list[dict]:
    from repositories.inventory_repository import get_inventory_items as get_inventory_items_impl

    return get_inventory_items_impl(db)


def send_verification_email(recipient_email: str, otp_code: str):
    from services.email_service import send_verification_email as send_verification_email_impl

    return send_verification_email_impl(
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
@app.post("/auth/register", response_model=AuthMessage)
def register_user(payload: UserCreate, background_tasks: BackgroundTasks, db: Session = Depends(get_db)):
    user, otp_code = create_pending_user(db, payload, send_email=False)
    background_tasks.add_task(send_verification_email, user.email, otp_code)
    return AuthMessage(message=f"Verification code sent to {user.email}")


@app.post("/auth/token", response_model=Token)
def login_for_access_token(payload: UserLogin, db: Session = Depends(get_db)):
    user = authenticate_user(db, payload.identifier, payload.password)
    if not user:
        raise HTTPException(status_code=401, detail="Incorrect username or password", headers={"WWW-Authenticate": "Bearer"})
    return create_user_token(db, user)


@app.post("/auth/verify-otp", response_model=Token)
def verify_otp(payload: OtpVerifyRequest, db: Session = Depends(get_db)):
    user = verify_user_otp(db, payload.email, payload.otp_code)
    return create_user_token(db, user)


@app.post("/admin/users", response_model=AuthMessage)
def create_admin_account(payload: AdminUserCreate, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    require_admin(current_user)
    user = create_admin_user(db, payload)
    return AuthMessage(message=f"Created {user.role} account for {user.email}")


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
def preview_scan(scan_code: str, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    require_role(current_user, {"user", "admin", "supplier"})
    product = resolve_scan_product(db, scan_code)
    if current_user.role == "supplier" and current_user.supplier_id != product.supplier_id:
        raise HTTPException(status_code=403, detail="Supplier can only preview own products")
    return build_scan_preview(db, product)


@app.post("/scans", response_model=ScanCaptureResponse)
def capture_scan(payload: ScanCreate, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    scan = record_product_scan(db, current_user, payload)
    return ScanCaptureResponse(
        message=f"Captured scan for {scan.product_sku}",
        product_id=scan.product_id,
        product_sku=scan.product_sku,
        quantity_remaining_snapshot=scan.quantity_remaining_snapshot,
    )


@app.get("/scans/me")
def my_scans(db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    scans = (
        db.query(ProductScan)
        .filter(ProductScan.scanned_by_user_id == current_user.id)
        .order_by(ProductScan.captured_at.desc())
        .limit(20)
        .all()
    )
    return {"count": len(scans), "items": [serialize_scan(scan) for scan in scans]}


@app.get("/admin/scans")
def all_scans(db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    require_admin(current_user)
    scans = db.query(ProductScan).order_by(ProductScan.captured_at.desc()).limit(100).all()
    return {"count": len(scans), "items": [serialize_scan(scan) for scan in scans]}


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


@app.get("/admin/users")
def list_users(db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    require_admin(current_user)
    users = db.query(User).order_by(User.id.asc()).all()
    return {
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


@app.post("/suppliers")
def create_supplier(s: SupplierCreate, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    require_admin(current_user)
    supplier = Supplier(name=s.name, contact_email=s.contact_email, lead_time_days=s.lead_time_days)
    db.add(supplier)
    db.commit()
    db.refresh(supplier)
    bump_cache_version()
    return supplier


@app.post("/products")
def create_product(p: ProductCreate, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    require_admin(current_user)
    product = Product(sku=p.sku, name=p.name, category=p.category, safety_stock=p.safety_stock, supplier_id=p.supplier_id)
    db.add(product)
    db.commit()
    db.refresh(product)
    bump_cache_version()
    return product


@app.post("/batches")
def create_batch(b: BatchCreate, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
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
def record_sale(s: SaleCreate, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
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
        # not enough stock
        db.rollback()
        raise HTTPException(status_code=400, detail="Not enough stock to fulfill sale")

    sale = Sale(product_id=s.product_id, quantity=s.quantity, timestamp=utc_now())
    db.add(sale)
    db.commit()
    db.refresh(sale)
    bump_cache_version()
    return {"sale_id": sale.id}


@app.get("/products/{product_id}/rop")
def product_rop(product_id: int, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    require_role(current_user, {"admin", "supplier"})
    product = db.query(Product).filter(Product.id == product_id).first()
    if not product:
        raise HTTPException(status_code=404, detail="Product not found")
    if current_user.role == "supplier" and current_user.supplier_id != product.supplier_id:
        raise HTTPException(status_code=403, detail="Supplier can only access own products")

    # Get sales for last 14 days for velocity (example)
    days_window = 14
    cutoff = utc_now() - timedelta(days=days_window)
    sales = db.query(Sale).filter(Sale.product_id == product_id, Sale.timestamp >= cutoff).all()
    sales_tuples = [(s.timestamp, s.quantity) for s in sales]
    d = compute_velocity(sales_tuples, days_window=days_window)

    # supplier lead time
    # load supplier lead time if available
    L = 7
    if product.supplier_id:
        supplier = db.query(Supplier).filter(Supplier.id == product.supplier_id).first()
        if supplier:
            L = supplier.lead_time_days
    SS = product.safety_stock or 0
    rop = compute_rop(d, L, SS)
    return {"d": d, "L": L, "SS": SS, "ROP": rop}


@app.get("/inventory")
def inventory(db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    require_admin(current_user)
    items = get_inventory_items(db)
    return {"count": len(items), "items": items}


@app.get("/health")
def health_check():
    return {"status": "ok"}


@app.get("/dashboard", response_class=HTMLResponse)
def dashboard_page(db: Session = Depends(get_db)):
    return HTMLResponse(render_dashboard(get_inventory_items(db)))


@app.get("/qa", response_class=HTMLResponse)
def qa_page(db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    require_admin(current_user)
    return HTMLResponse(render_quality_dashboard(get_inventory_items(db)))


@app.get("/products/{product_id}/batches")
def product_batches(product_id: int, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
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


@app.get("/supplier/movement")
def supplier_movement(db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    require_role(current_user, {"supplier"})
    if current_user.supplier_id is None:
        raise HTTPException(status_code=403, detail="Supplier account is not linked to a supplier")

    products = db.query(Product).filter(Product.supplier_id == current_user.supplier_id).all()
    items = []
    window_start = utc_now() - timedelta(days=14)

    for product in products:
        batches = db.query(Batch).filter(Batch.product_id == product.id).all()
        sales = db.query(Sale).filter(Sale.product_id == product.id, Sale.timestamp >= window_start).all()
        total_remaining = sum(batch.quantity_remaining for batch in batches)
        items.append({
            "product_id": product.id,
            "sku": product.sku,
            "name": product.name,
            "total_remaining": total_remaining,
            "sales_last_14_days": sum(sale.quantity for sale in sales),
            "status": inventory_status(total_remaining, product.safety_stock or 0),
        })

    return {"count": len(items), "items": items}
