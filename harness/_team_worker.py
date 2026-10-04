"""Subprocess entry point that hosts one team policy.

Launched by team_runtime.TeamRuntime as:
    python -I _team_worker.py <harness_root> <team_dir> <state_dir>

Protocol: one JSON object per line on a private pipe (the original stdout,
duplicated before team code loads; team prints go to stderr). Requests are
{"hook": name, "args": {...}, "state": {...}}; replies are
{"ok": bool, "result": ..., "state": {...}, "commands": [...],
 "violations": [...], "error": str|None}.

Restrictions are best-effort Python-level guards plus OS rlimits. They stop
accidental and casual misuse and make every attempt visible; they are not a
security boundary against deliberately adversarial code.
"""
from __future__ import annotations

import builtins
import json
import os
import sys
import traceback

harness_root, team_dir, state_dir = sys.argv[1], sys.argv[2], sys.argv[3]

# Load everything the worker itself needs before the import guard goes up.
sys.path.insert(0, harness_root)
from harness import team_api  # noqa: E402
from harness.team_api import Context, Decision, Message, StateStore, ToolRequest, ToolSpec  # noqa: E402

ALLOWED_MODULES = frozenset({
    "team_api", "harness.team_api",
    "math", "random", "re", "json", "collections", "itertools", "functools",
    "dataclasses", "typing", "hashlib", "statistics", "string", "enum", "copy",
    "heapq", "bisect", "datetime", "textwrap", "operator", "__future__",
})
for _m in list(ALLOWED_MODULES):
    try:
        __import__(_m)
    except ImportError:
        pass
sys.modules["team_api"] = team_api

TEAM_MODULE = "team_policy"
violations: list[dict] = []
_real_import = builtins.__import__
_real_open = builtins.open
state_dir_real = os.path.realpath(state_dir)


def _from_team_code(globals_) -> bool:
    name = (globals_ or {}).get("__name__", "")
    return name == TEAM_MODULE or name.startswith(TEAM_MODULE + ".")


def guarded_import(name, globals=None, locals=None, fromlist=(), level=0):
    if _from_team_code(globals) and level == 0:
        top = name.split(".")[0]
        if name not in ALLOWED_MODULES and (top not in ALLOWED_MODULES or top in ("team_api", "harness")):
            violations.append({"kind": "import_blocked", "detail": name})
            raise ImportError(f"import of {name!r} is not allowed in team harness code")
    return _real_import(name, globals, locals, fromlist, level)


def guarded_open(file, mode="r", *args, **kwargs):
    path = os.path.realpath(os.path.join(state_dir_real, file) if not os.path.isabs(str(file)) else file)
    if not (path == state_dir_real or path.startswith(state_dir_real + os.sep)):
        violations.append({"kind": "fs_blocked", "detail": f"{mode} {file}"})
        raise PermissionError(f"team harness code may only open files under state/: {file!r}")
    return _real_open(path, mode, *args, **kwargs)


# Second layer: an audit hook that is active only while team code runs. It
# catches filesystem, process and network access that slips past the
# namespace guards (e.g. via a module reached through an allowed import).
_in_team = [False]
_BLOCKED_AUDIT_PREFIXES = ("socket.", "subprocess.", "os.system", "os.exec", "os.posix_spawn",
                           "os.spawn", "os.fork", "os.kill", "os.remove", "os.rename",
                           "os.rmdir", "os.mkdir", "os.chmod", "os.symlink", "os.link",
                           "os.truncate", "shutil.", "urllib.", "http.", "ctypes.", "pty.")


def _audit(event, args):
    if not _in_team[0]:
        return
    if event == "open":
        path = args[0]
        if isinstance(path, int):
            return
        real = os.path.realpath(os.fsdecode(path))
        if not (real == state_dir_real or real.startswith(state_dir_real + os.sep)):
            violations.append({"kind": "fs_blocked", "detail": f"{args[1]} {path}"})
            raise PermissionError(f"team harness code may only open files under state/: {path!r}")
    elif event.startswith(_BLOCKED_AUDIT_PREFIXES):
        kind = "network_blocked" if event.startswith(("socket.", "urllib.", "http.")) else "os_blocked"
        violations.append({"kind": kind, "detail": event})
        raise PermissionError(f"{event} is not allowed in team harness code")


sys.addaudithook(_audit)


