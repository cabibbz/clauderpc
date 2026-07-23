"""Correctness test for hook merging: must never clobber a user's own config."""
import json, os, sys, tempfile
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from claude_rpc import wire_hooks, hooks_status, settings_path_for, HOOK_EVENTS

EXE = r"C:\apps\dist\claude-rpc\claude-rpc.exe"
OLD_EXE = r"D:\old\claude-rpc.exe"

PRE_EXISTING = {
    "permissions": {"allow": ["Bash(npm:*)"]},
    "env": {"FOO": "bar"},
    "hooks": {
        "PreToolUse": [
            {"matcher": "Bash", "hooks": [
                {"type": "command", "command": "my-audit-script.exe", "timeout": 5}]}
        ],
        "Stop": [{"hooks": [{"type": "command", "command": "notify-me.exe"}]}],
        "Notification": [{"hooks": [{"type": "command", "command": "beep.exe"}]}],
    },
}

proj = os.path.join(tempfile.mkdtemp(prefix="wiretest_"), "myproject")
os.makedirs(os.path.join(proj, ".claude"))
with open(settings_path_for(proj), "w", encoding="utf-8") as f:
    json.dump(PRE_EXISTING, f, indent=2)

fails = []


def check(label, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + label + (f"  {detail}" if detail and not cond else ""))
    if not cond:
        fails.append(label)


def read():
    with open(settings_path_for(proj), encoding="utf-8") as f:
        return json.load(f)


def commands(data, event):
    return [h.get("command", "") for g in data["hooks"].get(event, [])
            for h in g.get("hooks", [])]


print("\n1. wire into a project that already has its own hooks")
wire_hooks(proj, EXE)
d = read()
check("unrelated top-level keys preserved",
      d.get("permissions") == PRE_EXISTING["permissions"] and d.get("env") == PRE_EXISTING["env"])
check("unrelated event (Notification) untouched",
      commands(d, "Notification") == ["beep.exe"])
check("user's own PreToolUse hook still there",
      "my-audit-script.exe" in commands(d, "PreToolUse"))
check("user's own Stop hook still there", "notify-me.exe" in commands(d, "Stop"))
check("user's Bash matcher preserved",
      any(g.get("matcher") == "Bash" for g in d["hooks"]["PreToolUse"]))
check("all 5 of our events wired",
      all(any(EXE.lower() in c.lower() for c in commands(d, e))
          for e, _, _ in HOOK_EVENTS))
check("matcher present only where the schema wants it",
      all(any(("matcher" in g) == needs for g in d["hooks"][e]
              if any(EXE.lower() in h.get("command", "").lower()
                     for h in g.get("hooks", [])))
          for e, _, needs in HOOK_EVENTS))
check("backup written", os.path.exists(settings_path_for(proj) + ".bak"))

print("\n2. wire again (idempotent, no duplicates)")
wire_hooks(proj, EXE)
d = read()
dupes = {e: sum(1 for c in commands(d, e) if EXE.lower() in c.lower())
         for e, _, _ in HOOK_EVENTS}
check("exactly one entry per event", all(n == 1 for n in dupes.values()), str(dupes))
check("user's hooks survived re-wiring",
      "my-audit-script.exe" in commands(d, "PreToolUse")
      and "notify-me.exe" in commands(d, "Stop"))

print("\n3. re-wire from a stale path (moved install)")
with open(settings_path_for(proj), encoding="utf-8") as f:
    raw = f.read().replace(EXE.replace("\\", "\\\\"), OLD_EXE.replace("\\", "\\\\"))
with open(settings_path_for(proj), "w", encoding="utf-8") as f:
    f.write(raw)
wired, stale = hooks_status(proj, EXE)
check("stale path detected", not wired and stale, f"wired={wired} stale={stale}")
wire_hooks(proj, EXE)
d = read()
check("stale entries replaced, not duplicated",
      all(sum(1 for c in commands(d, e) if "claude-rpc.exe" in c.lower()) == 1
          for e, _, _ in HOOK_EVENTS))
check("no leftover old path", OLD_EXE not in json.dumps(d))

print("\n4. status reporting")
wired, stale = hooks_status(proj, EXE)
check("all 5 reported wired", len(wired) == 5, str(wired))
check("no stale reported", not stale)

print("\n5. remove")
wire_hooks(proj, EXE, remove=True)
d = read()
check("our hooks gone", "claude-rpc.exe" not in json.dumps(d).lower())
check("user's PreToolUse hook survived removal",
      commands(d, "PreToolUse") == ["my-audit-script.exe"])
check("user's Stop hook survived removal", commands(d, "Stop") == ["notify-me.exe"])
check("empty events dropped, not left as []",
      "SessionStart" not in d["hooks"] and "UserPromptSubmit" not in d["hooks"])
check("permissions/env still intact",
      d.get("permissions") == PRE_EXISTING["permissions"] and d.get("env") == PRE_EXISTING["env"])

print("\n6. brand-new project with no .claude folder")
fresh = os.path.join(tempfile.mkdtemp(prefix="wirefresh_"), "proj")
os.makedirs(fresh)
p = wire_hooks(fresh, EXE)
with open(p, encoding="utf-8") as f:
    nd = json.load(f)
check("settings.json created", os.path.exists(p))
check("5 events written", len(nd["hooks"]) == 5, str(list(nd["hooks"])))

print("\n" + ("ALL PASS" if not fails else f"{len(fails)} FAILURES: {fails}"))
sys.exit(1 if fails else 0)

