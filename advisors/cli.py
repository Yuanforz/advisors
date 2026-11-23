"""The advisors command line (DESIGN.md section 4). It is the only public interface; it starts the daemon if needed."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

from . import __version__, config, fmt, rules, store

PKG_PARENT = Path(__file__).resolve().parent.parent

EPILOG = """\
exit codes (ask; with --json the same fields are in the output object):
  0 ok              finished normally
  1 failed          backend error
  2                 command-line usage error
  3 busy            the session is busy with another turn (the output has that turn's run id)
  4 blocked_budget  a budget gate blocked the request before it was sent (e.g. the vertex-budget pi extension)
  5 quota_exhausted subscription quota exhausted (the output has the reset time)
  6 rate_limited    rate limited (429); the quota is not exhausted
  7 timeout         the turn hit its time limit, the backend sent no events for 10 minutes,
                    or a paid advisor's circuit breaker tripped
  8 aborted         stopped (including when the ask process gets SIGTERM / SIGINT)
  9 unknown         the status is unclear after submission; check with advisors status RUN
 10 auth_required   the advisor needs a new sign-in (the error message has the command to run);
                    the user must do it; do not retry

A run id is SESSION:TURN, for example gpt-1121-0712-ab12:2.
The data directory is ~/.advisors; set ADVISORS_HOME to use another one.
The advisor table is read from $ADVISORS_CONFIG, or <data dir>/advisors.toml.
"""

ASK_HELP = """\
Ask an advisor. ask stays in the foreground until the turn ends. While it waits, it prints progress
to stderr (phase changes, tool calls, and a heartbeat every 30 seconds). At the end it prints the answer
to stdout, followed by one metadata line.
To run it in the background, use the caller's own background mechanism (in Claude Code, Bash with
run_in_background); the background output file then shows live progress. If the ask process gets
SIGTERM / SIGINT, it stops the turn.

