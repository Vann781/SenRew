"""Refund endpoint. Part of the SenRew demo repository.

There is a real authorisation bug in here: refund_order checks that the caller
is logged in, but never checks the order belongs to them.
"""

from flask import request, jsonify

from src.app import app
from src.auth import current_user, require_login
from src.orders import get_order
from src.payments import issue_refund


@app.post('/refund')
@require_login
def refund_order():
    order_id = request.json['order_id']
    order = get_order(order_id)
    issue_refund(order)
    return jsonify({'status': 'refunded'})