def _blocked(name):
    def f(*a, **k):
        violations.append({"kind": "builtin_blocked", "detail": name})
        raise PermissionError(f"{name}() is not allowed in team harness code")
    return f


# Protocol channel: keep the real stdout for replies; team prints go to stderr.
_proto_out = os.fdopen(os.dup(1), "w", buffering=1)
os.dup2(2, 1)
sys.stdout = sys.stderr

# Build the restricted builtins namespace for the team module only.
team_builtins = dict(vars(builtins))
team_builtins["__import__"] = guarded_import
team_builtins["open"] = guarded_open
for _name in ("exec", "eval", "compile", "input", "breakpoint", "exit", "quit"):
    team_builtins[_name] = _blocked(_name)


def load_policy_class():
    path = os.path.join(team_dir, "policy.py")
    with _real_open(path, encoding="utf-8") as fh:
        source = fh.read()
    module = type(sys)(TEAM_MODULE)
    module.__file__ = path
    module.__builtins__ = team_builtins
    sys.modules[TEAM_MODULE] = module
    code = compile(source, path, "exec")
    _in_team[0] = True
    try:
        exec(code, module.__dict__)
    finally:
        _in_team[0] = False
    cls = module.__dict__.get("TeamPolicy")
    if cls is None:
        raise RuntimeError("policy.py does not define TeamPolicy")
    return cls


def to_jsonable(value):
    if isinstance(value, Decision):
        return {"__type__": "Decision", "action": value.action, "reason": value.reason,
                "visible_to": value.visible_to}
    if isinstance(value, Message):
        return {"__type__": "Message", **value.__dict__}
    if isinstance(value, ToolSpec):
        return {"__type__": "ToolSpec", **value.__dict__}
    if isinstance(value, (list, tuple)):
        return [to_jsonable(v) for v in value]
    return value


policy = None
store = StateStore()


def handle(req):
    global policy, store
    hook, args = req["hook"], req.get("args", {})
    store = StateStore(req.get("state") or {})
    ctx = None
    if hook == "__init__":
        cls = load_policy_class()
        tools = [ToolSpec(**t) for t in args["tools"]]
        _in_team[0] = True
        try:
            policy = cls(list(args["agents"]), tools, store)
        finally:
            _in_team[0] = False
        result = None
    else:
        if policy is None:
            raise RuntimeError("policy not initialized")
        policy.state = store  # convention: policies keep the store as self.state
        ctx = Context.from_dict(args["ctx"]) if "ctx" in args else None
        if hook not in _DISPATCH:
            raise ValueError(f"unknown hook {hook!r}")
        fn = getattr(policy, hook)
        _in_team[0] = True
        try:
            result = _dispatch(hook, fn, args, ctx)
        finally:
            _in_team[0] = False
    return result, ctx


_DISPATCH = {
    "next_speaker": lambda fn, a, ctx: fn(ctx),
    "visible_context": lambda fn, a, ctx: fn(a["agent"], ctx),
    "on_tool_request": lambda fn, a, ctx: fn(ToolRequest(**a["req"]), ctx),
    "on_message": lambda fn, a, ctx: fn(a["agent"], a["text"], ctx),
    "extra_tools": lambda fn, a, ctx: fn(),
    "on_extra_tool": lambda fn, a, ctx: fn(a["agent"], a["name"], a["args"], ctx),
    "is_done": lambda fn, a, ctx: fn(ctx),
}


def _dispatch(hook, fn, args, ctx):
    if hook not in _DISPATCH:
        raise ValueError(f"unknown hook {hook!r}")
    return _DISPATCH[hook](fn, args, ctx)


for line in sys.stdin:
    try:
        req = json.loads(line)
    except json.JSONDecodeError:
        continue
    violations.clear()
    reply = {"ok": True, "result": None, "state": None, "commands": [], "violations": [], "error": None}
    try:
        result, ctx = handle(req)
        reply["result"] = to_jsonable(result)
        reply["commands"] = ctx.commands() if ctx is not None else []
        reply["state"] = store.to_dict()
        json.dumps(reply)  # surface non-serializable results/state as errors
    except BaseException as e:  # noqa: BLE001 - report everything to the parent
        reply = {"ok": False, "result": None, "state": None, "commands": [],
                 "error": f"{type(e).__name__}: {e}",
                 "traceback": traceback.format_exc(limit=5)}
    reply["violations"] = list(violations)
    _proto_out.write(json.dumps(reply, default=str) + "\n")
    _proto_out.flush()
