/**
 * vertex-budget: a hard spending limit for pi's google-vertex provider.
 *
 * Before every model request (turn_start) it adds "spent so far + the worst case for this request". If that
 * exceeds the budget, it aborts the turn and the request is never sent.
 *
 * Ledger: vertex-ledger.jsonl, one line per Vertex reply and per compaction. Every pi process shares
 * one ledger, including runs with --no-session. Costs are computed at list prices (the LIST table below),
 * which is also how Google Cloud promotional credits and GCP spend caps count.
 *
 * Config: vertex-budget.json in the same directory.
 *
 *   {"since": "2026-11-01T00:00:00Z", "credit": 50, "grantDay": 15, "grantUsd": 0, "reserve": 2,
 *    "timeZone": "America/Los_Angeles"}
 *
 *   budget = credit + grantUsd x (number of grantDay days after `since`, counted in timeZone) - reserve
 *   spent  = the ledger's spending after `since`
 *
 *   credit    your balance at `since` (for example the credit balance shown in the Cloud console)
 *   grantDay  day of the month a recurring grant arrives; grantUsd its amount (0 if there is none)
 *   reserve   money kept back as a safety margin
 *   timeZone  IANA name used to count grant days and months (default America/Los_Angeles)
 *
 * To reconcile with the console, set credit to the balance shown there and since to the current time.
 * If the config cannot be read, the budget is 0 and every request is blocked.
 *
 * Signals for callers:
 *   - stderr (no UI) or a UI notification: "[vertex-budget] ... blocked; the request was not sent ..."
 *     Plain `pi -p` then exits with code 1; with `--mode json` the exit code stays 0, so read the events.
 *   - a custom entry (json / rpc event stream: entry_appended with customType "vertex-budget") with
 *     {blocked: true, model, limit, spent, worst, config}. advisors uses it to tell a budget block from
 *     a manual abort. It does not enter the model context.
 *
 * The gate sits in turn_start + ctx.abort(). Do not move it to before_provider_request: pi lets the request
 * through when that hook throws, so it cannot act as a gate.
 *
 * Env: PI_VERTEX_BUDGET_DIR overrides the directory (default ~/.pi/agent).
 */
import type { ExtensionAPI, ExtensionContext } from "@earendil-works/pi-coding-agent";
import { appendFileSync, readFileSync } from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";

const DIR = process.env.PI_VERTEX_BUDGET_DIR ?? join(homedir(), ".pi", "agent");
const LEDGER = join(DIR, "vertex-ledger.jsonl");
const CONF = join(DIR, "vertex-budget.json");
const PROVIDER = "google-vertex";
const DEFAULT_TZ = "America/Los_Angeles";

// Vertex list prices, USD per million tokens: [input, output (including thinking), cache read].
// Check the current Vertex AI pricing page and add the models you use.
const LIST: Record<string, [number, number, number]> = {
	"gemini-4-carbon": [4, 20, 0.2],
	"gemini-4-argon": [4, 20, 0.2],
};
const OUTPUT_RESERVE = 16_000; // assumed output + thinking tokens when estimating the next request
const FALLBACK_CONTEXT = 100_000; // assumed context right after a compaction, before the size is known

const readConf = (): any => JSON.parse(readFileSync(CONF, "utf8"));

const zoneOf = (c: any): string => {
	const tz = typeof c?.timeZone === "string" && c.timeZone ? c.timeZone : DEFAULT_TZ;
	new Intl.DateTimeFormat("en-CA", { timeZone: tz }); // throws RangeError for an unknown zone
	return tz;
};

const confZone = (): string => {
	try {
		return zoneOf(readConf());
	} catch {
		return DEFAULT_TZ;
	}
};

export const monthOf = (d: Date = new Date(), tz: string = DEFAULT_TZ): string => {
	const parts = new Intl.DateTimeFormat("en-CA", { timeZone: tz, year: "numeric", month: "2-digit" }).formatToParts(d);
	const y = parts.find((p) => p.type === "year")?.value;
	const m = parts.find((p) => p.type === "month")?.value;
	return `${y}-${m}`;
};

const priceOf = (model: string | undefined): [number, number, number] | undefined =>
	Object.entries(LIST).find(([id]) => typeof model === "string" && model.startsWith(id))?.[1];

/**
 * Cost of one call at list prices. A model missing from LIST is charged twice pi's own cost figure:
 * pi's built-in prices can be discounted ones, and over-counting is the safe side.
 */
export const listCost = (model: string | undefined, u: any): number => {
	if (!u) return 0;
	const p = priceOf(model);
	if (!p) return 2 * (u.cost?.total ?? 0);
	return (((u.input ?? 0) + (u.cacheWrite ?? 0)) * p[0] + (u.output ?? 0) * p[1] + (u.cacheRead ?? 0) * p[2]) / 1e6;
};

const localDate = (d: Date, tz: string): string =>
	new Intl.DateTimeFormat("en-CA", { timeZone: tz, year: "numeric", month: "2-digit", day: "2-digit" }).format(d);

