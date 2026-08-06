"""Printing the agent's work as it happens.

Kept separate from the CLI so the demo can use it too, and so nothing needs
to import the top-level senrew.py script.
"""

from senrew.models import Review


def show_tool(name: str, args: dict) -> None:
    """Print one tool call.

    This is what makes the agent legible: you watch it decide to go and read
    a file, instead of seeing only the verdict at the end.
    """
    if name in ("read_file", "read_diff"):
        span = ""
        if args.get("start_line") or args.get("end_line"):
            span = f":{args.get('start_line', 1)}-{args.get('end_line') or 'end'}"
        print(f"    {name}({args.get('path', '')}{span})")
    elif name == "search_repo":
        print(f"    search_repo({args.get('query', '')!r})")
    elif name in ("record_finding", "confirm_finding", "reject_finding"):
        print(f"    {name}: {str(args.get('title') or args.get('reason', ''))[:70]}")
    elif name == "finish":
        print("    finish()")
    else:
        print(f"    {name}()")


def note(message: str) -> None:
    print(f"  {message}")


def summarise(review: Review) -> str:
    """The two-line tally printed after a review."""
    return (
        f"  {review.candidates} candidate(s), {review.rejected} rejected, "
        f"{len(review.findings)} published\n"
        f"  {review.files_reviewed}/{review.files_changed} files opened, "
        f"{review.steps} steps, ${review.cost_usd:.4f}"
    )
