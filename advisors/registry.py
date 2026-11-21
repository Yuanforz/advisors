"""The advisor table (DESIGN.md section 3) and the static part of each capability declaration (section 5).

Advisors come from a TOML file: `$ADVISORS_CONFIG`, or `advisors.toml` in the data directory. See
`examples/advisors.toml` for a commented example. Nothing is built in: with no config file, `advisors list`
says so and points at the example.

Test advisors: only when the data directory is not the default one, the JSON file named by
ADVISORS_TEST_ADVISORS is read as well. Their ids must start with `test-`. A test advisor may use
`--no-extensions` only if its model comes from a `faux*` provider and it lists the extensions it loads
explicitly. The config file has no key for `--no-extensions`, so a real advisor can never get it (section 9).
"""

from __future__ import annotations

import json
import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from . import config

PI = "pi-rpc"
CODEX = "codex-app-server"
BACKENDS = (PI, CODEX)

EFFORT_ORDER = ("off", "minimal", "low", "medium", "high", "xhigh", "max")
DEFAULT_GROUP_LIMIT = 2
DEFAULT_READ_TOOLS = ("read", "grep", "find", "ls")
BUILTIN_QUOTA_SOURCES = ("codex", "vertex-budget")

_ID_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
_TOOL_RE = re.compile(r"^[A-Za-z0-9_.*-]+$")
_ADVISOR_KEYS = {"backend", "model", "efforts", "default_effort", "billing", "group", "paid", "description",
                 "quota", "read_tools", "notes"}
_NOTE_KEYS = {"steer", "interrupt", "thinking", "usage", "resume"}


@dataclass(frozen=True)
class Advisor:
    id: str
    backend: str
    model: str
    efforts: tuple[str, ...]
    default_effort: str
    billing: str
    group: str
    paid: bool
    description: str
    # Quota source: "codex", "vertex-budget", "<plugin>" or "<plugin>:<argument>", or None
    quota: str | None = None
    # pi only: the tool allowlist for `--tools read`
    read_tools: tuple[str, ...] = DEFAULT_READ_TOOLS
    # Overrides for the capability text shown by `advisors list`
    notes: dict = field(default_factory=dict)
    # The next four are for test advisors only
    env: dict = field(default_factory=dict)
    extensions: tuple[str, ...] = ()
    no_extensions: bool = False
    test: bool = False

    @property
    def provider(self) -> str:
        return self.model.split("/", 1)[0]


# Static capability text per backend (section 5). The startup probe adds versions and handshake results,
# see probe.py. An advisor's `notes` table replaces individual entries.
BACKEND_CAPS: dict[str, dict] = {
    CODEX: {
        "steer": "turn/steer, applied within the running turn",
        "interrupt": "turn/interrupt",
        "thinking": "event counts only, no raw text",
        "usage": "tokens; quota from account/rateLimits",
        "resume": "thread/resume",
    },
    PI: {
        "steer": "pi steer, delivered between tool calls; with no tool calls it waits until the answer is written",
        "interrupt": "pi abort",
        "thinking": "event counts only, no raw text",
        "usage": "tokens, plus USD when the provider reports cost",
        "resume": "pi --session-id",
    },
}

TEST_CAPS = {
    "steer": "pi steer (test advisor)",
    "interrupt": "pi abort (test advisor)",
    "thinking": "event counts only",
    "usage": "pi usage events",
    "resume": "pi --session-id",
}


class RegistryError(Exception):
    pass


def config_path() -> Path:
    raw = os.environ.get("ADVISORS_CONFIG")
    return Path(raw).expanduser() if raw else config.home() / "advisors.toml"


def _wide(ch: str) -> bool:
    return "\u2e80" <= ch <= "\u9fff" or "\uff00" <= ch <= "\uffef" or "\u3000" <= ch <= "\u303f"


def _one_line(text: str) -> str:
    """Join a multi-line TOML string into one line. Lines are joined with a space, except between CJK
    text, which has no spaces between sentences."""
    out = ""
    for line in (ln.strip() for ln in text.splitlines()):
        if not line:
            continue
        line = " ".join(line.split())
        out += line if not out or (_wide(out[-1]) and _wide(line[0])) else " " + line
    return out


