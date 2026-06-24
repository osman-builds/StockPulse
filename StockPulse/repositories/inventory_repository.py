from datetime import date
import json
import os

import redis
from sqlalchemy.orm import Session

from models import Batch, Product


CACHE_TTL_SECONDS = 30
CACHE_VERSION_KEY = "stockpulse:cache:version"
CACHE_PREFIX = "stockpulse"

_redis_client = None


def get_redis_client():
    global _redis_client
    if _redis_client is False:
        return None
    if _redis_client is None:
        redis_url = os.getenv("STOCKPULSE_REDIS_URL", "redis://redis:6379/0")
        try:
            _redis_client = redis.Redis.from_url(redis_url, decode_responses=True)
        except Exception:
            _redis_client = False
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
    cached = client.get(key)
    if not cached:
        return None
    return json.loads(cached)


def cache_set_json(key: str, value, ttl_seconds: int = CACHE_TTL_SECONDS):
    client = get_redis_client()
    if client is None:
        return
    client.setex(key, ttl_seconds, json.dumps(value, default=str))


def cache_version() -> str:
    client = get_redis_client()
    if client is None:
        return "local"
    version = client.get(CACHE_VERSION_KEY)
    if not version:
        version = "1"
        client.set(CACHE_VERSION_KEY, version)
    return version.decode() if isinstance(version, bytes) else str(version)


def bump_cache_version():
    client = get_redis_client()
    if client is None:
        return
    client.incr(CACHE_VERSION_KEY)


def inventory_status(total_remaining: int, safety_stock: int) -> str:
    if total_remaining <= 0:
        return "out_of_stock"
    if total_remaining <= safety_stock:
        return "low_stock"
    return "healthy"


def inventory_row(product: Product, batches: list[Batch]) -> dict:
    total_received = sum(batch.quantity_received or 0 for batch in batches)
    total_remaining = sum(batch.quantity_remaining or 0 for batch in batches)
    next_expiry = None
    for batch in batches:
        if batch.expiry_date is not None:
            next_expiry = batch.expiry_date
            break
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