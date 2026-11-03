from datetime import datetime, timedelta

from sqlalchemy import desc, func, select
from sqlalchemy.orm import Session

from app.config import settings
from app.models import Product, Sale


def get_reference_date(db: Session) -> datetime:
    """REFERENCE_DATE, ou a data da venda mais recente (os dados de exemplo são de 2025)."""
    if settings.reference_date:
        return settings.reference_date
    latest = db.scalar(select(func.max(Sale.sale_date)))
    return latest or datetime.now()


def get_top_products(db: Session, days: int = 30, limit: int = 5) -> dict:
    end = get_reference_date(db)
    start = end - timedelta(days=days)

    total_quantity = func.sum(Sale.quantity).label("total_quantity")
    total_revenue = func.sum(Sale.total_amount).label("total_revenue")

    stmt = (
        select(Product.id, Product.sku, Product.name, Product.category, total_quantity, total_revenue)
        .join(Sale, Sale.product_id == Product.id)
        .where(Sale.sale_date > start, Sale.sale_date <= end)
        .group_by(Product.id, Product.sku, Product.name, Product.category)
        .order_by(desc(total_quantity), desc(total_revenue))
        .limit(limit)
    )

    products = [
        {
            "rank": position,
            "product_id": row.id,
            "sku": row.sku,
            "name": row.name,
            "category": row.category,
            "total_quantity": int(row.total_quantity),
            "total_revenue": float(row.total_revenue),
        }
        for position, row in enumerate(db.execute(stmt), start=1)
    ]

    return {
        "period_start": start.isoformat(sep=" "),
        "period_end": end.isoformat(sep=" "),
        "products": products,
    }
