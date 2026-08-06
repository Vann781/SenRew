"""SenRew - a code review agent that reads the code before it complains.

    python senrew.py demo                          offline, no keys needed
    python senrew.py review owner/repo 42          review one pull request
    python senrew.py watch .                       review on every push

Nothing is posted unless you pass --post.
"""

import argparse
import sys
import time
from pathlib import Path

import watcher
from senrew import agent, config, github, llm, store
from senrew.codebase import GitHubCodebase, LocalCodebase
from senrew.models import Review
from senrew.output import note, show_tool, summarise


def report(review: Review, posted: bool) -> None:
    print()
    print(review.summary if posted else github.preview(review))
    print()
    print(summarise(review))
    if not posted:
        print("  Nothing was posted. Add --post to publish.")


# --- commands --------------------------------------------------------------


def do_review(repository: str, number: int, repo_path: str | None, post: bool) -> int:
    """Review one pull request."""
    print(f"\nSenRew - {repository}#{number}")
    print(f"  model {'CANNED (offline)' if config.USE_FAKE_MODEL else config.GEMINI_MODEL}")

    pr = github.get_pull_request(repository, number)
    files = github.get_changed_files(repository, number)
    print(f"  {pr.get('title')!r} - {len(files)} changed file(s)\n")

    # A local clone is faster and free; fall back to the API without one.
    if repo_path and Path(repo_path).is_dir():
        code = LocalCodebase(repo_path)
        print(f"  reading code from {Path(repo_path).resolve()}\n")
    else:
        code = GitHubCodebase(repository, pr["head"]["sha"])
        print("  reading code over the GitHub API\n")

    review = agent.review_pull_request(
        repository, pr, code, files, on_tool=show_tool, on_note=note
    )

    if review.status == "failed":
        print(f"\n  Review failed: {review.error}")
        return 1

    if post:
        github.post_review(repository, number, review, review.head_sha)
        print("\n  Posted to GitHub.")
    store.save(review)
    report(review, post)
    return 0


def do_watch(paths: list[str], interval: int, once: bool, post: bool) -> int:
    """Watch repositories and review each push that has an open pull request."""
    repos = watcher.find_repos(paths or ["."])
    if not repos:
        print("No git repositories found in: " + ", ".join(paths or ["."]))
        return 1

    print(f"\nSenRew watching {len(repos)} repo(s), every {interval}s")
    print("  This watches git refs. It does not read your terminal.\n")

    tracked = {}
    for repo in repos:
        slug = watcher.remote_slug(repo)
        print(f"  {repo.name:<28} {slug or '(no GitHub origin, skipped)'}")
        if slug:
            # Seed with the current state so we react to the NEXT push, not to
            # everything that ever happened.
            tracked[repo] = (slug, watcher.remote_refs(repo))
    print()

    if not tracked:
        print("None of those have a GitHub origin remote.")
        return 1

    try:
        while True:
            for repo, (slug, previous) in list(tracked.items()):
                current = watcher.remote_refs(repo)
                pushed = watcher.detect_pushes(previous, current)
                tracked[repo] = (slug, current)

                for branch in pushed:
                    _handle_push(slug, repo, branch, post)

            if once:
                return 0
            time.sleep(interval)
    except KeyboardInterrupt:
        print("\nStopped.")
        return 0


def _handle_push(slug: str, repo_dir: Path, branch: str, post: bool) -> None:
    """One pushed branch: find its pull request, review it if it is new."""
    print(f"[{time.strftime('%H:%M:%S')}] {slug} {branch} moved")

    try:
        open_prs = github.list_open_pull_requests(slug)
    except (RuntimeError, ValueError) as exc:
        print(f"  could not list pull requests: {exc}")
        return

    pr = next((p for p in open_prs if p.get("head", {}).get("ref") == branch), None)
    if pr is None:
        print("  no open pull request for that branch, nothing to do")
        return
    if pr.get("draft"):
        print(f"  #{pr['number']} is a draft, skipping")
        return

    head = pr["head"]["sha"]
    if store.already_reviewed(slug, pr["number"], head):
        print(f"  #{pr['number']} already reviewed at {head[:8]}")
        return

    print(f"  reviewing #{pr['number']} {pr.get('title')!r}")
    try:
        files = github.get_changed_files(slug, pr["number"])
        review = agent.review_pull_request(
            slug, pr, LocalCodebase(repo_dir), files, on_tool=show_tool, on_note=note
        )
        if post and review.status != "failed":
            github.post_review(slug, pr["number"], review, head)
            print("  posted.")
        store.save(review)
        report(review, post)
    except Exception as exc:  # noqa: BLE001 - one bad PR must not kill the watcher
        print(f"  review failed: {type(exc).__name__}: {exc}")


def do_demo() -> int:
    """Run the agent offline against a built-in sample pull request."""
    from senrew import demo

    return demo.run()


# --- entry point -----------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="senrew", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("demo", help="offline run, no API key needed")

    p_review = sub.add_parser("review", help="review one pull request")
    p_review.add_argument("repository", help="owner/repo")
    p_review.add_argument("pr_number", type=int)
    p_review.add_argument("--repo-path", help="local clone, for faster file reads")
    p_review.add_argument("--post", action="store_true", help="publish the review")

    p_watch = sub.add_parser("watch", help="review on every push")
    p_watch.add_argument("paths", nargs="*", default=["."])
    p_watch.add_argument("--interval", type=int, default=config.WATCH_INTERVAL_SECONDS)
    p_watch.add_argument("--once", action="store_true", help="one sweep, then exit")
    p_watch.add_argument("--post", action="store_true", help="publish reviews")

    args = parser.parse_args(argv)

    # So a 60-second free-tier wait explains itself instead of looking hung.
    llm.on_wait = note

    try:
        if args.command == "demo":
            return do_demo()

        if not config.GITHUB_TOKEN:
            print("GITHUB_TOKEN is not set. Copy .env.example to .env and add yours.")
            return 1
        if not config.GEMINI_API_KEY and not config.USE_FAKE_MODEL:
            print("GEMINI_API_KEY is not set. Add it to .env, or run: python senrew.py demo")
            return 1

        if args.command == "review":
            return do_review(args.repository, args.pr_number, args.repo_path, args.post)
        return do_watch(args.paths, args.interval, args.once, args.post)

    except llm.OutOfQuota as exc:
        # Common enough with an agent loop to deserve a plain message rather
        # than a stack trace.
        print(f"\n{exc}")
        return 2
    except KeyboardInterrupt:
        print("\nStopped.")
        return 0


if __name__ == "__main__":
    sys.exit(main())
