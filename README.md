# SenRew

A code review agent that **reads the code before it complains**.

Most AI reviewers get handed a diff and nothing else. That is why they flag
things like this:

```python
query = f'SELECT * FROM {name} WHERE created_at > %s'
```

"SQL injection!" — except six lines above, outside the diff, is this:

```python
ALLOWED_TABLES = {'orders', 'refunds', 'users'}
...
if name not in ALLOWED_TABLES:
    raise ValueError(f'unknown table: {name}')
```

A diff-only reviewer cannot see that, so it either cries wolf or stays quiet
about everything. SenRew has tools. It opens the file, finds the guard, and
throws the finding out.

Everything runs **on your machine**, with **your** GitHub token and **your**
Gemini key. Nothing is sent to anyone else's server.

---

## Try it in one command

```bash
pip install -r requirements.txt
python senrew.py demo
```

No API key, no GitHub account, no network. It runs the real agent loop against
a sample pull request built around the trap above, using canned model replies,
and prints every tool call as it happens:

```
    list_changed_files()
    read_diff(src/payments/refund.py)
    read_diff(src/reports/exporter.py)
    read_file(src/reports/exporter.py)
    record_finding: Endpoint does not check the record belongs to the caller
    record_finding: Possible SQL injection from an interpolated table name
    finish()
    read_file(src/reports/exporter.py)
    confirm_finding: Read the surrounding lines and the claim holds.
    reject_finding: Read the file: the value is checked against a hardcoded whitelist
    finish()

  2 candidate(s), 1 rejected, 1 published
  2/3 files opened, 9 steps
```

Two findings in, one out.

Add a `GEMINI_API_KEY` to `.env` and run the same command again to watch a real
model do it. Here is an actual run:

```
    list_changed_files()
    read_diff(src/payments/refund.py)
    read_diff(src/reports/exporter.py)
    read_file(src/reports/exporter.py)          <- checks the "SQL injection"
    search_repo('get_order')
    read_file(src/orders.py)                    <- does get_order filter by owner?
    read_file(src/payments.py)
    read_file(src/auth.py)                      <- what does require_login prove?
    record_finding: Missing ownership check allows authenticated users to refund any order
    finish()
```

It never raised the SQL injection at all. It opened `exporter.py`, saw the
whitelist, and said nothing — the false positive was avoided rather than
retracted. Then it chased the real bug through four files before reporting it.

The verifier re-read the code independently and confirmed with line numbers:

> Verified in `src/payments/refund.py` lines 15-21 and `src/orders.py` lines
> 11-17. `refund_order` fetches the order using `get_order(order_id)` and
> immediately passes it to `issue_refund(order)` without checking if
> `order.user_id == current_user().id`. `get_order` does not perform any
> authorization check.

One finding, `CRITICAL`, score 100.0, 12 steps, about six cents.

---

## Use it for real

```bash
cp .env.example .env        # add GEMINI_API_KEY and GITHUB_TOKEN
```

Get a Gemini key at <https://aistudio.google.com/apikey>. Get a GitHub token
with `repo` scope from Settings → Developer settings → Personal access tokens.
Both stay in `.env`, which is gitignored.

**Review one pull request** (prints the review, posts nothing):

```bash
python senrew.py review owner/repo 42
python senrew.py review owner/repo 42 --repo-path .    # faster: reads your clone
python senrew.py review owner/repo 42 --post           # actually publish
```

**Review automatically, every time you push:**

```bash
python senrew.py watch .              # or: watch ~/projects
python senrew.py watch . --post
```

Leave it running. When you push a branch that has an open pull request, SenRew
reviews it. Push again and it reviews the new commits. Nothing to configure per
repository, no webhook, no server.

> **It watches git, not you.** A push updates the remote-tracking refs inside
> `.git`, and that is the only thing being polled. SenRew does not read your
> shell history and does not see what you type. That also means it catches
> pushes from VS Code and GitHub Desktop, which watching a terminal would miss.

---

## How the agent works

Two agents share one loop. The loop is about forty lines in
[`senrew/agent.py`](senrew/agent.py):

```
ask the model -> it calls tools -> run them -> hand back the results -> repeat
```

**The reviewer** gets six tools:

