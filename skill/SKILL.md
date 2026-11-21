---
name: advisors
description: Consult other models for a second opinion, a blind review, or a multi-advisor discussion. Use the advisors command; flags are documented in advisors --help.
---

# Consulting advisors

The `advisors` command handles the backends, sessions, budgets, quotas and concurrency for you. This skill covers judgment only: who to ask, foreground or background, how to run a blind review or a discussion, and what it costs. For flags and exit codes, read `advisors --help` and `advisors ask --help`. Never assemble `pi` or `codex` commands yourself.

## Before you start

Run `advisors list`. It shows each advisor's description, whether it is available now, its remaining quota, how full its concurrency group is, and whether it can be steered and interrupted. If an advisor's quota is exhausted or a budget blocks it, pick another one or tell the user.

## Who to ask

- Read the descriptions in `advisors list`. They say what each advisor is good for, what it costs, and when not to use it.
- If the user or the project's instructions name an advisor or an effort, use that. The descriptions only guide you when nothing was specified.
- For an independent view, prefer advisors from a different vendor than you.
- Prefer subscription advisors over paid ones unless the task needs a specific paid model or the user asked for it.
- Use `--effort low` for simple questions; high efforts are slower and spend more quota.

## Foreground or background

- Short questions (no file reading, low effort, an answer within a minute or two): run in the foreground and wait.
- Everything else: run `advisors ask …` in the background (in Claude Code, Bash `run_in_background`). The background output file is the live progress: phases, tool calls, and a heartbeat every 30 seconds. You are notified when it ends. To read the answer text written so far, use `advisors status RUN --tail N`, and continue later with `--since` and the cursor from the last output.
- Reading progress costs your context. Don't poll the output file often. A heartbeat that says "waiting for the model, progress unknown" means the backend has been quiet for a while, not that something failed; after 10 minutes without backend events the daemon stops the turn as a timeout by itself.
- To stop: TaskStop the background task (`ask` stops the turn on SIGTERM), or `advisors stop RUN`.

## Steering

- If an advisor misread the question or is heading the wrong way, `advisors steer RUN "…"` first. It is cheaper than stopping and asking again, and keeps the context.
- Check the result: `delivered` arrived; `queued` waits for the next gap (pi advisors: between tool calls, or after the current answer when there are no tool calls); `expired` means the turn already ended, so use `ask --session`; `unsupported` means this backend cannot be steered right now.
- Steering only affects the running turn. A new turn is `ask --session`.

## Blind review, first round

The goal is independent opinions that cannot influence each other.

1. Open a new session per advisor with the same question.
2. Use `--tools none` and attach all material with `--files`. pi's read tools are not limited to the working directory and could read other advisors' answers under `~/.advisors`; with read tools it is not a blind review.
3. Don't put your own conclusion or leaning in the question. Give the problem, the material, and the aspects to cover.
4. Run them in parallel in the background. Concurrency groups queue anything beyond their limits, so you don't have to count.
5. Label the round with `--label` (for example `blind-1`) so `advisors log` is easy to read later.

## Multi-advisor discussion

1. Run the blind first round above.
2. Second round: send each advisor the other advisors' main points, without saying who made them, with `ask --session` ("Another reviewer argues … Which points do you agree with, which not, and why?"). Send the points, not whole answers.
3. Synthesize yourself: where they agree, where they disagree and on what grounds, and which claims you verified. Agreement is not proof; verify what advisors claim before relying on it.

## Cost and quota

- The metadata line at the end of every answer reports usage or cost and the remaining quota. Use those numbers when you report cost to the user; don't estimate.
- Paid advisors have a circuit breaker: a turn is stopped after 40 tool calls or 30 minutes (exit code 7, timeout). Give them specific questions and name the files to read instead of letting them explore a whole repository.
- Exit code 4 (blocked_budget): a budget gate blocked the request before it was sent. Stop and tell the user. Don't work around it by calling pi directly or editing budget settings.
- Exit code 5 (quota_exhausted): the output has the reset time. Tell the user; don't retry in a loop.
- For requests that may need a retry (an unstable network or process), add `--key`: resubmitting the same key returns the original turn and is never charged twice.

## Reading results

- A non-zero exit code is not an empty answer. Read the reason on stderr (the exit code table is in `advisors --help`). Exit code 9 means the outcome is unknown; check `advisors status RUN`.
- Exit code 10 (auth_required): the advisor needs a new sign-in. Give the user the command from the error message as it is and ask them to run it. Don't retry, and don't try to fix the sign-in yourself.
- The last stdout line is metadata: advisor, requested / sent / confirmed effort, session, turn, time, usage or cost, remaining quota, and the path of the full answer.
