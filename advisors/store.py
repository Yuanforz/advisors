"""What is written to disk (DESIGN.md section 8).

sessions/<session>/meta.json               advisor, model, backend session id, creation time, total usage
sessions/<session>/turns/<n>.prompt.md
sessions/<session>/turns/<n>.answer.md
sessions/<session>/turns/<n>.meta.json     requested / confirmed effort, elapsed time, usage, cost, status, idempotency key
sessions/<session>/turns/<n>.events.jsonl  raw backend events, slimmed (streaming text deltas removed)
keys/<sha256>.json                         idempotency key -> run
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from . import config

MAX_EVENT_STR = 2000  # the longest string kept in an event, in characters


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{secrets.token_hex(3)}.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)


def write_json(path: Path, obj: Any) -> None:
    atomic_write(path, json.dumps(obj, ensure_ascii=False, indent=1, default=str) + "\n")


def read_json(path: Path) -> Any:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def session_dir(name: str) -> Path:
    return config.sessions_dir() / name


def turn_path(name: str, n: int, kind: str) -> Path:
    return session_dir(name) / "turns" / f"{n}.{kind}"


def valid_session_name(name: str) -> bool:
    return bool(name) and len(name) <= 80 and all(c.isalnum() or c in "-_." for c in name) and not name.startswith(".")


def new_session_name(advisor_id: str) -> str:
    stamp = datetime.now().strftime("%m%d-%H%M")
    while True:
        name = f"{advisor_id}-{stamp}-{secrets.token_hex(2)}"
        if not session_dir(name).exists():
            return name


def load_session(name: str) -> dict | None:
    p = session_dir(name) / "meta.json"
    if not p.exists():
        return None
    return read_json(p)


def save_session(meta: dict) -> None:
    write_json(session_dir(meta["session"]) / "meta.json", meta)


def list_sessions() -> list[dict]:
    d = config.sessions_dir()
    if not d.exists():
        return []
    out = []
    for p in d.iterdir():
        m = p / "meta.json"
        if m.exists():
            try:
                out.append(read_json(m))
            except (OSError, ValueError):
                continue
    out.sort(key=lambda m: m.get("updated_at", m.get("created_at", "")), reverse=True)
    return out


def load_turn_meta(name: str, n: int) -> dict | None:
    p = turn_path(name, n, "meta.json")
    return read_json(p) if p.exists() else None


def save_turn_meta(name: str, n: int, meta: dict) -> None:
    write_json(turn_path(name, n, "meta.json"), meta)


def read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return None


def turn_numbers(name: str) -> list[int]:
    d = session_dir(name) / "turns"
    if not d.exists():
        return []
    nums = set()
    for p in d.iterdir():
        head = p.name.split(".", 1)[0]
        if head.isdigit():
            nums.add(int(head))
    return sorted(nums)


def _slim(obj: Any, depth: int = 0) -> Any:
    if isinstance(obj, str):
        if len(obj) > MAX_EVENT_STR:
            return obj[:MAX_EVENT_STR] + f"… ({len(obj)} characters in total, truncated)"
        return obj
    if depth > 12:
        return "…"
    if isinstance(obj, dict):
        return {k: _slim(v, depth + 1) for k, v in obj.items()}
    if isinstance(obj, list):
        if len(obj) > 200:
            return [_slim(v, depth + 1) for v in obj[:200]] + [f"… ({len(obj)} items in total)"]
        return [_slim(v, depth + 1) for v in obj]
    return obj


class EventLog:
    """The slimmed event stream of one turn. Callers filter out streaming text-delta events first."""

    def __init__(self, name: str, n: int) -> None:
        p = turn_path(name, n, "events.jsonl")
        p.parent.mkdir(parents=True, exist_ok=True)
        self._f = open(p, "a", encoding="utf-8")
        self._t0 = time.monotonic()

    def write(self, source: str, event: Any) -> None:
        if self._f.closed:
            return
        rec = {"t": round(time.monotonic() - self._t0, 3), "src": source, "ev": _slim(event)}
        self._f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
        self._f.flush()

    def close(self) -> None:
        if not self._f.closed:
            self._f.close()


# ---- idempotency keys ----

def _key_path(key: str) -> Path:
    return config.keys_dir() / (hashlib.sha256(key.encode()).hexdigest()[:32] + ".json")


def prompt_digest(advisor: str, session: str | None, prompt: str, files: list[str]) -> str:
    h = hashlib.sha256()
    for part in (advisor, session or "", prompt, "\0".join(files)):
        h.update(part.encode())
        h.update(b"\1")
    return h.hexdigest()[:24]


def load_key(key: str) -> dict | None:
    p = _key_path(key)
    return read_json(p) if p.exists() else None


def save_key(key: str, run_id: str, digest: str) -> None:
    write_json(_key_path(key), {"key": key, "run": run_id, "digest": digest, "created_at": now_iso()})


# ---- cleanup after a daemon restart ----

def mark_orphans_unknown() -> list[str]:
    """Mark turns still recorded as queued / running on disk as unknown (the daemon did not finish them before it last exited)."""
    changed = []
    for meta in list_sessions():
        name = meta["session"]
        for n in turn_numbers(name):
            tm = load_turn_meta(name, n)
            if tm and tm.get("status") in (None, "queued", "running"):
                tm["status"] = "unknown"
                tm["exit_code"] = 9
                tm["note"] = "the daemon exited before this turn finished; the result is unknown"
                tm["ended_at"] = tm.get("ended_at") or now_iso()
                save_turn_meta(name, n, tm)
                changed.append(f"{name}:{n}")
        if meta.get("busy_run"):
            meta["busy_run"] = None
            save_session(meta)
    return changed
