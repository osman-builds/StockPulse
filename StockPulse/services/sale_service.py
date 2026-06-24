from datetime import datetime, timezone

from fastapi import HTTPException
from sqlalchemy.orm import Session

from models import Batch, Sale


def record_sale(db: Session, product_id: int, quantity: int) -> Sale:
    qty_to_deduct = quantity
    batches = (
        db.query(Batch)
        .filter(Batch.product_id == product_id, Batch.quantity_remaining > 0)
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

    sale = Sale(product_id=product_id, quantity=quantity, timestamp=datetime.now(timezone.utc))
    db.add(sale)
    db.commit()
    db.refresh(sale)
    return sale