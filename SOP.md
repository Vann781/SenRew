# SenRew — Standard Operating Procedure

**Document:** SOP-SENREW-01
**Applies to:** SenRew v0.1.0
**Audience:** anyone installing, running or troubleshooting SenRew

This is the operating manual. It covers installation, configuration, day-to-day
use, what the output means, and what to do when something goes wrong. For *why*
the tool is built the way it is, see [README.md](README.md).

---

## 1. Purpose and scope

SenRew reviews GitHub pull requests using a Google Gemini agent that can read
the repository — not just the diff. It runs entirely on the operator's own
machine, using the operator's own credentials.

**In scope:** reviewing open pull requests on GitHub repositories the operator
can already access, either one at a time or automatically on every push.

**Out of scope:** SenRew does not merge, approve, block or close anything. It
posts review comments only, and only when explicitly told to.

---

## 2. Prerequisites

| Requirement | Notes |
|---|---|
| Python 3.10 or newer | Developed and tested on 3.14 |
| `git` on the PATH | Required for `watch`; not needed for `review` |
| A Google Gemini API key | Free tier is sufficient — see §9.1 for its limits |
| A GitHub personal access token | Needs `repo` scope to read PRs and post reviews |
| Network access | To `generativelanguage.googleapis.com` and `api.github.com` |

Both credentials stay on the operator's machine. Nothing is transmitted to the
author of this tool or to any third party.

---

## 3. Procedure — Installation

1. Obtain the source:

   ```bash
   git clone https://github.com/Vann781/SenRew.git
   cd SenRew
   ```

2. Install dependencies:

   ```bash
   pip install -r requirements.txt
   ```

   For running the test suite, use `requirements-dev.txt` instead.

3. Confirm the installation without any credentials (see §5).

---

## 4. Procedure — Configuration

1. Create the configuration file from the template:

   ```bash
   cp .env.example .env
   ```

2. Open `.env` and set the two credentials:

   ```
   GEMINI_API_KEY=<your key from https://aistudio.google.com/apikey>
   GITHUB_TOKEN=<your token with repo scope>
   ```

3. Leave every other value at its default unless §8 says otherwise.

**Rules:**

- `.env` is listed in `.gitignore`. **Never commit it, and never paste either
  credential into an issue, a screenshot or a chat.**
- Shell environment variables override `.env`. To try a setting once without
  editing the file, export it for that command only.
- If a credential is ever exposed, revoke it at its source immediately —
  rotating is cheap, a leaked `repo`-scope token is not.

---

## 5. Procedure — Verify the installation

Run the built-in demo. It requires **no credentials and no network**:

```bash
python senrew.py demo
```

**Expected result** — a sequence of tool calls, then a tally:

```
  2 candidate(s), 1 rejected, 1 published
  2/3 files opened, 9 steps, $0.0000
```

If you see this, the agent loop, the tools, the verifier, the severity scoring
and the report generator are all working. Exit code is `0`.

If `GEMINI_API_KEY` is set, the same command runs against the real model
instead and the numbers will differ — that is expected and is a stronger check.

---

## 6. Procedure — Review one pull request

### 6.1 Preview a review (default, publishes nothing)

```bash
python senrew.py review <owner>/<repo> <pr_number>
```

Example:

```bash
python senrew.py review octocat/hello-world 42
```

The review is printed under a `DRY RUN - not posted` banner. Nothing reaches
GitHub.

### 6.2 Review faster using a local clone

If you already have the repository checked out, point SenRew at it. File reads
then come off the disk instead of the GitHub API — faster, free, and not
subject to API rate limits.

```bash
python senrew.py review <owner>/<repo> <pr_number> --repo-path /path/to/clone
```

> The clone should be on or near the pull request's branch. SenRew reads
> whatever is currently in the working tree, which may differ from the commit
> being reviewed.

### 6.3 Publish the review to GitHub

**This is visible to everyone with access to the repository. Preview first.**

```bash
python senrew.py review <owner>/<repo> <pr_number> --post
```

One review is posted, containing all inline comments. The comments appear under
the account that owns `GITHUB_TOKEN`.

