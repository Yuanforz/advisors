"""Render cast.json (recorded by capture.py from real advisors sessions) into terminal-style GIFs and a PNG.

Waiting time is compressed in the GIFs; the elapsed seconds that advisors printed stay as recorded. The temporary
project and data directories are shown as ~/demo and ~/.advisors. Needs Pillow and the Menlo font (macOS).
"""

import json
import sys
import textwrap
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

D = Path(__file__).resolve().parent
OUT = Path(sys.argv[1]) if len(sys.argv) > 1 else D.parent / "media"
OUT.mkdir(parents=True, exist_ok=True)

FONT = ImageFont.truetype("/System/Library/Fonts/Menlo.ttc", 14, index=0)
BOLD = ImageFont.truetype("/System/Library/Fonts/Menlo.ttc", 14, index=1)
CW = FONT.getlength("M")
LH = 19
PAD = 18
BAR = 34
COLS = 100

BG = (24, 24, 32)
WIN = (30, 31, 41)
FG = (220, 223, 228)
DIM = (128, 134, 150)
PROMPT = (126, 211, 135)
META = (122, 178, 214)
ADD = (126, 211, 135)
DEL = (240, 120, 120)
WARN = (232, 185, 95)
SEP = (60, 62, 78)

CAST = json.loads((D / "cast.json").read_text()) if (D / "cast.json").exists() else {"paths": {}, "scenes": []}
SUBS = [(CAST["paths"].get("project", "/nonexistent"), "~/demo"), (CAST["paths"].get("home", "/nonexistent"), "~/.advisors")]


def clean(text):
    for a, b in SUBS:
        text = text.replace(a, b)
    return text


def color_of(kind, text):
    if kind == "cmd":
        return FG, True
    if text.startswith("[advisors] gpt ·") or text.startswith("[advisors] claude ·"):
        return META, False
    if "aborted" in text and text.startswith("[advisors]"):
        return WARN, False
    if text.startswith("[advisors]"):
        return DIM, False
    if text.startswith("delivered:") or "stopped:" in text:
        return PROMPT, False
    if text.startswith("+") and not text.startswith("+++"):
        return ADD, False
    if text.startswith("-") and not text.startswith("---") and not text.startswith("- "):
        return DEL, False
    if text.startswith("```"):
        return DIM, False
    return FG, False


META_ROWS = None  # set per scene: truncate the metadata line to this many rows


def rows_for(kind, text, cols):
    """Split one logical line into screen rows: (text, color, bold, is_prompt)."""
    text = clean(text)
    col, bold = color_of(kind, text)
    if kind == "cmd":
        body = "$ " + text
        rows = textwrap.wrap(body, cols, subsequent_indent="  ", break_on_hyphens=False) or [body]
        return [(r, col, bold, i == 0) for i, r in enumerate(rows)]
    if " tool: " in text:  # one row, truncated
        return [(text if len(text) <= cols else text[: cols - 1] + "…", col, bold, False)]
    if not text:
        return [("", col, bold, False)]
    if len(text) <= cols:
        return [(text, col, bold, False)]
    lead = text[: len(text) - len(text.lstrip(" "))]
    indent = lead + ("  " if text.lstrip(" ").startswith("- ") else "")
    rows = textwrap.wrap(text, cols, initial_indent="", subsequent_indent=indent, break_on_hyphens=False)
    if lead and rows and not rows[0].startswith(lead):
        rows[0] = lead + rows[0]
    if META_ROWS and col == META and len(rows) > META_ROWS:
        rows = rows[:META_ROWS]
        rows[-1] = rows[-1][: cols - 2].rstrip() + " …"
    return [(r.rstrip(), col, bold, False) for r in rows]


class Pane:
    def __init__(self, rows, cols=COLS):
        self.rows, self.cols = rows, cols
        self.lines = []      # finished screen rows
        self.typing = None   # (full text, chars shown)

    def add(self, kind, text):
        self.lines.extend(rows_for(kind, text, self.cols))

    def visible(self):
        lines = list(self.lines)
        if self.typing is not None:
            full, n = self.typing
            lines.extend(rows_for("cmd", full[:n], self.cols))
            lines[-1] = (lines[-1][0] + "█", *lines[-1][1:])
        elif not lines or lines[-1][0] != "$ ":
            pass
        return lines[-self.rows:]


