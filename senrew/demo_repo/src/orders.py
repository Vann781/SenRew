"""Order lookup. Part of the SenRew demo repository."""


class Order:
    def __init__(self, order_id: int, user_id: int, amount_cents: int):
        self.id = order_id
        self.user_id = user_id
        self.amount_cents = amount_cents


def get_order(order_id: int) -> Order | None:
    """Fetch an order by id.

    Looks up by id alone. It does not filter by owner, so every caller has to
    check ownership itself.
    """
    return _ORDERS.get(int(order_id))


_ORDERS: dict[int, Order] = {}
