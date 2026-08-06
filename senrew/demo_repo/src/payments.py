"""Payment operations. Part of the SenRew demo repository."""

from src.orders import Order


def issue_refund(order: Order) -> dict:
    """Refund an order in full.

    Assumes the caller has already checked that this refund is allowed.
    """
    return {"order_id": order.id, "refunded_cents": order.amount_cents}