---

## 7. Procedure — Continuous review on every push

### 7.1 Start the watcher

```bash
python senrew.py watch .                     # the current repository
python senrew.py watch ~/projects            # every repository one level down
python senrew.py watch repo-a repo-b         # specific repositories
```

On start it lists what it is watching and resolves each GitHub remote:

```
SenRew watching 1 repo(s), every 15s
  This watches git refs. It does not read your terminal.

  my-project                   owner/my-project
```

Leave it running. When you push a branch, SenRew checks whether that branch has
an **open pull request**, and if so reviews it.

By default nothing is posted. Add `--post` to publish.

### 7.2 What triggers a review

A review runs only when **all** of these hold:

1. A branch's remote-tracking ref changed (i.e. you pushed, or fetched someone
   else's push).
2. That branch has an **open** pull request.
3. The pull request is **not a draft**.
4. That exact head commit has **not already been reviewed** (state is kept in
   `~/.senrew/reviews.json`).

Anything else is reported and skipped, e.g. `no open pull request for that
branch, nothing to do`.

### 7.3 Options

| Option | Effect |
|---|---|
| `--once` | Do a single sweep and exit. Useful for testing. |
| `--interval N` | Seconds between checks. Default 15. |
| `--post` | Publish reviews instead of printing them. |

Stop the watcher with `Ctrl+C`. It exits cleanly.

> **What the watcher reads.** It polls the remote-tracking refs inside `.git`
> via `git for-each-ref`. It does **not** read shell history, keystrokes or
> terminal activity. This also means it catches pushes made from VS Code or
> GitHub Desktop.

---

## 8. Reference

### 8.1 Commands

| Command | Purpose |
|---|---|
| `python senrew.py demo` | Offline self-test against a built-in sample PR |
| `python senrew.py review <owner>/<repo> <pr>` | Review one pull request |
| `python senrew.py watch [paths...]` | Review automatically on every push |

Dry run is the default for `review` and `watch`. Publishing always requires
`--post`.

### 8.2 Settings

All are environment variables, set in `.env`.

| Variable | Default | Purpose |
|---|---|---|
| `GEMINI_API_KEY` | — | Required, except for `demo` |
| `GITHUB_TOKEN` | — | Required for `review` and `watch` |
| `GEMINI_MODEL` | `gemini-3.5-flash` | Model name |
| `MAX_STEPS` | `12` | Hard ceiling on tool-calling steps per agent |
| `VERIFY` | `true` | Run the second agent that checks the findings |
| `USE_FAKE_MODEL` | `false` | Canned replies, no network |
| `MIN_SEVERITY_TO_POST` | `low` | Findings below this band are not posted |
| `MAX_COMMENTS_PER_REVIEW` | `15` | Cap on inline comments |
| `MAX_FILE_LINES` | `400` | Cap on one `read_file` result |
| `MAX_SEARCH_RESULTS` | `20` | Cap on one `search_repo` result |
| `RATE_LIMIT_RETRIES` | `6` | How many times to wait out a per-minute limit |
| `RATE_LIMIT_MAX_WAIT` | `75` | Longest single wait, seconds |
| `WATCH_INTERVAL_SECONDS` | `15` | Watcher poll interval |

### 8.3 Exit codes

| Code | Meaning |
|---|---|
| `0` | Completed successfully |
| `1` | Configuration problem, no repositories found, or the review failed |
| `2` | Gemini quota exhausted |

---

## 9. Interpreting the output

### 9.1 Tool calls

Each line is one action the agent took:

```
    read_diff(src/payments/refund.py)     the diff of one changed file
    read_file(src/orders.py)              a file, including code not in the diff
    search_repo('get_order')              looking for a definition or caller
    record_finding: <title>               a problem it decided to report
    confirm_finding / reject_finding      the verifier's verdict
```

### 9.2 The tally

```
  2 candidate(s), 1 rejected, 1 published
  2/3 files opened, 9 steps, $0.0000
```

- **candidates** — what the reviewer found.
- **rejected** — what the verifier threw out after reading the code. A non-zero
  number here is the tool working as designed.
