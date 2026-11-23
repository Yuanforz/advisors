"""Capability declarations and version probing (DESIGN.md section 5).

When the daemon starts it reads the versions of pi, codex and any executables plugins ask for. If they
match the last recorded probe (and every configured pi model was covered), that probe is reused;
otherwise it probes again. Codex only does the initialize handshake and pi only answers
get_available_models: no prompt is sent and no quota is spent.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil

from . import config, store
from .quota import run_cmd
from .registry import CODEX, PI, Advisor, Registry, caps_for
from .rules import EFFORT_ORDER

log = logging.getLogger("advisord")

CORE_BINS = ("pi", "codex", "node")


def resolve_bins(env: dict, extra: list[str] | tuple[str, ...] = ()) -> tuple[dict, dict]:
    """Find absolute paths for pi / codex / node (and plugin executables), and put their directories
    at the front of PATH.

    pi is a `#!/usr/bin/env node` script, and version managers such as fnm hand out per-shell
    directories that disappear with the shell, so the real paths are used.
    """
    path = env.get("PATH", os.defpath)
    bins = {}
    for name in (*CORE_BINS, *extra):
        found = shutil.which(name, path=path)
        bins[name] = os.path.realpath(found) if found else name
    env = dict(env)
    dirs = [os.path.dirname(bins[n]) for n in ("node", "codex", *extra) if os.path.isabs(bins[n])]
    env["PATH"] = os.pathsep.join(dict.fromkeys(dirs + path.split(os.pathsep)))
    return bins, env


async def tool_versions(bins: dict, env: dict, names: list[str]) -> dict:
    async def one(name: str) -> str | None:
        try:
            rc, out, err = await run_cmd([bins.get(name, name), "--version"], 30, env=env)
        except OSError as e:
            return f"unavailable: {e}"
        text = (out or err).strip().splitlines()
        return text[0] if text else None

    vals = await asyncio.gather(*(one(n) for n in names))
    return dict(zip(names, vals))


def pi_levels(model: dict) -> list[str]:
    """Supported efforts from pi's thinkingLevelMap: null means unsupported; xhigh / max must be listed."""
    if not model.get("reasoning"):
        return ["off"]
    m = model.get("thinkingLevelMap") or {}
    out = []
    for lv in EFFORT_ORDER:
        if lv == "off":
            continue
        if lv in ("xhigh", "max"):
            if m.get(lv) is not None:
                out.append(lv)
        elif lv not in m or m[lv] is not None:
            out.append(lv)
    return out


async def probe_pi(bins: dict, env: dict, advisors: dict[str, Advisor]) -> dict:
    from .backends.base import JsonlProcess
    from .backends.pi_rpc import FIXED_FLAGS

    p = JsonlProcess("pi-probe")
    fut: asyncio.Future = asyncio.get_running_loop().create_future()

    def on(obj: dict) -> None:
        if obj.get("__eof__") and not fut.done():
            fut.set_exception(RuntimeError("the pi probe process exited"))
        elif obj.get("type") == "response" and obj.get("id") == "probe-models" and not fut.done():
            fut.set_result(obj)

    p.on_message = on
    try:
        await p.start([bins["pi"], "--mode", "rpc", "--no-session", *FIXED_FLAGS], str(config.home()), env,
                      config.home() / "probe.stderr.log")
        await p.send({"id": "probe-models", "type": "get_available_models"})
        resp = await asyncio.wait_for(fut, 60)
    finally:
        await p.close(grace=5)
    models = {f"{m.get('provider')}/{m.get('id')}": m for m in (resp.get("data") or {}).get("models") or []}
    out = {}
    for a in advisors.values():
        if a.backend != PI or a.test:
            continue
        m = models.get(a.model)
        out[a.model] = {"found": m is not None, "levels": pi_levels(m) if m else []}
    return {"ok": True, "models": out}


