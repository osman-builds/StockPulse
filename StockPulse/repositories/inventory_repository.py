from collections import defaultdict
from datetime import date
import json
import os
import time

import redis
from sqlalchemy import func
from sqlalchemy.orm import Session

from models import Batch, Product


CACHE_TTL_SECONDS = int(os.getenv("STOCKPULSE_CACHE_TTL_SECONDS", "30"))
CACHE_VERSION_KEY = "stockpulse:cache:version"
CACHE_PREFIX = "stockpulse"

# Redis connection state — uses timestamp-based retry instead of permanent False flag
_redis_client: "redis.Redis | None" = None
_redis_client_failed: bool = False
_redis_last_failed: float = 0.0
REDIS_RETRY_AFTER_SECONDS = 30


def get_redis_client() -> "redis.Redis | None":
    global _redis_client, _redis_client_failed, _redis_last_failed

    # If we recently failed, check if retry window has passed
    if _redis_client_failed:
        if time.monotonic() - _redis_last_failed < REDIS_RETRY_AFTER_SECONDS:
            return None
        # Retry window passed — reset and attempt reconnection
        _redis_client_failed = False
        _redis_client = None

    if _redis_client is None:
        redis_url = os.getenv("STOCKPULSE_REDIS_URL", "redis://redis:6379/0")
        try:
            client = redis.Redis.from_url(
                redis_url,
                decode_responses=True,
                socket_connect_timeout=1,
                socket_timeout=1,
            )
            client.ping()
            _redis_client = client
        except Exception:
            _redis_client_failed = True
            _redis_last_failed = time.monotonic()
            return None

    return _redis_client


def batch_status(batch: Batch) -> str:
    today = date.today()
    if getattr(batch, "quantity_remaining", 0) <= 0:
        return "depleted"
    expiry = getattr(batch, "expiry_date", None)
    if expiry is None:
        return "active"
    if expiry < today:
        return "expired"
    if (expiry - today).days <= 30:
        return "expiring_soon"
    return "active"


def cache_get_json(key: str):
    client = get_redis_client()
    if client is None:
        return None
    try:
        cached = client.get(key)
        return json.loads(cached) if cached else None
    except Exception:
        return None


def cache_set_json(key: str, value, ttl_seconds: int = CACHE_TTL_SECONDS):
    client = get_redis_client()
    if client is None:
        return
    try:
        client.setex(key, ttl_seconds, json.dumps(value, default=str))
    except Exception:
        return


def cache_version() -> str:
    client = get_redis_client()
    if client is None:
        return "local"
    try:
        version = client.get(CACHE_VERSION_KEY)
        if not version:
            client.set(CACHE_VERSION_KEY, "1")
            return "1"
        return str(version)
    except Exception:
        return "local"


def bump_cache_version():
    client = get_redis_client()
    if client is None:
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
    total_received = sum(batch.quantity_received or 0 for batch in batches)
    total_remaining = sum(batch.quantity_remaining or 0 for batch in batches)
    next_expiry = next(
        (batch.expiry_date for batch in batches if batch.expiry_date is not None),
        None,
    )
    return {
        "product_id": product.id,
        "sku": product.sku,
        "name": product.name,
        "category": product.category,
        "safety_stock": product.safety_stock or 0,
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
    if not products:
        return []

    product_ids = [p.id for p in products]

    # Single query for all batches — avoids N+1
    all_batches = (
        db.query(Batch)
        .filter(Batch.product_id.in_(product_ids))
        .order_by(Batch.product_id, Batch.expiry_date.asc().nulls_last())
        .all()
    )

    batches_by_product: dict[int, list[Batch]] = defaultdict(list)
    for batch in all_batches:
        batches_by_product[batch.product_id].append(batch)

    items = [inventory_row(product, batches_by_product[product.id]) for product in products]
    cache_set_json(cache_key, items)
    return items