// A fake model for zero-cost tests (pi-ai's fauxProvider). Only the tests load it, with --no-extensions -e.
// Its behaviour is set by the latest user message that contains "FAUX:". Format: FAUX:<mode> k=v ...
// (msg= takes the rest of the line)
//   echo  [delay=SEC]          answer ECHO with the number of user messages, attachments and images, and
//                              the user messages received after it (steers)
//   tools n=3 [delay=SEC]      call the ls tool n times in a row, then answer DONE with the user messages
//                              received after it
//   slow  words=2000           stream a long text through the slow provider (faux-slow, about 30 tokens/s)
//   sleep s=30                 send no events for s seconds (can be aborted), then answer WOKE
//   error msg=...              end at once with stopReason=error and errorMessage set to msg
import { fauxAssistantMessage, fauxProvider, fauxText, fauxThinking, fauxToolCall } from "@earendil-works/pi-ai";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

function textOf(m: any): string {
	if (typeof m.content === "string") return m.content;
	return (m.content ?? []).filter((b: any) => b.type === "text").map((b: any) => b.text).join("");
}

function imagesOf(m: any): number {
	if (typeof m.content === "string") return 0;
	return (m.content ?? []).filter((b: any) => b.type === "image").length;
}

function sleep(ms: number, signal?: AbortSignal): Promise<void> {
	return new Promise((resolve) => {
		if (signal?.aborted) return resolve();
		const t = setTimeout(resolve, ms);
		signal?.addEventListener("abort", () => { clearTimeout(t); resolve(); }, { once: true });
	});
}

function parse(s: string): Record<string, string> {
	const line = s.split("\n")[0];
	const out: Record<string, string> = {};
	const msgAt = line.indexOf("msg=");
	const head = msgAt >= 0 ? line.slice(0, msgAt) : line;
	if (msgAt >= 0) out.msg = line.slice(msgAt + 4).trim();
	const toks = head.trim().split(/\s+/).filter(Boolean);
	out.mode = toks.shift() ?? "echo";
	for (const t of toks) {
		const i = t.indexOf("=");
		if (i > 0) out[t.slice(0, i)] = t.slice(i + 1);
	}
	return out;
}

function directive(messages: any[]) {
	for (let i = messages.length - 1; i >= 0; i--) {
		const m = messages[i];
		if (m.role !== "user") continue;
		const t = textOf(m);
		const k = t.indexOf("FAUX:");
		if (k >= 0) return { index: i, msg: m, text: t, args: parse(t.slice(k + 5)) };
	}
	return { index: -1, msg: undefined, text: "", args: { mode: "echo" } as Record<string, string> };
}

const step = async (context: any, options: any) => {
	const msgs: any[] = context.messages ?? [];
	const d = directive(msgs);
	const after = msgs.slice(d.index + 1);
	const toolResults = after.filter((m) => m.role === "toolResult").length;
	const laterUsers = after.filter((m) => m.role === "user").map(textOf);
	const userTurns = msgs.filter((m) => m.role === "user").length;
	const a = d.args;
	const delay = Number(a.delay ?? 0) * 1000;
	if (delay) await sleep(delay, options?.signal);
	switch (a.mode) {
		case "tools": {
			const n = Number(a.n ?? 3);
			if (toolResults < n) return fauxAssistantMessage([fauxToolCall("ls", { path: "." })], { stopReason: "toolUse" });
			return fauxAssistantMessage(fauxText(`DONE tools=${toolResults} seen=${JSON.stringify(laterUsers)}`));
		}
		case "slow":
			return fauxAssistantMessage(fauxText("word ".repeat(Number(a.words ?? 2000))));
		case "sleep":
			await sleep(Number(a.s ?? 30) * 1000, options?.signal);
			return fauxAssistantMessage(fauxText("WOKE"));
		case "error":
			return fauxAssistantMessage([], { stopReason: "error", errorMessage: a.msg ?? "faux boom" });
		default: {
			const files = (d.text.match(/<file name=/g) ?? []).length;
			const imgs = d.msg ? imagesOf(d.msg) : 0;
			return fauxAssistantMessage([
				fauxThinking("faux thinking"),
				fauxText(`ECHO turns=${userTurns} files=${files} images=${imgs} seen=${JSON.stringify(laterUsers)}`),
			]);
		}
	}
};

export default function (pi: ExtensionAPI) {
	const fast = fauxProvider({ provider: "faux", models: [{ id: "fast", reasoning: true }] });
	fast.setResponses(Array.from({ length: 2000 }, () => step));
	pi.registerProvider(fast.provider);
	const slow = fauxProvider({ provider: "faux-slow", models: [{ id: "slow", reasoning: true }], tokensPerSecond: 30 });
	slow.setResponses(Array.from({ length: 2000 }, () => step));
	pi.registerProvider(slow.provider);
}
