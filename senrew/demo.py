"""A self-contained demo, so you can see the agent work before setting anything up.

The sample pull request in `demo_repo/` is built around one trap.

`src/reports/exporter.py` adds this line:

    query = f'SELECT * FROM {name} WHERE created_at > %s'

which looks exactly like SQL injection. It is not: `name` is checked against a
hardcoded ALLOWED_TABLES set a few lines above - and those lines are NOT in the
diff. A reviewer that only reads diffs cannot tell. An agent that calls
read_file can.

The other file has a real authorisation bug, so a good run finds one problem
and rejects one false alarm.

Runs with a real model if GEMINI_API_KEY is set, otherwise with canned output.
"""

from pathlib import Path

from senrew import agent, config, github, llm
from senrew.codebase import LocalCodebase
from senrew.output import note, show_tool, summarise

REPO_DIR = Path(__file__).parent / "demo_repo"

PR = {
    "number": 1,
    "title": "Add refund endpoint and nightly export",
    "body": "Implements the refund flow and the nightly table export.",
    "head": {"sha": "demo000000000000000000000000000000000000", "ref": "feature/refunds"},
    "draft": False,
}

# Note the exporter hunk: it starts BELOW the ALLOWED_TABLES guard, so the
# whitelist is invisible unless the agent goes and reads the file.
FILES = [
    {
        "filename": "src/payments/refund.py",
        "status": "added",
        "changes": 14,
        "patch": (
            "@@ -0,0 +1,14 @@\n"
            "+from flask import request, jsonify\n"
            "+\n"
            "+from src.app import app\n"
            "+from src.auth import current_user, require_login\n"
            "+from src.orders import get_order\n"
            "+from src.payments import issue_refund\n"
            "+\n"
            "+\n"
            "+@app.post('/refund')\n"
            "+@require_login\n"
            "+def refund_order():\n"
            "+    order_id = request.json['order_id']\n"
            "+    order = get_order(order_id)\n"
            "+    issue_refund(order)\n"
            "+    return jsonify({'status': 'refunded'})\n"
        ),
    },
    {
        "filename": "src/reports/exporter.py",
        "status": "modified",
        "changes": 14,
        "patch": (
            "@@ -27,2 +27,15 @@ def export_table(name, since, conn):\n"
            "-    raise NotImplementedError\n"
            "+    query = f'SELECT * FROM {name} WHERE created_at > %s'\n"
            "+    rows = conn.execute(query, (since,)).fetchall()\n"
            "+\n"
            "+    for attempt in range(3):\n"
            "+        try:\n"
            "+            upload_export(rows)\n"
            "+            break\n"
            "+        except Exception as exc:\n"
            "+            log.error('upload failed: %s', exc)\n"
            "+            if attempt == 2:\n"
            "+                raise\n"
            "+            time.sleep(2 ** attempt)\n"
            "+\n"
            "+    return len(rows)\n"
        ),
    },
    {
        # A file GitHub sends with no diff text. It must show up in the
        # coverage report rather than vanishing.
        "filename": "assets/logo.png",
        "status": "added",
        "changes": 0,
    },
]


def run() -> int:
    llm.on_wait = note
    offline = config.USE_FAKE_MODEL or not config.GEMINI_API_KEY
    if offline:
        config.USE_FAKE_MODEL = True

    print("\nSenRew demo")
    print("=" * 70)
    print(f"  sample PR   {len(FILES)} changed file(s) in {REPO_DIR.name}/")
    print(f"  model       {'CANNED (no API key, no network)' if offline else config.GEMINI_MODEL}")
    print(f"  verifier    {'on' if config.VERIFY else 'off'}")
    print("=" * 70)
    print("\nWatch the tool calls: the agent has to open exporter.py to find")
    print("the ALLOWED_TABLES guard, because it is not in the diff.\n")

    review = agent.review_pull_request(
        "senrew/demo", PR, LocalCodebase(REPO_DIR), FILES,
        on_tool=show_tool, on_note=note,
    )

    print()
    print(github.preview(review))
    print()
    print(summarise(review))

    if offline:
        print("\n  That was canned output. Add GEMINI_API_KEY to .env and run")
        print("  it again to watch a real model reason about the trap.")
    return 0
