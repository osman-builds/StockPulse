from sqlalchemy.orm import Session

from models import Batch


def lock_fefo_batches(db: Session, product_id: int) -> list[Batch]:
    return (
        db.query(Batch)
        .filter(Batch.product_id == product_id, Batch.quantity_remaining > 0)
        .order_by(Batch.expiry_date.asc().nulls_last())
        .with_for_update()
        .all()
    )
