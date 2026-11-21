# advisors design

advisors lets a working agent (mainly Claude Code, but anything that can run a shell command) consult other models the way it uses its own sub-agents. One command reaches any configured advisor. The caller decides whether to wait for the answer or run it in the background, can watch progress, steer and stop a running turn, and every turn is written to disk.

Code comments refer to the numbered sections below.

## 1. Goals

1. One command asks any advisor. The caller does not need to know pi or Codex flags.
2. The caller chooses foreground or background (in Claude Code: Bash `run_in_background`). In the background it can read live progress, steer, and stop, and it gets the harness's own completion notification.
3. Follow-up questions continue the same backend session.
4. Budget, quota and concurrency rules are enforced inside the daemon, where the caller cannot bypass them. A blocked request or an exhausted quota is reported as such, never as an empty answer.
5. Every turn is written to disk: prompt, answer, and metadata (model, requested and confirmed effort, time, cost or usage, remaining quota).
6. No cost when idle: without sessions the daemon exits on its own.

## 2. Architecture

- **`advisord`**: a local daemon on a unix socket (`~/.advisors/advisord.sock`, mode 0600). The first `advisors` command starts it; it exits after 30 minutes without active sessions or connections. It owns every backend process and does concurrency, queueing, persistence and the rules.
- **`advisors`**: the command-line client and the only interface. There is no MCP server: parameters are explained by `--help` and the skill, and background runs, progress, stopping and notifications come from the caller's own shell tooling.
- **Backend adapters**, exactly two:
  - `pi-rpc`: one `pi --mode rpc` process per active session. pi reaches many providers (Anthropic, Vertex, OpenAI-compatible APIs, and providers added by pi extensions).
  - `codex-app-server`: one `codex app-server` (stdio JSON-RPC) per active session.
  - A session idle for 10 minutes has its backend process closed. The next question resumes from disk (pi `--session-id`, Codex `thread/resume`).
- **Skill**: `skill/SKILL.md` covers judgment only: when to consult, foreground or background, blind review, multi-advisor discussion, cost. Flags live in `advisors --help`, not in the skill. Which advisor is good for what lives in each advisor's `description` in the config, so the skill stays generic.
- **Plugins**: `~/.advisors/plugins/*.py` can add quota sources (see `advisors/plugins.py`).

## 3. Advisors

Advisors are defined in `~/.advisors/advisors.toml` (or `$ADVISORS_CONFIG`); `examples/advisors.toml` documents every field. Each advisor has a backend, a model, the efforts it allows and a default, a concurrency group, whether it is paid, an optional quota source, and a description that `advisors list` shows to the calling model.

- **Concurrency groups**: each group has a limit (default 2, set in `[groups]`). Requests beyond it queue, and the status shows the queue position.
- **Efforts**: checked per advisor. An unsupported effort is lowered to the nearest supported one (ties go lower), and the metadata says "requested X, used Y". For pi advisors the startup probe intersects the configured efforts with what pi reports for the model.
- **The config is reloaded** when the file changes. A broken edit keeps the last good table and `advisors list` shows the error.

## 4. Commands

```
advisors ask (ADVISOR | --session S) PROMPT
             [--effort E] [--files F ...] [--cwd DIR] [--tools none|read]
             [--key IDEMPOTENCY_KEY] [--label L] [--timeout SEC] [--json]
advisors status [SESSION | RUN] [--tail N] [--since CURSOR] [--json]
advisors steer (SESSION | RUN) TEXT [--mode steer|follow_up] [--json]
advisors stop (SESSION | RUN) [--json]
advisors list [--json]          # advisors, capabilities, availability, quota, concurrency
advisors log SESSION            # a human-readable session record
```