/** Reads the config and returns the budget and its start. Anything unreadable gives a budget of 0 (block). */
export const budgetAt = (now: Date = new Date()): { limit: number; since: number } => {
	try {
		const c = readConf();
		const tz = zoneOf(c);
		const since = Date.parse(c.since);
		const nums = [c.credit, c.grantDay, c.grantUsd, c.reserve];
		if (!Number.isFinite(since) || nums.some((x) => typeof x !== "number" || !(x >= 0))) return { limit: 0, since: 0 };
		const from = localDate(new Date(since), tz);
		const to = localDate(now, tz);
		let grants = 0;
		let [y, m] = from.slice(0, 7).split("-").map(Number);
		for (let i = 0; i < 1200; i++) {
			const g = `${y}-${String(m).padStart(2, "0")}-${String(c.grantDay).padStart(2, "0")}`;
			if (g > to) break;
			if (g > from) grants++;
			if (++m > 12) {
				m = 1;
				y++;
			}
		}
		return { limit: Math.max(0, c.credit + c.grantUsd * grants - c.reserve), since };
	} catch {
		return { limit: 0, since: 0 };
	}
};

/** Spending recorded after `since`. No ledger counts as 0; a ledger that exists but cannot be read is Infinity (block). */
const spentSince = (since: number): number => {
	let text: string;
	try {
		text = readFileSync(LEDGER, "utf8");
	} catch (e: any) {
		return e?.code === "ENOENT" ? 0 : Infinity;
	}
	let sum = 0;
	for (const line of text.split("\n")) {
		if (!line) continue;
		try {
			const r = JSON.parse(line);
			if (typeof r.usd === "number" && Date.parse(r.ts) >= since) sum += r.usd;
		} catch {
			/* a half-written line from a concurrent append: skip it */
		}
	}
	return sum;
};

const record = (kind: string, model: string | undefined, u: any) => {
	const usd = listCost(model, u);
	if (!(usd > 0)) return;
	const now = new Date();
	const row = {
		ts: now.toISOString(),
		month: monthOf(now, confZone()),
		kind,
		model,
		input: u.input ?? 0,
		cacheRead: u.cacheRead ?? 0,
		output: u.output ?? 0,
		usd: Math.round(usd * 1e6) / 1e6,
		pid: process.pid,
	};
	appendFileSync(LEDGER, JSON.stringify(row) + "\n"); // O_APPEND, one line per write: safe across processes
};

const fmt = (x: number) => (Number.isFinite(x) ? `$${x.toFixed(2)}` : "(ledger unreadable)");

export default function (pi: ExtensionAPI) {
	const say = (ctx: ExtensionContext, text: string, level: "info" | "warning" = "warning") => {
		if (ctx.hasUI) ctx.ui.notify(text, level);
		else process.stderr.write(`[vertex-budget] ${text}\n`);
	};

	pi.on("turn_start", (_event, ctx) => {
		const model = ctx.model;
		if (model?.provider !== PROVIDER) return;
		const { limit, since } = budgetAt();
		const spent = spentSince(since);
		const tokens = ctx.getContextUsage()?.tokens ?? FALLBACK_CONTEXT;
		const p = priceOf(model.id);
		const worst = p
			? (tokens * p[0] + OUTPUT_RESERVE * p[1]) / 1e6
			: (2 * (tokens * (model.cost?.input ?? 0) + OUTPUT_RESERVE * (model.cost?.output ?? 0))) / 1e6;
		if (spent + worst <= limit) return;
		say(ctx, `Vertex budget ${fmt(limit)}, spent ${fmt(spent)}, this request could cost up to ${fmt(worst)}: blocked; the request was not sent. Budget config: ${CONF}`);
		ctx.abort();
		// Machine-readable record of the block, after the abort and inside try so that nothing here can undo it.
		try {
			const r2 = (x: number) => (Number.isFinite(x) ? Math.round(x * 100) / 100 : null);
			pi.appendEntry("vertex-budget", { blocked: true, model: model.id, limit: r2(limit), spent: r2(spent), worst: r2(worst), config: CONF });
		} catch {}
	});

	pi.on("message_end", (event) => {
		const msg: any = event.message;
		if (msg?.role !== "assistant" || msg.provider !== PROVIDER) return;
		record("turn", msg.model, msg.usage);
	});

	pi.on("session_compact", (event: any, ctx) => {
		const u = event.compactionEntry?.usage;
		if (!u || ctx.model?.provider !== PROVIDER) return;
		record("compact", ctx.model.id, u);
	});

	// Without a UI (-p / json), report spending after every run so callers can pace themselves.
	pi.on("agent_end", (_event, ctx) => {
		if (ctx.hasUI || ctx.model?.provider !== PROVIDER) return;
		const { limit, since } = budgetAt();
		say(ctx, `spent ${fmt(spentSince(since))} of ${fmt(limit)} (list prices)`, "info");
	});
}