- **published** — what survived, after the severity floor and comment cap.
- **files opened** — how many changed files the agent actually read. If this is
  lower than the total, the review names the ones it skipped and why.
- **steps / cost** — API calls made, and the approximate spend.

### 9.3 Severity

Bands are computed in code from the model's labels, never invented by the
model: `critical` ≥ 80, `high` ≥ 60, `medium` ≥ 35, `low` below that.

---

## 10. Troubleshooting

| Symptom | Cause and action |
|---|---|
| `GITHUB_TOKEN is not set` | No `.env`, or the value is blank. See §4. |
| `GEMINI_API_KEY is not set` | Same. Or run `python senrew.py demo` instead. |
| `rate limited (5/min free-tier limit), waiting 56s` | **Normal on the free tier.** An agent spends one request per step. It will resume by itself. Do not interrupt it. |
| `Gemini daily quota is gone` | No more requests today on that model. Quota is **per model**, so try `GEMINI_MODEL=gemini-3.1-flash-lite`, or wait for the reset, or use a paid key. |
| `Model '<name>' not found. It may have been retired` | Model names change. Pick another; verified working: `gemini-3.5-flash`, `gemini-3.6-flash`, `gemini-3.1-flash-lite`, `gemini-flash-latest`. |
| `No git repositories found in: ...` | The path has no `.git`, and neither do its immediate children. |
| `(no GitHub origin, skipped)` | That repository's `origin` remote is not GitHub. |
| `no open pull request for that branch` | Expected. Open a PR first; SenRew reviews pull requests, not branches. |
| `already reviewed at <sha>` | That exact commit was already done. Push a new commit to trigger another review. |
| `Refused: '<path>' is outside the repository` | Working as designed — the agent may only read inside the repository. No action needed. |
| `Could not fetch <repo>#<n>: 404` | Wrong `owner/repo`, wrong PR number, or the token cannot see that repository. |
| Review is slower than expected | Each step is one API call, and free-tier keys force a wait between them. Lower `MAX_STEPS`, or use a paid key. |

---

## 11. Security and data handling

- **Credentials** live only in `.env`, which is gitignored. They are sent only
  to Google and GitHub, never to the author of this tool.
- **API keys are stripped from error messages** before display, because the HTTP
  library otherwise includes the full request URL in exceptions.
- **File access is confined to the repository.** The agent chooses which files
  to read based on diff content written by whoever opened the pull request, so
  those paths are treated as untrusted: `..`, absolute paths and symlinks
  pointing outside the repository are refused.
- **Repository content is data, never instruction.** A file attempting to tell
  the agent to approve a change is reported as a security finding rather than
  obeyed.
- **Your code is sent to Google Gemini** for analysis — diffs, and the files the
  agent chooses to open. Do not run SenRew against a repository whose contents
  you are not permitted to send to a third-party API.

---

## 12. Known limitations

Stated plainly so no one is surprised in front of an audience:

1. **The `watch` loop has not been verified end to end.** Startup, repository
   discovery, remote resolution and ref seeding are tested. The full
   *push → detect → review* sequence has not been run against a live push.
2. **`--post` has never been executed.** Every review produced so far has been a
   dry-run preview. Posting is implemented and unit-tested but unproven live.
3. **Free-tier quota is the practical bottleneck**, not the tool. Roughly five
   requests per minute, one per agent step.
4. **Coverage is best-effort.** If the agent skips a changed file, it is asked
   once more; anything still unopened is named in the review rather than
   silently dropped — but it is still unreviewed.
5. **`--repo-path` reads the working tree as it currently is**, which may not
   match the commit under review.

---

## 13. Maintenance

| Task | When | How |
|---|---|---|
| Check the model still exists | On a `not found` error | Try another name from §10 |
| Rotate credentials | If exposed, or periodically | Revoke at source, update `.env` |
| Run the test suite | After any code change | `pytest -q` — expect 93 passing |
| Clear review history | To force a re-review | Delete `~/.senrew/reviews.json` |

---

## 14. Revision history

| Version | Change |
|---|---|
| 1.0 | First issue, for SenRew v0.1.0 |
