"""Local plugins: `*.py` files in `<data dir>/plugins/` (or `$ADVISORS_PLUGINS`), loaded by the daemon.

A plugin is a plain module. It may define:

    PROBE_BINS = ["mytool"]          # extra executables to resolve on PATH and report in `advisors list`

    async def quota_mysource(ctx, advisor, arg, fresh):   # any coroutine; registered below
        ...
        return {"text": "...", "exhausted": False, "resets_at": None}   # or None

    QUOTA_SOURCES = {"mysource": quota_mysource}

An advisor with `quota = "mysource:some-arg"` then gets `quota_mysource(ctx, advisor, "some-arg", fresh)`.
`ctx` has `bins` (resolved executables), `env` (the backend environment) and `run_cmd` (an async helper:
`await ctx.run_cmd(argv, timeout)` -> (returncode, stdout, stderr)). `fresh` is True when a turn has just
failed and the daemon wants to know whether the quota is exhausted; otherwise cached data is fine.

The returned dict is shown as the advisor's quota. Recognised keys: `text` (one line for people),
`exhausted` (turns a failed or rate-limited turn into quota_exhausted, exit code 5) and `resets_at`.

Plugins run inside the daemon with your user's permissions, so only put files there that you wrote or read.
A plugin that fails to import is skipped and the error shows in `advisors list`.
"""

from __future__ import annotations

import importlib.util
import logging
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

from . import config

log = logging.getLogger("advisord")


@dataclass
class Plugins:
    quota_sources: dict = field(default_factory=dict)
    bins: list[str] = field(default_factory=list)
    loaded: list[str] = field(default_factory=list)
    errors: dict[str, str] = field(default_factory=dict)


def plugins_dir() -> Path:
    raw = os.environ.get("ADVISORS_PLUGINS")
    return Path(raw).expanduser() if raw else config.home() / "plugins"


def load(directory: Path | None = None) -> Plugins:
    out = Plugins()
    d = directory or plugins_dir()
    try:
        files = sorted(p for p in d.glob("*.py") if not p.name.startswith(("_", ".")))
    except OSError:
        return out
    for path in files:
        name = f"advisors_plugin_{path.stem}"
        try:
            spec = importlib.util.spec_from_file_location(name, path)
            if not spec or not spec.loader:
                raise ImportError("cannot load")
            mod = importlib.util.module_from_spec(spec)
            sys.modules[name] = mod
            spec.loader.exec_module(mod)
        except Exception as e:  # noqa: BLE001
            sys.modules.pop(name, None)
            out.errors[path.name] = f"{type(e).__name__}: {e}"
            log.exception("plugin %s failed to load", path)
            continue
        for src, fn in (getattr(mod, "QUOTA_SOURCES", None) or {}).items():
            if src in out.quota_sources:
                out.errors[path.name] = f"quota source {src!r} is already defined by another plugin"
                continue
            out.quota_sources[src] = fn
        for b in getattr(mod, "PROBE_BINS", None) or []:
            if isinstance(b, str) and b and b not in out.bins:
                out.bins.append(b)
        out.loaded.append(path.name)
    return out
