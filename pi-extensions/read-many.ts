/**
 * read_many: read several files in one tool call.
 *
 * Each entry is handed unchanged to pi's built-in read tool (ctx.executeTool("read", ...)), so text,
 * images, truncation, offset/limit and errors are exactly those of read. This tool only does two
 * things: it calls read for each file in order, and it puts a path header before each result.
 * The read tool must be active (it is by default).
 *
 * Why: models that pay a full model round trip per tool step (some subscription-backed providers take
 * 8-17 s per step) read a known set of files in one step instead of many.
 *
 * Install: copy or symlink this file into ~/.pi/agent/extensions/. With advisors, add "read_many" to an
 * advisor's read_tools.
 */
import { Type } from "@earendil-works/pi-ai";
import { defineTool, type ExtensionAPI } from "@earendil-works/pi-coding-agent";

const fileEntry = Type.Object({
	path: Type.String({ description: "Path to the file to read (relative or absolute)" }),
	offset: Type.Optional(Type.Number({ description: "Line number to start reading from (1-indexed)" })),
	limit: Type.Optional(Type.Number({ description: "Maximum number of lines to read" })),
});

type Block = { type: string; text?: string; [key: string]: unknown };

const readManyTool = defineTool({
	name: "read_many",
	label: "read_many",
	description:
		"Read the contents of several files in one call. Each entry is passed unchanged to the read tool, so text files, images, truncation and offset/limit behave exactly as in read. Results come back in the given order, each under a line with its path. A file that fails to read reports its error in place; the other files are still returned.",
	promptSnippet: "Read several files in one call",
	promptGuidelines: ["Use read_many instead of consecutive read calls when you already know which files you need."],
	parameters: Type.Object({
		files: Type.Array(fileEntry, { minItems: 1, description: "Files to read, in order" }),
	}),

	async execute(_toolCallId, params, signal, _onUpdate, ctx) {
		const content: Block[] = [];
		const files: { path: string; isError: boolean; details: unknown }[] = [];
		for (const [index, entry] of params.files.entries()) {
			if (signal?.aborted) throw new Error("Operation aborted");
			const outcome = await ctx.executeTool("read", entry, { signal });
			content.push({ type: "text", text: `${index === 0 ? "" : "\n\n"}=== ${entry.path} ===\n` });
			content.push(...((outcome.result?.content ?? []) as Block[]));
			files.push({ path: entry.path, isError: outcome.isError, details: outcome.result?.details });
		}
		const result = { content, details: { files } };
		// As with read, a failed read is a tool error. Here the call as a whole fails only when every file failed.
		return files.every((file) => file.isError) ? { ...result, isError: true } : result;
	},
});

export default function (pi: ExtensionAPI) {
	pi.registerTool(readManyTool);
}