async def probe_codex(bins: dict, env: dict, advisor: Advisor | None) -> dict:
    from .backends.codex_app import CodexBackend

    if advisor is None:
        return {"ok": False, "skipped": True, "error": "no codex-app-server advisor is configured"}
    b = CodexBackend(advisor, {"session": "probe"}, env, bins)
    try:
        init = await asyncio.wait_for(b.handshake(str(config.home()), config.home() / "probe.stderr.log"), 60)
        return {"ok": True, "userAgent": init.get("userAgent")}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)}
    finally:
        await b.close()


def _pi_models(registry: Registry) -> set[str]:
    return {a.model for a in registry.advisors.values() if a.backend == PI and not a.test}


async def probe(bins: dict, env: dict, registry: Registry, extra_bins: list[str] | tuple[str, ...] = ()) -> dict:
    versions = await tool_versions(bins, env, ["pi", "codex", *extra_bins])
    cached = None
    try:
        cached = store.read_json(config.caps_path())
    except (OSError, ValueError):
        pass
    codex_adv = registry.first(CODEX)
    if (cached and cached.get("versions") == versions and cached.get("codex_app_server") and cached.get("pi")
            and _pi_models(registry) <= set(((cached.get("pi") or {}).get("models") or {}))
            and bool((cached.get("codex_app_server") or {}).get("skipped")) == (codex_adv is None)):
        cached["reused"] = True
        return cached
    caps: dict = {"versions": versions, "probed_at": store.now_iso(), "bins": bins}
    pi_res, codex_res = await asyncio.gather(
        _guard(probe_pi(bins, env, registry.advisors)), _guard(probe_codex(bins, env, codex_adv)))
    caps["pi"] = pi_res
    caps["codex_app_server"] = codex_res
    if cached and cached.get("versions") != versions:
        caps["previous_versions"] = cached.get("versions")
    store.write_json(config.caps_path(), caps)
    caps["reused"] = False
    return caps


async def _guard(coro) -> dict:
    try:
        return await coro
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)}


def effective_efforts(advisor: Advisor, caps: dict | None) -> tuple[tuple[str, ...], list[str]]:
    """The configured efforts intersected with what pi reports. Returns (efforts, dropped efforts)."""
    if advisor.backend != PI or advisor.test or not caps:
        return advisor.efforts, []
    info = ((caps.get("pi") or {}).get("models") or {}).get(advisor.model)
    if not info or not info.get("found"):
        return advisor.efforts, []
    sup = set(info.get("levels") or [])
    keep = tuple(e for e in advisor.efforts if e in sup)
    dropped = [e for e in advisor.efforts if e not in sup]
    return (keep or advisor.efforts), dropped


def capability_view(advisor: Advisor, caps: dict | None) -> dict:
    view = caps_for(advisor)
    if advisor.backend == CODEX and caps and not (caps.get("codex_app_server") or {}).get("ok", True):
        view["steer"] = "unsupported (app-server handshake failed; falling back to codex exec --json)"
        view["interrupt"] = "can only end the codex exec process"
        view["mode"] = "codex exec --json"
    return view


def read_tools_view(advisor: Advisor) -> str:
    """What an advisor can use under --tools read, for `advisors list`."""
    if advisor.backend == PI:
        return ", ".join(advisor.read_tools)
    return "commands in a read-only sandbox, web search, view_image"


def availability(advisor: Advisor, caps: dict | None) -> tuple[bool, str]:
    if not caps:
        return True, "not probed yet"
    if advisor.backend == PI:
        info = ((caps.get("pi") or {}).get("models") or {}).get(advisor.model)
        if advisor.test:
            return True, "test advisor"
        if not (caps.get("pi") or {}).get("ok"):
            return False, f"pi probe failed: {(caps.get('pi') or {}).get('error')}"
        if info and not info.get("found"):
            return False, f"pi has no model {advisor.model}"
        return True, "available"
    c = caps.get("codex_app_server") or {}
    if c.get("ok"):
        return True, "available (app-server)"
    return True, f"app-server handshake failed, using the codex exec fallback: {c.get('error')}"
