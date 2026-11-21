# Using advisors

The full reference behind the README. Flags are always up to date in `advisors --help` and `advisors ask --help`.

## Commands

```
advisors ask (ADVISOR | --session S) PROMPT
             [--effort E] [--files F ...] [--cwd DIR] [--tools none|read]
             [--key KEY] [--label L] [--timeout SEC] [--json]
advisors status [SESSION | RUN] [--tail N] [--since CURSOR] [--json]
advisors steer (SESSION | RUN) TEXT [--mode steer|follow_up] [--json]
advisors stop (SESSION | RUN) [--json]
advisors list [--json]
advisors log SESSION
```

- **`ask`** waits in the foreground: progress on stderr, then the answer on stdout and one metadata line (advisor, requested / sent / confirmed effort, session, turn, time, tokens or cost, remaining quota, path of the full answer). SIGTERM or SIGINT stops the turn.
- **Background** is the caller's choice. In Claude Code, run `advisors ask …` with Bash `run_in_background`: the output file is the live progress, the harness notifies you at the end, and TaskStop stops the turn.
- **`--session S`** continues a session. A session is bound to one advisor and one working directory. If it is running a turn, `ask` exits with code 3 and names that turn instead of turning into a steer.
- **`--tools read`** (default) lets the advisor check things itself: pi advisors read files; Codex advisors also run commands in a read-only sandbox and search the web. **`--tools none`** gives no tools; attach material with `--files` (text is inlined, images are sent as images).
- **`--key K`** is an idempotency key: the same key returns the original turn instead of running and paying for it again.
- **`status`** shows the phase, the last backend event and how long ago it was, recent tool calls, and the tail of the answer with a cursor (`--since` continues from it). Raw thinking is never shown.
- **`steer`** reports `delivered`, `queued` (waits for the next gap), `expired` (the turn already ended) or `unsupported`. `--mode follow_up` delivers after the current answer, still within the turn.
- A run id is `SESSION:TURN`, e.g. `gpt-1121-0712-ab12:2`. `status`, `steer` and `stop` also accept a session name, meaning its current turn.

## Exit codes

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

## Rules the daemon enforces

- **Concurrency groups** (default 2 per group, set in `[groups]`): extra requests queue and show their position.
- **Paid circuit breaker**: a turn of an advisor with `paid = true` stops after 40 tool calls or 30 minutes. `--timeout` cannot raise it.
- **Efforts**: an effort the advisor does not allow is lowered to the nearest allowed one, and the metadata says so.
- **pi** always runs with `--no-context-files --no-skills --no-prompt-templates --no-mcp` and never with `--no-extensions`, so budget gates and custom providers keep working.
- **Codex** runs with the read-only sandbox and `approvalPolicy: never`, approval requests are declined, and the user's MCP servers, plugins, apps and sub-agents are turned off for advisor threads. With `--tools none` the shell, `view_image` and web search are off too; `tests/manual/codex_tools.py` prints the tools a thread really gets.

## What advisors can see

- The prompt, files attached with `--files`, and with `--tools read` (the default) whatever their read-only tools reach. pi's read tools are **not** confined to the working directory, and Codex's read-only sandbox can read anything your user can. Treat an advisor like a colleague with read access to your account.
- That includes other advisors' earlier answers (under `~/.advisors/sessions/`, and in Codex's and pi's own session logs). Every advisor is instructed not to open them; this is a soft rule, not a sandbox. When an answer must rest on the attached material alone, use `--tools none`.
- Nothing is written into the working directory. Records live under `~/.advisors/` (mode 0700; the socket is 0600).

## Plugins

A Python file in `~/.advisors/plugins/` can add a quota source for a provider advisors cannot read by itself: define `QUOTA_SOURCES = {"name": async_function}` and set `quota = "name:argument"` on the advisor. The interface is documented in [`advisors/plugins.py`](../advisors/plugins.py). Plugins run inside the daemon with your permissions.

## Data and processes

- Data directory: `~/.advisors/` (`ADVISORS_HOME` moves it; `ADVISORS_CONFIG` points at another config file). Each turn is `sessions/<session>/turns/<n>.{prompt.md,answer.md,meta.json,events.jsonl}`.
- The first `advisors` command starts `advisord`. A backend process closes after its session is idle for 10 minutes and resumes from disk on the next question. The daemon exits after 30 minutes without active sessions or connections. After upgrading advisors, stop the running daemon so the new code is used.

## Tests

```bash
python3 -m unittest discover -s tests -t .
```

The suite drives the daemon with pi's `fauxProvider` fake models in temporary directories, so it costs nothing; it needs `pi` on PATH. `tests/test_budget.py` has a control group that sends one request to a Vertex project that does not exist (403, no cost); it runs only with `ADVISORS_NETWORK_TESTS=1`. `tests/manual/live_check.py ADVISOR` exercises a real advisor and spends real quota or money.

The README demos are real sessions recorded with [`docs/demo/capture.py`](demo/capture.py) and drawn with [`docs/demo/render.py`](demo/render.py).

## Status

Early (0.1), tested on macOS with Python 3.13 / 3.14, pi 1.4.0 and Codex CLI 0.171.0. Verified: the zero-cost suite; with Codex, handshakes, `turn/steer` and `turn/interrupt` mid-turn, resuming a stopped session, and the usage-limit path; pi `steer` and `abort` on subscription-backed providers. Not yet verified live: steering paid pi providers (Anthropic, Vertex) mid-turn.