| Tool | What it is for |
|---|---|
| `list_changed_files` | What this pull request touches |
| `read_diff` | The diff of one file |
| `read_file` | **Any** file, including code the diff does not show |
| `search_repo` | Find where a function is defined, or who calls it |
| `record_finding` | Report a problem |
| `finish` | Stop |

**The verifier** then gets the findings and the same reading tools, and is told
to disprove them. It can open the file and check. A verdict reached without
reading the code is a guess, and the prompt says so.

**Severity is arithmetic, not opinion.** The model reports impact, likelihood
and blast radius as labels; [`senrew/severity.py`](senrew/severity.py) computes
the number:

```
impact = major (70) x likelihood = possible (0.6)   = 42.0
blast_radius = module                      x 1.15   = 48.3
category = security                        x 1.25   = 60.4
path matches "payment"                     x 1.20   = 72.5
                                                      -> HIGH
```

The same bug always scores the same, tuning is one number rather than a prompt
rewrite, and anyone who disagrees can be shown the sum.

---

## Coverage is reported, not assumed

A reviewer that quietly skips a file reads as a reviewer that found less. So
every review states what it opened:

```
**Coverage:** opened 2 of 3 changed file(s).

Not reviewable: `assets/logo.png` - binary or empty - GitHub sent no diff text
```

Coverage is measured from the `read_diff` calls the agent actually made, not
from what it claims. If it skips a file, it gets asked once more, and anything
still unopened is named in the review.

---

## Guardrails

An agent loop with tools has two failure modes worth taking seriously, and both
are handled in code rather than in a prompt.

**It can spend your money.** Every step is an API call. `MAX_STEPS` (default 12)
is a hard ceiling; hitting it keeps the findings so far and says so rather than
throwing the work away. Tool output is capped too — an uncapped file read sits
in the context of every later step.

**It can read the wrong things.** The agent picks paths out of a diff someone
else wrote, so `read_file` is untrusted input. It is confined to the repository:
`../`, absolute paths and symlinks pointing outside are refused, and there are
tests for it.

Repository content is treated as data, never as instructions. A file telling
the agent to approve the change is reported as a security finding, not obeyed.

---

## Configuration

Everything is an environment variable — see [`.env.example`](.env.example).
The ones that matter:

| Variable | Default | |
|---|---|---|
| `GEMINI_API_KEY` | — | Yours. Never leaves your machine. |
| `GITHUB_TOKEN` | — | Yours, `repo` scope. |
| `GEMINI_MODEL` | `gemini-3.5-flash` | Verified against the live API |
| `MAX_STEPS` | `12` | Hard ceiling on tool-calling steps |
| `VERIFY` | `true` | The second agent. Turn it off to see the difference. |
| `USE_FAKE_MODEL` | `false` | Canned replies, no network |
| `MIN_SEVERITY_TO_POST` | `low` | Noise floor |

### A note on the free tier

A free Gemini key allows about **5 requests per minute**, and an agent spends
one per step. A real review will therefore pause partway through:

```
  rate limited (5/min free-tier limit), waiting 56s
```

That is expected, not a failure. SenRew reads the delay Google sends back and
waits exactly that long, then carries on. If you run out for the *day* it says
so plainly and stops, because waiting another minute would not help.

Two things make it cheaper: the agent is told to batch independent tool calls
into one turn, and `MAX_STEPS` bounds the worst case. A paid key removes the
pauses entirely.

---

## Tests

```bash
pip install -r requirements-dev.txt
pytest -q          # 77 tests, no network
```

They cover the loop (termination, the step ceiling, that the model's reply goes
back verbatim so Gemini's `thoughtSignature` survives, that parallel call ids
are echoed), the guardrails (path traversal, size caps), coverage reporting,
the verifier actually dropping a finding, and the severity formula.

---

## Layout

```
senrew.py           CLI: demo | review | watch
watcher.py          git ref polling
senrew/
  agent.py          the loop, the reviewer, the verifier
  tools.py          the six tools, their schemas, dispatch
  llm.py            Gemini with function calling
  codebase.py       reading code: local clone or GitHub API
  github.py         pull requests, diffs, posting the review
  severity.py       the scoring formula
  store.py          what has already been reviewed
  prompts/          reviewer.md, verifier.md
  demo_repo/        the sample repository used by `senrew.py demo`
```

## Licence

MIT.
