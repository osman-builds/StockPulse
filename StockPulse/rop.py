from datetime import datetime, timedelta, timezone
from typing import List, Tuple


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def compute_velocity(sales: List[Tuple[datetime, int]], days_window: int = 7) -> float:
    """
    Compute average daily sales velocity over the last `days_window` days.
    `sales` is a list of (timestamp, quantity) tuples.
    """
    if days_window <= 0:
        raise ValueError("days_window must be > 0")
    cutoff = _utc_now() - timedelta(days=days_window)
    total = 0
    for ts, qty in sales:
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        if ts >= cutoff:
            total += qty
    return total / days_window


def compute_rop(d: float, L: float, SS: float) -> float:
    """Return Reorder Point using ROP = (d * L) + SS"""
    return (d * L) + SS