- **`ask`**
  - Waits in the foreground until the turn ends. Meanwhile it prints progress to stderr: phase changes, tool calls, and a heartbeat every 30 seconds that names the last backend event and how long ago it was. At the end it prints the answer to stdout, followed by one metadata line.
  - Run in the background, the output file is the live progress.
  - SIGTERM or SIGINT to the `ask` process (for example Claude Code's TaskStop) stops the turn.
- **Sessions**
  - Without `--session` a new session is created; its name is in the first progress line.
  - With `--session` the session continues; the advisor cannot be changed (a session is bound to one advisor) and the working directory is fixed.
  - While a session is running a turn, `ask` returns busy with the running turn's id. It never silently turns into a steer.
- **`--tools`**
  - `read` (default): pi gets the advisor's `read_tools` (default `read,grep,find,ls`); Codex runs with the `read-only` sandbox and `approvalPolicy: never`.
  - `none`: no tools; material can only come in through `--files`. Use it for blind review: pi's read tools are not limited to the working directory and can read other advisors' answers under `~/.advisors`.
- **`--files`**: text files are inlined into the prompt; images are sent as image inputs.
- **`--key`**
  - Idempotency key. Submitting the same key again returns the original turn (attaches if it is still running, returns the result if it is done), so nothing is charged twice.
  - If the client disconnects after submitting, the turn still runs to the end and is written to disk. If its state really is unknown, it is marked unknown.
- **`steer`**
  - Applies only to the running turn. It reports what happened: `delivered`, `queued` (waits for the next gap), `expired` (the turn has ended) or `unsupported`.
  - A new turn is `ask --session`.
  - `--mode follow_up` delivers after the current answer is written, still within the same turn.
- **`stop`**: idempotent; cancels the backend's turn.
- **Exit codes**: one fixed code per outcome (section 7). `--json` prints an object with the same fields.

## 5. Capabilities (probed at start-up, shown by `list`)

| Capability | codex-app-server | pi-rpc |
|---|---|---|
| Steer | `turn/steer`, applied within the running turn | pi `steer`, delivered between tool calls; with no tool calls it waits until the answer is written. How well a model follows it depends on the provider |
| Interrupt | `turn/interrupt` | pi `abort`; the session can continue afterwards |
| Thinking | event counts only, no raw text | same |
| Usage | tokens; quota from `account/rateLimits` | tokens, plus USD when the provider reports cost |
| Resume | `thread/resume` | `--session-id` |

An advisor's `notes` table can replace any of these lines with what you have verified for that provider.

The daemon records the versions of `pi`, `codex` and any executables plugins name, and probes again when they change (handshakes only, no quota spent). If the Codex handshake fails, Codex advisors fall back to `codex exec --json`, and the capability lines say that steering and interrupting are unavailable.

## 6. Progress and status

`status RUN` returns:

- The phase: queued / connecting / thinking / calling a tool / writing the answer / retrying / done.
- The type of the last backend event and how many seconds ago it was. When the backend is quiet the status says "waiting for the model, progress unknown"; the daemon's own heartbeat never stands in for backend progress.
- The recent tool trace (tool name and an argument summary).
- Bytes of answer text so far and the number of thinking events.
- The last N characters of the answer (default 400) with a cursor; `--since` continues from it.

Raw thinking is never returned: providers differ in what they expose, it costs the caller's tokens, and it would carry draft guesses into the caller's judgment.

## 7. Outcomes

| Status | How it is recognised | Exit code |
|---|---|---|
| ok | the turn ended normally | 0 |
| failed | the backend reported an error | 1 |
| busy | the session is running another turn | 3 |
| blocked_budget | an `entry_appended` event with `entry.customType == "vertex-budget"` in pi's event stream, or the extension's notify text | 4 |
| quota_exhausted | Codex `turn/completed` with status failed and `codexErrorInfo == "usageLimitExceeded"` (the reset time is parsed from the message); or a quota source reporting `exhausted` after a failed or rate-limited turn | 5 |
| rate_limited | 429 / RESOURCE_EXHAUSTED while the quota is not exhausted | 6 |
| timeout | the per-turn hard limit (default 30 minutes), 10 minutes without any backend event, or the paid circuit breaker | 7 |
| aborted | stopped by `stop` or a signal to `ask` | 8 |
| unknown | the state after submission is unknown | 9 |
| auth_required | the advisor needs a new sign-in; the message names the command. Retrying cannot help, so the user has to act | 10 |

Exit code 2 is a usage error. When a budget gate blocks a pi turn, the turn's end looks exactly like a manual abort (`aborted: true` and "This operation was aborted"); only the custom entry tells them apart.

## 8. Files

```
~/.advisors/
  advisors.toml                           # the advisor table (yours)
  plugins/*.py                            # optional local plugins (yours)
  advisord.sock, advisord.pid, advisord.log
  capabilities.json                       # the last probe
  sessions/<session>/meta.json            # advisor, model, backend session id, created, totals
  sessions/<session>/turns/<n>.prompt.md
  sessions/<session>/turns/<n>.answer.md
  sessions/<session>/turns/<n>.meta.json  # requested/confirmed effort, time, usage, cost, status, key
  sessions/<session>/turns/<n>.events.jsonl  # backend events, with streaming deltas removed
  keys/<sha256>.json                      # idempotency key -> run
```

Run records never go into a working directory an advisor might read. `--cwd` defaults to the caller's current directory.

## 9. Hard rules

- pi never gets `--no-extensions`: budget gates and providers are extensions. The config has no way to ask for it; only test advisors with a `faux` model may use it. pi always gets `--no-context-files --no-skills --no-prompt-templates --no-mcp`.
- Codex runs with the `read-only` sandbox and `approvalPolicy: never`. Any approval request from the server is declined and logged.
- Codex threads start with a lean config: the user's MCP servers, plugins and apps are turned off, and so are the multi-agent tools (an advisor should not spawn sub-agents). Without this, every Codex request carried about 20k tokens of tool definitions from the user's Codex setup.
- Circuit breaker: a paid advisor's turn is stopped after 40 tool calls or 30 minutes and reported as timeout. `--timeout` cannot raise it.

## 10. Tests

1. **Zero cost** (`tests/`): pi's `fauxProvider` fake models (`--no-extensions -e tests/faux/faux-advisor.ts`) drive every daemon path: foreground and background, status, steer, stop, busy, idempotency keys, timeouts, budget blocks. The budget test isolates pi with `PI_CODING_AGENT_DIR` and `PI_VERTEX_BUDGET_DIR` and credentials for a project that does not exist.
2. **Live** (`tests/manual/live_check.py`): a real advisor through foreground, background + status, steer, stop and resume. It spends real quota or money.
3. **Codex overhead** (`tests/manual/codex_overhead.py`): the fixed per-request token cost with the lean config.

Protocol references: pi's `docs/rpc.md`, `rpc-commands.md` and `json.md` (under `$(npm root -g)/@earendil-works/pi-coding-agent/`), and `codex app-server generate-json-schema --out DIR` for Codex.