def draw_window(panes, title):
    width = int(PAD * 2 + CW * COLS)
    total_rows = sum(p.rows for p in panes)
    height = BAR + PAD * 2 + LH * total_rows + (len(panes) - 1) * (PAD + 1)
    img = Image.new("RGB", (width + 40, height + 40), BG)
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([20, 20, 20 + width, 20 + height], radius=12, fill=WIN)
    for i, c in enumerate([(255, 95, 86), (255, 189, 46), (39, 201, 63)]):
        d.ellipse([20 + 16 + i * 22, 20 + 11, 20 + 28 + i * 22, 20 + 23], fill=c)
    tw = BOLD.getlength(title)
    d.text((20 + (width - tw) / 2, 20 + 8), title, font=FONT, fill=DIM)
    y = 20 + BAR + PAD - 6
    for pi, pane in enumerate(panes):
        if pi:
            d.line([20 + PAD, y + PAD / 2 - 2, 20 + width - PAD, y + PAD / 2 - 2], fill=SEP, width=1)
            y += PAD + 1
        for r, (text, col, bold, is_prompt) in enumerate(pane.visible()):
            x = 20 + PAD
            yy = y + r * LH
            if is_prompt and text.startswith("$ "):
                d.text((x, yy), "$", font=BOLD, fill=PROMPT)
                d.text((x + 2 * CW, yy), text[2:], font=BOLD if bold else FONT, fill=col)
            else:
                d.text((x, yy), text, font=BOLD if bold else FONT, fill=col)
        y += pane.rows * LH
    return img


def compress(gap):
    if gap <= 0.4:
        return gap
    return min(0.4 + 0.3 * (gap - 0.4), 1.6)


def needed_rows(scene, pane_index):
    return sum(len(rows_for(e["kind"], e["text"], COLS)) for e in scene["events"] if e["pane"] == pane_index)


def render_gif(scene, name, title, pane_caps, hold=3.5, first_pause=0.8, meta_rows=None):
    global META_ROWS
    META_ROWS = meta_rows
    panes = [Pane(min(cap, needed_rows(scene, i))) for i, cap in enumerate(pane_caps)]
    frames, durs = [], []

    def snap(sec):
        frames.append(draw_window(panes, title))
        durs.append(max(20, int(sec * 1000)))

    snap(first_pause)
    last_t = 0.0
    events = scene["events"]
    i = 0
    while i < len(events):
        e = events[i]
        gap = compress(e["t"] - last_t)
        if gap > 0.02 and frames:
            durs[-1] += int(gap * 1000)
        last_t = e["t"]
        pane = panes[e["pane"]]
        if e["kind"] == "cmd":
            full = clean(e["text"])
            for n in range(0, len(full) + 1, 3):
                pane.typing = (full, n)
                snap(0.035)
            pane.typing = (full, len(full))
            snap(0.45)
            pane.typing = None
            pane.add("cmd", e["text"])
            snap(0.1)
            i += 1
            continue
        # group lines that arrived together
        batch = [e]
        while i + 1 < len(events) and events[i + 1]["kind"] == "out" and events[i + 1]["pane"] == e["pane"] \
                and events[i + 1]["t"] - e["t"] < 0.05:
            i += 1
            batch.append(events[i])
        for b in batch:
            pane.add("out", b["text"])
        snap(0.08)
        i += 1
    durs[-1] += int(hold * 1000)
    pal = frames[-1].quantize(colors=256, method=Image.Quantize.MAXCOVERAGE)
    pframes = [f.quantize(palette=pal, dither=Image.Dither.NONE) for f in frames]
    path = OUT / f"{name}.gif"
    pframes[0].save(path, save_all=True, append_images=pframes[1:], duration=durs, loop=0, optimize=True,
                    disposal=1)
    print(path, f"{path.stat().st_size / 1024:.0f} KB", len(frames), "frames", f"{sum(durs) / 1000:.1f} s")


def render_png(scene, name, title, rows):
    pane = Pane(rows)
    for e in scene["events"]:
        pane.add(e["kind"], e["text"])
    img = draw_window([pane], title)
    path = OUT / f"{name}.png"
    img.save(path, optimize=True)
    print(path, f"{path.stat().st_size / 1024:.0f} KB")


def main():
    scenes = {s["name"]: s for s in CAST["scenes"]}
    render_gif(scenes["ask"], "ask", "advisors", [40])
    render_gif(scenes["steer"], "steer", "advisors · steer a running turn", [28, 6], meta_rows=2)
    render_gif(scenes["stop"], "stop", "advisors · stop, then continue the session", [40, 40], meta_rows=2)
    n = sum(len(rows_for(e["kind"], e["text"], COLS)) for e in scenes["list"]["events"])
    render_png(scenes["list"], "list", "advisors list", n)


if __name__ == "__main__":
    main()
