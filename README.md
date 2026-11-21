# advisors

Let a coding agent consult other models the way it uses its own sub-agents.

`advisors` is one command plus a small local daemon. A working agent (Claude Code, or anything that can run a shell command) asks a configured advisor a question. Advisors run through [pi](https://github.com/earendil-works/pi) (`pi --mode rpc`, which reaches Anthropic, Vertex AI, OpenAI-compatible APIs and anything a pi extension adds) or the [Codex CLI](https://github.com/openai/codex) app-server. The caller decides whether to wait or run in the background. While a turn runs it can read progress, steer it, or stop it, and every turn is written to disk.

```bash
advisors list                                        # who is available, quota, concurrency, capabilities
advisors ask gpt "What is wrong with this function?" --files src/a.py --effort low
advisors ask --session gpt-1121-0712-ab12 "Expand on your second point"
advisors status gpt-1121-0712-ab12:2                 # phase, last backend event, tool trace, answer tail
advisors steer gpt-1121-0712-ab12:2 "Only look at the error handling"
advisors stop gpt-1121-0712-ab12:2
advisors log gpt-1121-0712-ab12
```

## Why

Asking another model for a second opinion from inside an agent usually means pasting a long `pi -p …` or `codex exec …` line, waiting blindly, and losing the answer in a scroll buffer. advisors gives the agent:

- **One interface for every backend.** Session handling, effort levels, attachments and tool restrictions are the same whether the advisor runs on Codex or pi.
- **Foreground or background, chosen per call.** In the background the output file is live progress (phases, tool calls, a heartbeat that says when the backend last did something), and the harness's own notification fires at the end. No MCP server and no fixed timeout decide for the caller.
- **Steering and stopping.** `steer` reaches the running turn (Codex `turn/steer`, pi `steer`) and reports whether it was delivered, queued or too late. `stop` and SIGTERM end a turn cleanly, and the session can continue.
- **Rules the caller cannot bypass.** Per-group concurrency limits with queueing, a circuit breaker for paid advisors (40 tool calls / 30 minutes per turn), budget blocks and exhausted quotas reported with their own exit codes, idempotency keys so a retry is never charged twice.
- **Records.** Prompt, answer and metadata (requested / sent / confirmed effort, time, tokens or cost, remaining quota) for every turn under `~/.advisors/`.
- **A skill** (`skill/SKILL.md`) that teaches the agent when to consult, how to run a blind review, and how to run a multi-advisor discussion.

The design is in [DESIGN.md](DESIGN.md).

## Requirements

- macOS or Linux, Python 3.12 or newer (standard library only; tested on 3.13 and 3.14).
- At least one backend, installed and signed in:
  - [pi](https://github.com/earendil-works/pi) for `pi-rpc` advisors (tested with pi 1.4.0), with the providers you want configured in pi;
  - the [Codex CLI](https://github.com/openai/codex) for `codex-app-server` advisors (tested with 0.171.0).

advisors drives these official CLIs with your own credentials and never handles API keys or tokens itself. Use each provider within its terms.

## Install

```bash
git clone https://github.com/Yuanforz/advisors.git && cd advisors
pipx install .            # or: pip install --user .   (or run bin/advisors straight from the checkout)

mkdir -p ~/.advisors
cp examples/advisors.toml ~/.advisors/advisors.toml   # then edit it: one table per advisor
advisors list
```

For Claude Code, install the skill:

```bash
mkdir -p ~/.claude/skills/advisors
ln -s "$PWD/skill/SKILL.md" ~/.claude/skills/advisors/SKILL.md
```

Optional pi extensions in [`pi-extensions/`](pi-extensions/README.md):

- `read-many.ts` adds a `read_many` tool (several files in one call, each read exactly like `read`). Worth it for slow providers where every tool step costs a full model round trip. Add `"read_many"` to an advisor's `read_tools`.
- `vertex-budget.ts` puts a hard spending limit on pi's google-vertex provider. advisors recognises its blocks (exit code 4) and shows its remaining budget for advisors with `quota = "vertex-budget"`.

## Configuring advisors

`~/.advisors/advisors.toml` (or `$ADVISORS_CONFIG`) holds the advisor table. [`examples/advisors.toml`](examples/advisors.toml) explains every field. In short:

```toml
[groups]
codex = 2                      # at most two Codex turns at once; more are queued

[advisors.gpt]
backend = "codex-app-server"   # or "pi-rpc"
model = "gpt-6.2-astra"        # pi: provider/model
efforts = ["low", "medium", "high", "xhigh", "max"]
default_effort = "high"
group = "codex"
paid = false                   # true adds the circuit breaker
quota = "codex"                # where `list` reads the remaining quota
description = "What this advisor is good for, what it costs, when not to use it."
```

The description matters: `advisors list` shows it to the calling model, which picks advisors from it. The daemon re-reads the file when it changes. A broken edit keeps the last good table, and `advisors list` shows the error.

## Using it from an agent

`advisors ask` waits in the foreground by default: progress on stderr, then the answer on stdout and one metadata line. In Claude Code, run it with Bash `run_in_background` for anything longer than a quick question. The background output file is the progress, and TaskStop stops the turn. `advisors ask --help` has every flag. The most important ones:

- `--session S` continues a session. A session is bound to one advisor and one working directory. If it is busy, `ask` returns exit code 3 with the running turn's id instead of turning into a steer.
- `--tools none` gives the advisor no tools; attach material with `--files`. Use it for blind reviews (see below).
- `--key K` is an idempotency key: the same key returns the original turn instead of running (and paying for) it again.
- `--json` prints one JSON object at the end instead of the answer and metadata line.

### Exit codes

| Status | Code | Meaning |
|---|---|---|
| ok | 0 | finished normally |
| failed | 1 | the backend reported an error |
| — | 2 | usage error |
| busy | 3 | the session is running another turn |
| blocked_budget | 4 | a budget gate blocked the request before it was sent |
| quota_exhausted | 5 | subscription quota used up; the output has the reset time |
| rate_limited | 6 | 429 / RESOURCE_EXHAUSTED while quota remains |
| timeout | 7 | turn time limit (default 30 min), 10 min without backend events, or the paid circuit breaker |
| aborted | 8 | stopped |
| unknown | 9 | outcome unknown after submission; check `advisors status` |
| auth_required | 10 | the advisor needs a new sign-in; the error names the command; the user has to run it |

`steer` exits 0 for delivered and queued, 1 for expired and unsupported.

## What advisors can see

- The prompt, files attached with `--files`, and with `--tools read` (the default) whatever their read-only tools can reach. pi's read tools are **not** confined to the working directory, and Codex's read-only sandbox can read anything your user can. Treat an advisor like a colleague with read access to your account.
- That includes earlier answers from other advisors under `~/.advisors/sessions/`. For a blind review, use `--tools none` and attach the material.
- Nothing is written into the working directory. Run records live under `~/.advisors/` (mode 0700; the socket is 0600).
- Codex advisors run with the `read-only` sandbox and `approvalPolicy: never`, and approval requests are declined. pi advisors get only the tools in `read_tools`, and pi's context files, skills, prompt templates and MCP servers are turned off. pi extensions stay on, so budget gates and custom providers keep working.

## Plugins

A Python file in `~/.advisors/plugins/` can add a quota source, for a provider whose remaining quota advisors cannot read by itself. A plugin defines `QUOTA_SOURCES = {"name": async_function}`, and an advisor opts in with `quota = "name:argument"`. The interface is documented in [`advisors/plugins.py`](advisors/plugins.py). Plugins run inside the daemon with your permissions.

## Data and processes

- Data directory: `~/.advisors/` (`ADVISORS_HOME` moves it). Each turn is `sessions/<session>/turns/<n>.{prompt.md,answer.md,meta.json,events.jsonl}`.
- The first `advisors` command starts `advisord`. A backend process is closed after its session has been idle for 10 minutes and resumed from disk on the next question. The daemon exits after 30 minutes with no active sessions or connections.
- pi keeps its own session files where it always does, so pi's usage accounting still sees advisor turns.

## Tests

```bash
python3 -m unittest discover -s tests -t .
```

The suite uses pi's `fauxProvider` fake models and temporary data directories, so it costs nothing. It needs `pi` on PATH; tests that need it are skipped otherwise. `tests/test_budget.py` has a control group that sends one request to a Vertex project that does not exist (it gets a 403 and costs nothing). It needs network access and Google application-default credentials, and only runs with `ADVISORS_NETWORK_TESTS=1`.

`tests/manual/live_check.py ADVISOR` runs a real advisor through foreground, background, steer, stop and resume. It spends real quota or money.

## Status

Early (0.1). Verified so far: the zero-cost suite; Codex app-server handshakes and `turn/steer` within a turn (one live run); the Codex usage-limit path (`quota_exhausted` with the reset time, from real events); pi `steer` and `abort` on subscription-backed pi providers. Not verified live yet: Codex `turn/interrupt`, and steering paid pi providers (Anthropic, Vertex) mid-turn.

## License

MIT, see [LICENSE](LICENSE).
