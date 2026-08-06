"""Authentication helpers. Part of the SenRew demo repository.

Deliberately minimal, but real enough that an agent searching for
`require_login` or `current_user` finds a definitive answer: require_login
proves you are SOMEBODY, it does not prove you own anything.
"""

from functools import wraps

from flask import g, jsonify


class User:
    def __init__(self, user_id: int, is_admin: bool = False):
        self.id = user_id
        self.is_admin = is_admin


def current_user() -> User | None:
    """The signed-in user for this request, or None."""
    return getattr(g, "user", None)


def require_login(view):
    """Reject anyone who is not signed in.

    Note what this does NOT do: it says nothing about which records the user
    may touch. Ownership is the caller's job.
    """

    @wraps(view)
    def wrapper(*args, **kwargs):
        if current_user() is None:
            return jsonify({"error": "login required"}), 401
        return view(*args, **kwargs)

    return wrapper