Without --session, ask opens a new session; the session name is in the first progress line. With
--session, ask continues that session, and you cannot name an advisor. If the session is already running
a turn, ask returns busy (exit code 3); it never silently turns into a steer.
"""


# ---- talking to the daemon ----

def _connect_once() -> socket.socket:
    addr, chdir = config.socket_address()
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    here = os.getcwd() if chdir else None
    try:
        if chdir:
            os.chdir(chdir)
        s.connect(addr)
    except OSError:
        s.close()
        raise
    finally:
        if here:
            os.chdir(here)
    return s


def _start_daemon() -> None:
    home = config.home()
    home.mkdir(parents=True, exist_ok=True, mode=0o700)
    with open(home / "advisord.start.lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            _connect_once().close()
            return
        except OSError:
            pass
        err = open(home / "advisord.stderr.log", "ab")
        # Run the daemon from the same package as this CLI, whether installed or run from a checkout.
        env = dict(os.environ)
        env["PYTHONPATH"] = os.pathsep.join(filter(None, [str(PKG_PARENT), env.get("PYTHONPATH")]))
        subprocess.Popen([sys.executable, "-m", "advisors.daemon"], stdin=subprocess.DEVNULL, stdout=err,
                         stderr=err, start_new_session=True, close_fds=True, cwd=str(home), env=env)
        err.close()
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            try:
                _connect_once().close()
                return
            except OSError:
                time.sleep(0.05)
        raise SystemExit(f"advisord did not come up within 20 seconds; see {home / 'advisord.log'} and advisord.stderr.log")


def connect(autostart: bool = True) -> socket.socket:
    try:
        return _connect_once()
    except OSError:
        if not autostart:
            raise
    _start_daemon()
    return _connect_once()


def send(s: socket.socket, obj: dict) -> None:
    s.sendall((json.dumps(obj, ensure_ascii=False) + "\n").encode())


def request(obj: dict, autostart: bool = True, timeout: float | None = None) -> dict:
    for attempt in range(40):
        s = connect(autostart)
        try:
            if timeout:
                s.settimeout(timeout)
            send(s, obj)
            f = s.makefile("rb")
            line = f.readline()
        finally:
            s.close()
        if not line:
            raise SystemExit("the daemon closed the connection without replying")
        resp = json.loads(line)
        if resp.get("retry") and autostart:  # the daemon is shutting down: wait for it to exit, then start a new one
            time.sleep(0.25)
            continue
        return resp
    return resp


def eprint(*a) -> None:
    print(*a, file=sys.stderr, flush=True)


def _die(resp: dict, as_json: bool = False) -> int:
    eprint(f"{fmt.PREFIX} error: {resp.get('error')}")
    code = int(resp.get("exit_code") or 1)
    if as_json:
        print(json.dumps({"status": "usage_error" if code == rules.USAGE_ERROR else "error", "exit_code": code,
                          "error": resp.get("error")}, ensure_ascii=False))
    return code


def _usage(a: argparse.Namespace, msg: str) -> int:
    return _die({"error": msg, "exit_code": rules.USAGE_ERROR}, getattr(a, "json", False))


# ---- ask ----

def cmd_ask(a: argparse.Namespace) -> int:
    pos = list(a.args)
    if a.session:
        if len(pos) != 1:
            return _usage(a, "usage: advisors ask --session S PROMPT (do not name an advisor when continuing a session)")
        advisor, prompt = None, pos[0]
    else:
        if len(pos) != 2:
            return _usage(a, "usage: advisors ask ADVISOR PROMPT, or advisors ask --session S PROMPT")
        advisor, prompt = pos
    if prompt == "-":
        prompt = sys.stdin.read()
    files = [os.path.abspath(f) for f in (a.files or [])]
    for f in files:
        if not os.path.isfile(f):
            return _usage(a, f"attachment not found: {f}")
    req = {"op": "ask", "advisor": advisor, "session": a.session, "prompt": prompt, "effort": a.effort,
           "files": files, "cwd": os.path.abspath(a.cwd or os.getcwd()), "cwd_explicit": a.cwd is not None,
           "tools": a.tools, "key": a.key, "label": a.label, "timeout": a.timeout}

    s = connect()
    state = {"run": None, "stop_sent": False, "signal": None, "deadline": None}

    def send_stop() -> None:
        if state["stop_sent"] or not state["run"]:
            return
        state["stop_sent"] = True
        try:
            r = request({"op": "stop", "target": state["run"], "nowait": True}, autostart=False, timeout=5)
            eprint(f"{fmt.PREFIX} got {state['signal']}; asked the daemon to stop {state['run']}"
                   + (f": {r.get('error')}" if r.get("type") == "error" else ""))
        except Exception as e:  # noqa: BLE001
            eprint(f"{fmt.PREFIX} got a signal, but the stop request could not be sent: {e}")

    def on_signal(signum, frame) -> None:  # noqa: ARG001
        state["signal"] = signal.Signals(signum).name
        state["deadline"] = time.monotonic() + 8
        send_stop()
        try:
            s.settimeout(1.0)
        except OSError:
            pass

    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)
    first = b""
    for _ in range(40):  # reconnect if the daemon is shutting down
        send(s, req)
        f = s.makefile("rb")
        while True:
            try:
                first = f.readline()
                break
            except (socket.timeout, TimeoutError):  # a signal arrived while waiting for the first line (the handler set a timeout)
                if state["deadline"] and time.monotonic() > state["deadline"]:
                    first = b""
                    break
        msg0 = json.loads(first) if first else None
        if not (msg0 and msg0.get("retry")):
            break
        s.close()
        time.sleep(0.25)
        s = connect()
    result = None
    pending = first
    while True:
        if state["deadline"] and time.monotonic() > state["deadline"]:
            break
        try:
            line, pending = (pending, None) if pending is not None else (f.readline(), None)
        except (socket.timeout, TimeoutError):
            continue
        except OSError:
            break
        if not line:
            break
        msg = json.loads(line)
        t = msg.get("type")
        if t == "accepted":
            state["run"] = msg["run"]
            _print_accepted(msg)
            if state["signal"]:
                send_stop()
        elif t == "progress":
            eprint(msg["line"])
        elif t == "error":
            return _die(msg, a.json)
        elif t == "result":
            result = msg
            break
    s.close()
    if result is None:
        if state["signal"]:
            eprint(f"{fmt.PREFIX} stop sent; exiting without waiting for the result. Check with advisors status {state['run']}")
            return rules.EXIT_CODES["aborted"]
        eprint(f"{fmt.PREFIX} lost the connection to the daemon; the result of this turn is unknown. "
               f"Check with advisors status {state['run'] or 'SESSION'}")
        return rules.EXIT_CODES["unknown"]
    return _print_result(result, a.json)


def _print_accepted(m: dict) -> None:
    head = "new session" if m.get("new_session") else "continuing session"
    if m.get("attached"):
        head = "attaching to the running turn for this idempotency key in session"
    eprint(f"{fmt.PREFIX} {head} {m['session']} ({m['advisor']}, {m['model']}) · turn {m['turn']} · run {m['run']}"
           f" · effort {m['effort']} · tools {m['tools']} · time limit {fmt.dur(m['timeout_sec'])}"
           + (f" · circuit breaker at {m['max_tool_calls']} tool calls" if m.get("max_tool_calls") else ""))
    if m.get("effort_note"):
        eprint(f"{fmt.PREFIX} effort: {m['effort_note']}")


def _print_result(r: dict, as_json: bool) -> int:
    code = int(r.get("exit_code") if r.get("exit_code") is not None else 1)
    if as_json:
        print(json.dumps(r, ensure_ascii=False, indent=1, default=str))
        return code
    if r.get("status") == "busy":
        eprint(f"{fmt.PREFIX} busy: {r.get('error')}")
        return code
    if r.get("replayed"):
        eprint(f"{fmt.PREFIX} the turn for this idempotency key already finished; returning its original result (not resubmitted)")
    if r.get("status") != "ok" and r.get("error"):
        eprint(f"{fmt.PREFIX} {r.get('status')}: {r.get('error')}")
    if r.get("note"):
        eprint(f"{fmt.PREFIX} note: {r['note']}")
    answer = r.get("answer") or ""
    if answer:
        print(answer)
    print(fmt.result_line(r), flush=True)
    return code


# ---- status ----

def cmd_status(a: argparse.Namespace) -> int:
    r = request({"op": "status", "target": a.target, "tail": a.tail, "since": a.since})
    if r.get("type") == "error":
        return _die(r)
    if a.json:
        print(json.dumps(r, ensure_ascii=False, indent=1, default=str))
        return 0
    if r.get("kind") == "overview":
        d = r["daemon"]
        print(f"advisord pid {d['pid']} · version {d['version']} · up {fmt.dur(d['uptime_sec'])} · data directory {d['home']}")
        if r["active"]:
            print("Running / queued turns:")
            for x in r["active"]:
                print(f"  {x['run']} ({x['advisor']}) phase {x['phase_text']} · {fmt.short(x['elapsed_sec'])} · {x['progress_note']}")
        else:
            print("No turns running")
        print("Concurrency groups: " + ", ".join(f"{g['group']} {g['active']}/{g['limit']}" + (f" queued {g['queued']}" if g["queued"] else "")
                                  for g in r["groups"]))
        if r.get("live_backends"):
            print("Live backend processes: " + ", ".join(f"{b['session']} ({b['kind']} pid {b['pid']})" for b in r["live_backends"]))
        if r["recent_sessions"]:
            print("Recent sessions:")
            for m in r["recent_sessions"]:
                print(f"  {m['session']} ({m['advisor']}) {m.get('turns')} turns · last {m.get('last_status') or '-'} · {m.get('updated_at')}")
        return 0
    if r.get("kind") == "session":
        print(f"{r['session']} ({r['advisor']}): {r.get('note')}")
        return 0
    _print_run_status(r)
    return 0


def _print_run_status(r: dict) -> None:
    head = f"run {r['run']} · {r.get('advisor')} · phase {r.get('phase_text')}"
    if r.get("phase_detail"):
        head += f" ({r['phase_detail']})"
    head += f" · elapsed {fmt.dur(r.get('elapsed_sec'))}"
    print(head)
    if r.get("finished"):
        print(f"result: {r.get('status')} ({r.get('status_text')}, exit code {r.get('exit_code')})" +
              (f": {r['error']}" if r.get("error") else ""))
    else:
        if r.get("queue"):
            q = r["queue"]
            print(f"queued: position {q.get('position')} in group {q.get('group')}")
        print(r.get("progress_note", ""))
    if r.get("tool_trace"):
        print("Recent tool calls:")
        for t in r["tool_trace"]:
            print(f"  {t['at']:>7.1f}s {t['tool']} {t['args']}")
    print(f"answer text {fmt.size(r.get('text_bytes') or 0)} · thinking events {r.get('thinking_events', 0)}"
          f" · tool calls {r.get('tool_calls', 0)}")
    for s in r.get("steers") or []:
        print(f"steer [{s.get('state')}] {s.get('mode')}: {s.get('text')[:120]}")
    if r.get("tail"):
        print(f"--- answer text, characters {r['tail_start']}–{r['cursor']} (continue with --since {r['cursor']}) ---")
        print(r["tail"])
    elif r.get("cursor") is not None:
        print(f"(no new answer text; cursor {r['cursor']})")
    if r.get("finished") and r.get("answer_path"):
        print(f"full answer: {r['answer_path']}")


# ---- steer / stop / list / log ----

def cmd_steer(a: argparse.Namespace) -> int:
    r = request({"op": "steer", "target": a.target, "text": a.text, "mode": a.mode})
    if r.get("type") == "error":
        return _die(r)
    if a.json:
        print(json.dumps(r, ensure_ascii=False, indent=1))
    else:
        disp, note = r.get("disposition"), str(r.get("note") or "")
        if note.startswith(f"{disp}; "):
            note = note[len(disp) + 2:]
        print(f"{disp}: {note} ({r.get('run')})")
    return 0 if r.get("disposition") in ("delivered", "queued") else 1


def cmd_stop(a: argparse.Namespace) -> int:
    r = request({"op": "stop", "target": a.target})
    if r.get("type") == "error":
        return _die(r)
    if a.json:
        print(json.dumps(r, ensure_ascii=False, indent=1, default=str))
    elif r.get("already_finished"):
        print(f"{r.get('run')} had already finished: {r.get('status')}")
    elif r.get("status"):
        print(f"{r.get('run')} stopped: {r.get('status')} ({r.get('error') or ''})")
    else:
        print(r.get("note") or json.dumps(r, ensure_ascii=False))
    return 0


def cmd_list(a: argparse.Namespace) -> int:
    r = request({"op": "list"}, timeout=180)
    if r.get("type") == "error":
        return _die(r)
    if a.json:
        print(json.dumps(r, ensure_ascii=False, indent=1, default=str))
        return 0
    v = r.get("versions") or {}
    cfg = r.get("config") or {}
    plug = r.get("plugins") or {}
    vtext = " · ".join(f"{name} {ver}" for name, ver in v.items()) or "unknown"
    print(f"versions: {vtext}" + (f" (probed at {r['probed_at']})" if r.get("probed_at") else ""))
    for name, err in (plug.get("errors") or {}).items():
        print(f"plugin {name} failed to load: {err}")
    if cfg.get("error"):
        if r.get("advisors"):
            print(f"config error (keeping the last good table): {cfg['error']}")
        else:
            print(f"config error: {cfg['error']}")
            return 1
    if not r.get("advisors"):
        print(f"No advisors configured. Copy examples/advisors.toml from the advisors repository to {cfg.get('path')}"
              " and edit it.")
        return 0
    for x in r["advisors"]:
        g = x["group"]
        print()
        print(f"{x['id']}  {x['model']}  [{x['backend']}]  {'available' if x['available'] else 'unavailable'}: {x['availability']}")
        print(f"  {x['description']}")
        print(f"  billing: {x['billing']} · efforts: {' / '.join(x['efforts'])} (default {x['default_effort']})"
              + (f" · dropped because pi does not support them: {', '.join(x['efforts_dropped'])}" if x.get("efforts_dropped") else ""))
        print(f"  concurrency: group {g['group']}, {g['active']}/{g['limit']} in use" + (f", {g['queued']} queued" if g["queued"] else ""))
        if x.get("breaker"):
            b = x["breaker"]
            print(f"  circuit breaker: at most {b['max_tool_calls']} tool calls and {fmt.dur(b['max_sec'])} per turn")
        q = x.get("quota")
        if q and q.get("text"):
            print(f"  quota: {q['text']}")
        if x.get("tools"):
            print(f"  tools with --tools read: {x['tools']}")
        c = x["capabilities"]
        print(f"  steer: {c.get('steer')}")
        print(f"  interrupt: {c.get('interrupt')}")
        print(f"  thinking: {c.get('thinking')} · usage: {c.get('usage')} · resume: {c.get('resume')}")
    return 0


def cmd_log(a: argparse.Namespace) -> int:
    meta = store.load_session(a.session) if store.valid_session_name(a.session) else None
    if not meta:
        eprint(f"no such session: {a.session}")
        return rules.USAGE_ERROR
    print(f"# Session {meta['session']}")
    print(f"advisor {meta['advisor']} ({meta['model']}) · working directory {meta['cwd']} · created {meta['created_at']}"
          f" · {meta.get('turns')} turns"
          + (f" · total cost ${meta.get('cost_total_usd'):.4f}" if meta.get("cost_total_usd") else ""))
    for n in store.turn_numbers(meta["session"]):
        tm = store.load_turn_meta(meta["session"], n) or {}
        print()
        print(f"## Turn {n} · {tm.get('created_at')} · {tm.get('status')} · {fmt.dur(tm.get('elapsed_sec'))}"
              f" · effort requested {tm.get('effort_requested') or 'default'} / sent {tm.get('effort')}"
              f" / confirmed {tm.get('effort_confirmed') or 'none'}"
              f" · tools {tm.get('tools')} · {fmt.usage_text(tm.get('usage'), tm.get('cost_usd'))}")
        if tm.get("error"):
            print(f"error: {tm['error']}")
        if tm.get("note"):
            print(f"note: {tm['note']}")
        print()
        print("### Prompt")
        print((store.read_text(store.turn_path(meta["session"], n, "prompt.md")) or "").rstrip())
        for s in tm.get("steers") or []:
            print()
            print(f"### Steer ({s.get('mode')}, at {s.get('at')} s, {s.get('state')})")
            print(s.get("text"))
        print()
        print("### Answer")
        print((store.read_text(store.turn_path(meta["session"], n, "answer.md")) or "(no answer)").rstrip())
    return 0


def cmd_shutdown(a: argparse.Namespace) -> int:
    try:
        r = request({"op": "shutdown", "force": a.force}, autostart=False, timeout=10)
    except OSError:
        print("advisord is not running")
        return 0
    print(json.dumps(r, ensure_ascii=False))
    return 0 if r.get("stopped") else 1


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="advisors", description="Consult other models (advisors) from the command line. "
                                "Start with `advisors list`.", epilog=EPILOG,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--version", action="version", version=f"advisors {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True, metavar="{ask,status,steer,stop,list,log}")

    q = sub.add_parser("ask", help="ask an advisor (in a new session or a continued one)", description=ASK_HELP, epilog=EPILOG,
                       usage="advisors ask (ADVISOR | --session S) PROMPT [options]",
                       formatter_class=argparse.RawDescriptionHelpFormatter)
    q.add_argument("args", nargs="+", metavar="ADVISOR PROMPT | PROMPT",
                   help="new session: the advisor id and the prompt; continuing (--session): only the prompt. "
                        "A prompt of - is read from stdin")
    q.add_argument("--session", help="continue this session (a session is bound to one advisor and one working directory)")
    q.add_argument("--effort", help="effort: low / medium / high / xhigh / max; an unsupported effort moves to the "
                   "nearest supported one, and the output says so")
    q.add_argument("--files", nargs="+", metavar="F", help="attachments: text files are added to the prompt; "
                   "images are sent as image input")
    q.add_argument("--cwd", help="the advisor's working directory (default: the current directory; new sessions only)")
    q.add_argument("--tools", choices=["read", "none"], default="read",
                   help="read (default): the advisor's read-only tools, listed by `advisors list` (pi: reading files, "
                        "plus web search if its config adds it; Codex: also commands in a read-only sandbox and web "
                        "search). none: no tools, so the answer rests on --files alone")
    q.add_argument("--key", help="idempotency key: resubmitting the same key returns the original turn "
                   "and is not charged again")
    q.add_argument("--label", help="a recognizable name for this turn (and for a new session)")
    q.add_argument("--timeout", type=float, metavar="SEC", help="hard timeout per turn, 1800 seconds by default; "
                   "paid advisors cannot go above 1800 seconds")
    q.add_argument("--json", action="store_true", help="print one JSON object to stdout at the end (progress stays on stderr)")
    q.set_defaults(fn=cmd_ask)

    s = sub.add_parser("status", help="show progress: phase, last backend event, tool trace, end of the answer text")
    s.add_argument("target", nargs="?", metavar="SESSION|RUN", help="omit to list all running turns")
    s.add_argument("--tail", type=int, default=400, metavar="N", help="how many characters from the end of the answer text "
                   "to show (default 400)")
    s.add_argument("--since", type=int, metavar="CURSOR", help="continue reading the answer text from this cursor "
                   "(the previous output gives it)")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_status)

    t = sub.add_parser("steer", help="steer the running turn",
                       description="Applies only to the turn that is running. Returns delivered, queued (waiting for "
                                   "the next gap), expired (the turn already ended) or unsupported. The exit code is "
                                   "0 for the first two and 1 for the last two. To start a new turn, use ask --session.")
    t.add_argument("target", metavar="SESSION|RUN")
    t.add_argument("text", nargs="?")
    t.add_argument("--mode", choices=["steer", "follow_up"], default="steer",
                   help="steer (default): deliver as soon as possible; follow_up: send after this turn's answer ends, "
                        "still as part of the same turn")
    t.add_argument("--json", action="store_true")
    t.set_defaults(fn=cmd_steer)

    st = sub.add_parser("stop", help="stop the running turn (safe to call more than once)")
    st.add_argument("target", metavar="SESSION|RUN")
    st.add_argument("--json", action="store_true")
    st.set_defaults(fn=cmd_stop)

    ls = sub.add_parser("list", help="advisors, capabilities, availability, quota and concurrency use")
    ls.add_argument("--json", action="store_true")
    ls.set_defaults(fn=cmd_list)

    lg = sub.add_parser("log", help="readable session log (prompts, steers, answers, metadata)")
    lg.add_argument("session")
    lg.set_defaults(fn=cmd_log)

    sd = sub.add_parser("_shutdown")  # maintenance: make the daemon exit
    sd.add_argument("--force", action="store_true")
    sd.set_defaults(fn=cmd_shutdown)
    return p


def main(argv: list[str] | None = None) -> int:
    p = build_parser()
    # Allow options between positional arguments, e.g. ask gpt --effort low "prompt" or steer RUN --mode follow_up "text"
    a, extra = p.parse_known_args(argv)
    if any(x.startswith("-") and x != "-" for x in extra):
        p.error(f"unrecognized arguments: {' '.join(extra)}")
    if extra and a.cmd == "ask":
        a.args = list(a.args) + extra
    elif extra and a.cmd == "steer" and a.text is None and len(extra) == 1:
        a.text = extra[0]
    elif extra:
        p.error(f"extra arguments: {' '.join(extra)}")
    if a.cmd == "steer" and not a.text:
        p.error("steer needs the text to send")
    try:
        return a.fn(a)
    except KeyboardInterrupt:
        return rules.EXIT_CODES["aborted"]
    except BrokenPipeError:
        return 1
