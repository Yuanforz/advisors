# pi extensions

Two optional [pi](https://github.com/earendil-works/pi) extensions that work well with advisors. Each is a single TypeScript file: copy or symlink it into `~/.pi/agent/extensions/` and pi loads it on start.

```bash
ln -s "$PWD/read-many.ts" ~/.pi/agent/extensions/read-many.ts
ln -s "$PWD/vertex-budget.ts" ~/.pi/agent/extensions/vertex-budget.ts
```

## read-many.ts

Adds a `read_many` tool that reads several files in one call. Each entry goes unchanged to pi's own `read` tool, so text, images, truncation, `offset` / `limit` and errors behave exactly as with `read`; results come back in order, each under a `=== path ===` header. The call fails only if every file fails.

It helps most with providers where every tool step costs a full model round trip: a model that already knows which five files it needs reads them in one step instead of five. To let an advisor use it, add `"read_many"` to its `read_tools` in `advisors.toml`.

## vertex-budget.ts

A hard spending limit for pi's `google-vertex` provider. Before every model request it checks "spent so far + the worst case for this request" against the budget and aborts the turn if it would exceed it, so the request is never sent. Spending is recorded at list prices in a ledger shared by every pi process.

Configure it with `~/.pi/agent/vertex-budget.json`:

```json
{"since": "2026-11-01T00:00:00Z", "credit": 50, "grantDay": 15, "grantUsd": 0, "reserve": 2,
 "timeZone": "America/Los_Angeles"}
```

- `credit`: your balance at `since`.
- `grantDay` / `grantUsd`: a recurring monthly grant (day of month and amount); use `"grantUsd": 0` if there is none.
- `reserve`: kept back as a margin.
- `timeZone`: the zone grant days are counted in (default `America/Los_Angeles`).
- Budget = credit + grantUsd × grants since `since` − reserve.

If the file cannot be read, the budget is 0 and every Vertex request is blocked. Update the `LIST` price table at the top of the file for the models you use. Models missing from it are charged at twice pi's own cost figure, to stay on the safe side.

When it blocks a request it prints (or, with a UI, notifies) a message containing "blocked; the request was not sent", and appends a custom session entry `vertex-budget` with `{blocked: true, ...}`. advisors reads that entry to report exit code 4 instead of a generic abort. A plain `pi -p` exits with code 1 when blocked; with `--mode json` the exit code stays 0, so look for the entry.
