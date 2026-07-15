"""
Sale processing service — FEFO (First-Expired, First-Out) batch deduction.

This is the single canonical implementation. The now-deleted sale_service.py was
a duplicate that wrote timestamp=None to the sales table, breaking ROP calculations.
"""
from datetime import datetime, timezone

from fastapi import HTTPException
from sqlalchemy.orm import Session

from models import Batch, Sale
from repositories.sales_repository import lock_fefo_batches


def process_sale(db: Session, product_id: int, quantity: int) -> int:
    """Deduct stock FEFO, record a Sale, return the sale ID."""
    qty_to_deduct = quantity
    batches = lock_fefo_batches(db, product_id)
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

    sale = Sale(
        product_id=product_id,
        quantity=quantity,
        timestamp=datetime.now(timezone.utc),  # was None — broke ROP velocity
    )
    db.add(sale)
    db.commit()
    db.refresh(sale)
    return sale.id