def _strings(value, what: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not value or not all(isinstance(v, str) and v for v in value):
        raise RegistryError(f"{what} must be a non-empty list of strings")
    return tuple(value)


def _parse_advisor(aid: str, d: dict) -> Advisor:
    where = f"[advisors.{aid}]"
    if not _ID_RE.match(aid) or aid.startswith("test-"):
        raise RegistryError(f"{where}: ids are lowercase letters, digits, '-' and '_', start with a letter, "
                            "and may not start with 'test-'")
    if not isinstance(d, dict):
        raise RegistryError(f"{where} must be a table")
    unknown = set(d) - _ADVISOR_KEYS
    if unknown:
        raise RegistryError(f"{where}: unknown keys {', '.join(sorted(unknown))}")
    backend = d.get("backend")
    if backend not in BACKENDS:
        raise RegistryError(f"{where}: backend must be one of {', '.join(BACKENDS)}")
    model = d.get("model")
    if not isinstance(model, str) or not model.strip():
        raise RegistryError(f"{where}: model is required")
    if backend == PI and "/" not in model:
        raise RegistryError(f"{where}: pi models are written provider/model, for example anthropic/claude-sonnet-5-5")
    efforts = _strings(d.get("efforts"), f"{where} efforts")
    bad = [e for e in efforts if e not in EFFORT_ORDER]
    if bad:
        raise RegistryError(f"{where}: unknown efforts {', '.join(bad)}; known: {', '.join(EFFORT_ORDER)}")
    efforts = tuple(sorted(dict.fromkeys(efforts), key=EFFORT_ORDER.index))
    default = d.get("default_effort", efforts[-1])
    if default not in efforts:
        raise RegistryError(f"{where}: default_effort {default!r} is not in efforts")
    paid = d.get("paid")
    if not isinstance(paid, bool):
        raise RegistryError(f"{where}: paid = true or false is required (paid advisors get the circuit breaker)")
    quota = d.get("quota")
    if quota is not None and (not isinstance(quota, str) or not quota.strip()):
        raise RegistryError(f"{where}: quota must be a string")
    if quota == "codex" and backend != CODEX:
        raise RegistryError(f"{where}: quota = \"codex\" only works with the codex-app-server backend")
    read_tools = DEFAULT_READ_TOOLS
    if "read_tools" in d:
        if backend != PI:
            raise RegistryError(f"{where}: read_tools only applies to the pi-rpc backend")
        read_tools = _strings(d["read_tools"], f"{where} read_tools")
        bad = [t for t in read_tools if not _TOOL_RE.match(t)]
        if bad:
            raise RegistryError(f"{where}: bad tool names {', '.join(bad)}")
    notes = d.get("notes") or {}
    if not isinstance(notes, dict) or set(notes) - _NOTE_KEYS or not all(isinstance(v, str) for v in notes.values()):
        raise RegistryError(f"{where}.notes: string values for {', '.join(sorted(_NOTE_KEYS))} only")
    for key in ("billing", "group", "description"):
        if key in d and not isinstance(d[key], str):
            raise RegistryError(f"{where}: {key} must be a string")
    default_group = "codex" if backend == CODEX else model.split("/", 1)[0]
    return Advisor(
        id=aid, backend=backend, model=model.strip(), efforts=efforts, default_effort=default,
        billing=d.get("billing", "paid" if paid else "subscription"), group=d.get("group") or default_group,
        paid=paid, description=_one_line(d.get("description", "")), quota=quota,
        read_tools=read_tools, notes=dict(notes),
    )


def load_config(path: Path | None = None) -> tuple[dict[str, Advisor], dict[str, int]]:
    """Returns (advisors, group limits). A missing file means no advisors; a broken file raises RegistryError."""
    path = path or config_path()
    try:
        with open(path, "rb") as f:
            raw = tomllib.load(f)
    except FileNotFoundError:
        return {}, {}
    except (OSError, tomllib.TOMLDecodeError) as e:
        raise RegistryError(f"cannot read {path}: {e}") from e
    unknown = set(raw) - {"advisors", "groups"}
    if unknown:
        raise RegistryError(f"{path}: unknown top-level keys {', '.join(sorted(unknown))}")
    groups = raw.get("groups") or {}
    if not isinstance(groups, dict) or not all(isinstance(v, int) and not isinstance(v, bool) and v >= 1
                                               for v in groups.values()):
        raise RegistryError(f"{path}: [groups] values must be integers >= 1")
    table = raw.get("advisors") or {}
    if not isinstance(table, dict):
        raise RegistryError(f"{path}: [advisors] must be a table of tables")
    advisors = {aid: _parse_advisor(aid, d) for aid, d in table.items()}
    return advisors, dict(groups)


def _load_test_advisors() -> dict[str, Advisor]:
    path = os.environ.get("ADVISORS_TEST_ADVISORS")
    if not path or not config.is_test_home():
        return {}
    with open(path, encoding="utf-8") as f:
        raw = json.load(f)
    out: dict[str, Advisor] = {}
    for d in raw.get("advisors", []):
        aid = d["id"]
        if not aid.startswith("test-"):
            raise RegistryError(f"test advisor ids must start with test-: {aid}")
        model = d["model"]
        no_ext = bool(d.get("no_extensions"))
        exts = tuple(d.get("extensions", ()))
        if no_ext and (not model.split("/", 1)[0].startswith("faux") or not exts):
            raise RegistryError(f"{aid}: --no-extensions needs a faux model and an explicit extension list")
        out[aid] = Advisor(
            id=aid,
            backend=d.get("backend", PI),
            model=model,
            efforts=tuple(d.get("efforts", ("low", "medium", "high"))),
            default_effort=d.get("default_effort", "medium"),
            billing=d.get("billing", "test"),
            group=d.get("group", "test"),
            paid=bool(d.get("paid", False)),
            description=d.get("description", "test advisor"),
            quota=d.get("quota"),
            read_tools=tuple(d.get("read_tools", DEFAULT_READ_TOOLS)),
            env=dict(d.get("env", {})),
            extensions=exts,
            no_extensions=no_ext,
            test=True,
        )
    return out


class Registry:
    """The advisor table as the daemon sees it. Reloads the config file when it changes on disk.

    A broken edit keeps the last good table and reports the error (`error`), so a typo never takes
    running advisors away.
    """

    def __init__(self, strict: bool = True) -> None:
        self.path = config_path()
        self.advisors: dict[str, Advisor] = {}
        self.groups: dict[str, int] = {}
        self.error: str | None = None
        self._stamp: tuple | None = None
        self.refresh(strict=strict)

    def _file_stamp(self) -> tuple | None:
        try:
            st = self.path.stat()
            return (st.st_mtime_ns, st.st_size)
        except OSError:
            return None

    def refresh(self, strict: bool = False) -> None:
        stamp = self._file_stamp()
        if stamp == self._stamp and self._stamp is not None:
            return
        try:
            advisors, groups = load_config(self.path)
            test = _load_test_advisors()
        except RegistryError as e:
            if strict:
                raise
            self.error = str(e)
            return
        if test and os.environ.get("ADVISORS_ONLY_TEST") and config.is_test_home():
            advisors = {}
        advisors.update(test)
        self.advisors, self.groups, self.error, self._stamp = advisors, groups, None, stamp

    def get(self, aid: str | None) -> Advisor | None:
        return self.advisors.get(aid) if aid else None

    def group_limit(self, group: str) -> int:
        if group in self.groups:
            return self.groups[group]
        raw = os.environ.get("ADVISORS_TEST_GROUP_LIMIT") if config.is_test_home() else None
        return int(raw) if raw else DEFAULT_GROUP_LIMIT

    def first(self, backend: str) -> Advisor | None:
        return next((a for a in self.advisors.values() if a.backend == backend and not a.test), None)


def all_advisors() -> dict[str, Advisor]:
    return Registry().advisors


def caps_for(advisor: Advisor) -> dict:
    if advisor.test:
        return dict(TEST_CAPS)
    caps = dict(BACKEND_CAPS.get(advisor.backend, {}))
    caps.update(advisor.notes)
    return caps
