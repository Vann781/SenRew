"""Nightly export worker. Part of the SenRew demo repository.

This file is the trap. The query below looks like SQL injection, and a
reviewer who only sees the diff will say so. It is not: `name` is checked
against ALLOWED_TABLES a few lines up, and that check is NOT part of the diff.

Only an agent that opens this file can tell the difference.
"""

import logging
import time

log = logging.getLogger(__name__)

ALLOWED_TABLES = {'orders', 'refunds', 'users'}


def upload_export(rows):
    """Hand the rows to the storage layer."""
    raise NotImplementedError


def export_table(name, since, conn):
    # Called only from the nightly worker, never from a request.
    if name not in ALLOWED_TABLES:
        raise ValueError(f'unknown table: {name}')

    query = f'SELECT * FROM {name} WHERE created_at > %s'
    rows = conn.execute(query, (since,)).fetchall()

    for attempt in range(3):
        try:
            upload_export(rows)
            break
        except Exception as exc:
            log.error('upload failed: %s', exc)
            if attempt == 2:
                raise
            time.sleep(2 ** attempt)

    return len(rows)
