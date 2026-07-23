"""Discord Rich Presence for Claude Code.

One binary, three modes:
  claude-rpc.exe                        - status UI window (default when double-clicked)
  claude-rpc.exe daemon                 - long-running process that owns the Discord RPC connection
  claude-rpc.exe thinking|tool|idle     - invoked by Claude Code hooks; writes state file and exits

The hook processes are short-lived (Claude Code spawns them per event), so they
can't hold Discord's IPC socket. They drop "<status>|<model>" into
%TEMP%\claude_rpc and the daemon polls it. The daemon also writes a heartbeat
file the UI polls, and appends to a log file (the exe is built windowed, so
there is no console to print to).
"""

import ctypes
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request

TEMP = os.environ.get("TEMP") or "."
STATE_FILE = os.path.join(TEMP, "claude_rpc")
MODEL_FILE = os.path.join(TEMP, "claude_rpc_model")
HEARTBEAT_FILE = os.path.join(TEMP, "claude_rpc_daemon.json")
LOG_FILE = os.path.join(TEMP, "claude_rpc_daemon.log")
CONFIG_FILE = os.path.join(os.environ.get("APPDATA") or TEMP, "claude-rpc.json")


def load_config() -> dict:
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_config(**fields) -> None:
    data = load_config()
    data.update(fields)
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


def get_app_id() -> str:
    """Env var wins; otherwise the ID saved from the UI."""
    env = (os.environ.get("DISCORD_APP_ID") or "").strip()
    return env or str(load_config().get("app_id") or "").strip()


def save_app_id(app_id: str) -> None:
    save_config(app_id=app_id.strip())

VALID_STATUSES = ("thinking", "tool", "idle")

STATE_LABELS = {
    "thinking": "Reasoning",
    "tool": "Running tool",
    "idle": "Idle",
}

PUSH_SECONDS = 15  # ~Discord's presence update throttle
TICK_SECONDS = 5   # heartbeat cadence so the UI notices a dead daemon quickly


# ---------------------------------------------------------------- shared bits

def log(msg: str) -> None:
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(f"[{time.strftime('%H:%M:%S')}] {msg}\n")
    except OSError:
        pass


def write_heartbeat(**fields) -> None:
    fields["pid"] = os.getpid()
    fields["ts"] = int(time.time())
    try:
        with open(HEARTBEAT_FILE, "w", encoding="utf-8") as f:
            json.dump(fields, f)
    except OSError:
        pass


def read_heartbeat() -> dict:
    try:
        with open(HEARTBEAT_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def pid_alive(pid: int) -> bool:
    if not pid:
        return False
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    STILL_ACTIVE = 259
    k32 = ctypes.windll.kernel32
    handle = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
    if not handle:
        return False
    code = ctypes.c_ulong()
    ok = k32.GetExitCodeProcess(handle, ctypes.byref(code))
    k32.CloseHandle(handle)
    return bool(ok) and code.value == STILL_ACTIVE


def daemon_pid() -> int:
    """PID of a live daemon, or 0."""
    hb = read_heartbeat()
    pid = hb.get("pid", 0)
    return pid if pid != os.getpid() and pid_alive(pid) else 0


# ------------------------------------------------------------------ hook mode

def prettify_model(raw: str) -> str:
    """'claude-fable-5' -> 'Fable 5', 'claude-opus-4-8' -> 'Opus 4.8'."""
    name = raw.strip()
    if not name:
        return "Claude"
    if not name.lower().startswith("claude-"):
        return name  # already a display name
    parts = name[len("claude-"):].split("-")
    # drop trailing date stamps like 20251001
    parts = [p for p in parts if not (p.isdigit() and len(p) >= 6)]
    words, version = [], []
    for p in parts:
        if p.isdigit():
            version.append(p)
        else:
            words.append(p.capitalize())
    if version:
        words.append(".".join(version))
    return " ".join(words) or "Claude"


def hook_mode(status: str) -> None:
    """Fast path: read hook JSON from stdin, write state file, exit silently.

    In Claude Code 2.1.x only SessionStart hooks receive a model field, so
    whenever one shows up we cache it to MODEL_FILE and read the cache on
    every other event.
    """
    model = ""
    try:
        stdin = sys.stdin.buffer if sys.stdin is not None else os.fdopen(0, "rb")
        data = json.loads(stdin.read().decode("utf-8-sig"))
        m = data.get("model")
        if isinstance(m, dict):
            model = m.get("display_name") or m.get("id") or ""
        elif isinstance(m, str):
            model = m.strip()
    except Exception:
        pass  # malformed/absent stdin must never break the hook

    if model:
        model = prettify_model(model)
        try:
            with open(MODEL_FILE, "w", encoding="utf-8") as f:
                f.write(model)
        except OSError:
            pass
    else:
        try:
            with open(MODEL_FILE, "r", encoding="utf-8") as f:
                model = f.read().strip()
        except OSError:
            pass

    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            f.write(f"{status}|{model or 'Claude'}")
    except OSError:
        pass


# ---------------------------------------------------------------- daemon mode

def read_state() -> str:
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return ""


def connect(app_id: str):
    """Block until Discord is up and accepting RPC connections."""
    from pypresence import Presence

    while True:
        rpc = Presence(app_id)
        try:
            rpc.connect()
            log("connected to Discord RPC")
            return rpc
        except Exception as exc:
            log(f"Discord not reachable ({exc.__class__.__name__}); retrying in {PUSH_SECONDS}s")
            write_heartbeat(discord="connecting")
            time.sleep(PUSH_SECONDS)


def daemon_mode() -> int:
    app_id = get_app_id()
    if not app_id:
        log("no Application ID yet; waiting (enter it in the UI and Save)")
        while not app_id:
            write_heartbeat(discord="no_app_id")
            time.sleep(TICK_SECONDS)
            app_id = get_app_id()
        log("Application ID received")

    if daemon_pid():
        log("daemon already running; exiting")
        return 0

    try:
        os.remove(LOG_FILE)
    except OSError:
        pass
    log(f"daemon starting (pid {os.getpid()})")
    write_heartbeat(discord="connecting")

    rpc = connect(app_id)
    write_heartbeat(discord="connected")
    last_state = None
    model, label, since = "", "", 0
    ticks_until_poll = 0
    try:
        while True:
            if ticks_until_poll <= 0:
                ticks_until_poll = PUSH_SECONDS // TICK_SECONDS
                state = read_state()
                if state and state != last_state:
                    status, _, model = state.partition("|")
                    label = STATE_LABELS.get(status, status.capitalize() or "Idle")
                    since = int(time.time())
                    try:
                        rpc.update(
                            details=model or "Claude",
                            state=label,
                            large_image="claude",
                            start=since,
                        )
                        last_state = state
                        log(f"presence -> {model or 'Claude'} / {label}")
                    except Exception as exc:
                        # Discord closed or restarted; reconnect and retry next poll
                        log(f"update failed ({exc.__class__.__name__}); reconnecting")
                        try:
                            rpc.close()
                        except Exception:
                            pass
                        rpc = connect(app_id)
                        last_state = None  # force re-push after reconnect
            write_heartbeat(discord="connected", model=model, activity=label, since=since)
            time.sleep(TICK_SECONDS)
            ticks_until_poll -= 1
    except KeyboardInterrupt:
        log("shutting down")
        try:
            rpc.clear()
            rpc.close()
        except Exception:
            pass
        try:
            os.remove(HEARTBEAT_FILE)
        except OSError:
            pass
        return 0


# ------------------------------------------------------------ setup & doctor

ASSET_NAME = "claude"
API = "https://discord.com/api/v9/oauth2/applications"

# (event, status argument, whether the event takes a matcher) - schema as of
# Claude Code 2.1.x: UserPromptSubmit and Stop take no matcher.
HOOK_EVENTS = [
    ("SessionStart", "idle", True),      # also captures the model name
    ("UserPromptSubmit", "thinking", False),
    ("PreToolUse", "tool", True),
    ("PostToolUse", "thinking", True),
    ("Stop", "idle", False),
]

OK, WARN, BAD = "ok", "warn", "bad"


def settings_path_for(project_dir: str) -> str:
    return os.path.join(project_dir, ".claude", "settings.json")


def load_settings(path: str) -> dict:
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}


def _is_ours(hook, exe_name: str) -> bool:
    cmd = (hook or {}).get("command")
    return isinstance(cmd, str) and exe_name.lower() in cmd.lower()


def _strip_ours(groups, exe_name: str) -> list:
    """Remove our hook entries; everything else is left exactly as found."""
    kept = []
    for g in groups if isinstance(groups, list) else []:
        if not isinstance(g, dict):
            kept.append(g)
            continue
        inner = [h for h in (g.get("hooks") or []) if not _is_ours(h, exe_name)]
        if inner:
            ng = dict(g)
            ng["hooks"] = inner
            kept.append(ng)
    return kept


def wire_hooks(project_dir: str, exe: str, remove: bool = False) -> str:
    """Merge our five hooks into settings.json, preserving any other content.
    Re-running is idempotent: our old entries (even at stale paths) are replaced.
    """
    path = settings_path_for(project_dir)
    data = load_settings(path)
    if not isinstance(data, dict):
        raise ValueError("settings.json does not contain a JSON object")
    hooks = data.get("hooks")
    if not isinstance(hooks, dict):
        hooks = {}
    exe_name = os.path.basename(exe)

    for event, status, needs_matcher in HOOK_EVENTS:
        kept = _strip_ours(hooks.get(event), exe_name)
        if not remove:
            entry = {"matcher": "*"} if needs_matcher else {}
            entry["hooks"] = [{"type": "command",
                               "command": f'"{exe}" {status}',
                               "timeout": 10}]
            kept.append(entry)
        if kept:
            hooks[event] = kept
        else:
            hooks.pop(event, None)

    if hooks:
        data["hooks"] = hooks
    else:
        data.pop("hooks", None)

    os.makedirs(os.path.dirname(path), exist_ok=True)
    if os.path.exists(path):
        shutil.copy2(path, path + ".bak")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
        f.write("\n")
    return path


def hooks_status(project_dir: str, exe: str):
    """(events wired to this exe, other paths our hooks point at)."""
    data = load_settings(settings_path_for(project_dir))
    hooks = data.get("hooks") if isinstance(data, dict) else {}
    exe_name = os.path.basename(exe)
    wired, stale = [], set()
    for event, _status, _m in HOOK_EVENTS:
        for g in (hooks or {}).get(event) or []:
            for h in (g or {}).get("hooks") or []:
                if _is_ours(h, exe_name):
                    cmd = h.get("command", "")
                    if exe.lower() in cmd.lower():
                        wired.append(event)
                    else:
                        stale.add(cmd)
    return sorted(set(wired)), sorted(stale)


def discord_get(url: str):
    req = urllib.request.Request(url, headers={"User-Agent": "claude-rpc"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read().decode())


def check_discord_app(out: list, app_id: str) -> None:
    try:
        info = discord_get(f"{API}/{app_id}/rpc")
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            out.append((BAD, "Discord doesn't recognise this Application ID",
                        "Copy the ID from General Information at "
                        "discord.com/developers/applications."))
        else:
            out.append((WARN, f"Discord API returned HTTP {exc.code}",
                        "Not fatal - try again in a moment."))
        return
    except Exception as exc:
        out.append((WARN, f"Couldn't reach Discord's API ({exc.__class__.__name__})",
                    "Only this check needs internet; presence itself is local."))
        return

    name = info.get("name") or "(unnamed)"
    out.append((OK, f'Discord app found: "{name}"',
                "This name is the banner's title line."))
    try:
        names = [a.get("name") for a in discord_get(f"{API}/{app_id}/assets")]
    except Exception as exc:
        out.append((WARN, f"Couldn't list art assets ({exc.__class__.__name__})", ""))
        return
    if ASSET_NAME in names:
        out.append((OK, f'Art asset "{ASSET_NAME}" uploaded', ""))
    elif info.get("icon"):
        out.append((WARN, f'No art asset named "{ASSET_NAME}"'
                          + (f" (found: {', '.join(n for n in names if n)})" if names else ""),
                    "The card falls back to your app icon, so it still looks fine. "
                    f'For the intended image, upload a 512x512 PNG named exactly '
                    f'"{ASSET_NAME}" under Rich Presence -> Art Assets.'))
    else:
        out.append((BAD, f'No art asset "{ASSET_NAME}" and no app icon',
                    "The card will show a blank image. Upload a 512x512 PNG named "
                    f'exactly "{ASSET_NAME}" under Rich Presence -> Art Assets.'))


def run_checks(exe: str, project_dir: str) -> list:
    """[(level, headline, fix), ...] - safe to call off the UI thread."""
    out = []

    app_id = get_app_id()
    if app_id.isdigit():
        src = "environment variable" if os.environ.get("DISCORD_APP_ID", "").strip() \
            else "saved settings"
        out.append((OK, f"Application ID set ({app_id}, from {src})", ""))
        check_discord_app(out, app_id)
    else:
        out.append((BAD, "No Discord Application ID",
                    "Paste it into the Application ID box and click Save."))

    if project_dir:
        wired, stale = hooks_status(project_dir, exe)
        missing = [e for e, _s, _m in HOOK_EVENTS if e not in wired]
        if not missing:
            out.append((OK, f"All 5 hooks wired in {settings_path_for(project_dir)}", ""))
        elif wired:
            out.append((WARN, f"Only {len(wired)} of 5 hooks wired "
                              f"(missing: {', '.join(missing)})",
                        "Open Setup hooks and wire this folder again."))
        else:
            out.append((BAD, f"No hooks for this EXE in {project_dir}",
                        "Open Setup hooks, pick this project folder, and click "
                        "Wire hooks here."))
        if stale:
            out.append((WARN, "Hooks here point at a different copy of the EXE",
                        "Wiring again repoints them at this one: " + stale[0]))
    else:
        out.append((WARN, "No project folder chosen to check",
                    "Open Setup hooks and pick the project you use Claude Code in."))

    pid = daemon_pid()
    hb = read_json_file(HEARTBEAT_FILE)
    if pid:
        out.append((OK, f"Daemon running (pid {pid})", ""))
        state = hb.get("discord")
        if state == "connected":
            out.append((OK, "Connected to Discord", ""))
        elif state == "no_app_id":
            out.append((BAD, "Daemon is waiting for an Application ID",
                        "Save the ID; the daemon picks it up within 5 seconds."))
        else:
            out.append((WARN, "Daemon can't reach Discord yet",
                        "Start the Discord desktop app - the browser version has no "
                        "RPC socket. The daemon retries every 15 seconds."))
    else:
        out.append((BAD, "Daemon not running", "Click Start daemon."))

    try:
        age = time.time() - os.path.getmtime(STATE_FILE)
        if age < 900:
            out.append((OK, f"A hook fired {int(age)}s ago - Claude Code is "
                            "driving the banner", ""))
        else:
            out.append((WARN, f"Last hook fired {int(age / 60)} min ago",
                        "Normal if you haven't used Claude Code since. If you have, "
                        "restart the session so it reloads hooks."))
    except OSError:
        out.append((WARN, "No hook has ever fired",
                    "Restart Claude Code after wiring hooks - it loads them at "
                    "session start and will ask you to approve them."))

    if read_json_file(os.path.join(TEMP, "codex_rpc_daemon.json")).get("discord") \
            == "connected":
        out.append((WARN, "codex-rpc is also connected to Discord",
                    "One Discord client shows one activity, so only one banner is "
                    "visible. Stop the other daemon, or run a second Discord client "
                    "(PTB/Canary) to show both."))
    return out


def read_json_file(path: str) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def set_run_at_login(exe: str, enable: bool) -> str:
    lnk = os.path.join(os.environ.get("APPDATA", TEMP), "Microsoft", "Windows",
                       "Start Menu", "Programs", "Startup", "Claude RPC.lnk")
    if not enable:
        try:
            os.remove(lnk)
        except OSError:
            pass
        return "Removed from login startup."
    ps = (f"$w = New-Object -ComObject WScript.Shell; "
          f"$s = $w.CreateShortcut('{lnk}'); $s.TargetPath = '{exe}'; "
          f"$s.Arguments = 'daemon'; "
          f"$s.Description = 'Discord Rich Presence for Claude Code'; $s.Save()")
    subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
                   capture_output=True, creationflags=0x08000000)
    return "Added to login startup." if os.path.exists(lnk) else \
        "Could not create the startup shortcut."


# -------------------------------------------------------------------- UI mode

BG = "#1e1f22"
CARD = "#2b2d31"
FG = "#dbdee1"
DIM = "#949ba4"
GREEN = "#23a55a"
RED = "#f23f43"
YELLOW = "#f0b232"


def exe_command(*args):
    if getattr(sys, "frozen", False):
        return [sys.executable, *args]
    return [sys.executable, os.path.abspath(__file__), *args]


def resource_path(name: str) -> str:
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, name)


def exe_path() -> str:
    return sys.executable if getattr(sys, "frozen", False) \
        else os.path.abspath(__file__)


def ui_mode() -> int:
    import threading
    import tkinter as tk
    from tkinter import filedialog

    root = tk.Tk()
    root.title("Claude RPC")
    root.configure(bg=BG, padx=16, pady=14)
    root.resizable(False, False)
    try:
        root.iconbitmap(resource_path("claude-rpc.ico"))
    except Exception:
        pass  # window icon is cosmetic

    header = tk.Label(root, text="Claude Code — Discord Rich Presence",
                      bg=BG, fg=FG, font=("Segoe UI Semibold", 11))
    header.grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 10))

    card = tk.Frame(root, bg=CARD, padx=14, pady=12)
    card.grid(row=1, column=0, columnspan=2, sticky="ew")

    def row(r, name):
        tk.Label(card, text=name, bg=CARD, fg=DIM, font=("Segoe UI", 9),
                 width=9, anchor="w").grid(row=r, column=0, sticky="w", pady=2)
        val = tk.Label(card, text="—", bg=CARD, fg=FG, font=("Segoe UI", 10), anchor="w")
        val.grid(row=r, column=1, sticky="w", pady=2)
        return val

    daemon_val = row(0, "Daemon")
    discord_val = row(1, "Discord")
    activity_val = row(2, "Activity")
    timer_val = row(3, "Elapsed")

    btns = tk.Frame(root, bg=BG)
    btns.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(12, 0))

    def styled_btn(text, cmd):
        return tk.Button(btns, text=text, command=cmd, bg=CARD, fg=FG,
                         activebackground="#404249", activeforeground=FG,
                         relief="flat", font=("Segoe UI", 9), padx=14, pady=4)

    def start_daemon():
        if daemon_pid():
            return
        flags = 0x00000008 | 0x08000000  # DETACHED_PROCESS | CREATE_NO_WINDOW
        subprocess.Popen(exe_command("daemon"), creationflags=flags,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, close_fds=True)

    def stop_daemon():
        pid = daemon_pid()
        if pid:
            try:
                subprocess.run(["taskkill", "/PID", str(pid), "/F"],
                               capture_output=True, creationflags=0x08000000)
            except Exception:
                pass
        try:
            os.remove(HEARTBEAT_FILE)
        except OSError:
            pass

    start_btn = styled_btn("Start daemon", start_daemon)
    start_btn.pack(side="left")
    stop_btn = styled_btn("Stop daemon", stop_daemon)
    stop_btn.pack(side="left", padx=(8, 0))

    def dialog(title, pady=14):
        win = tk.Toplevel(root)
        win.title(title)
        win.configure(bg=BG, padx=16, pady=pady)
        win.transient(root)
        try:
            win.iconbitmap(resource_path("claude-rpc.ico"))
        except Exception:
            pass
        return win

    def dlg_btn(parent, text, cmd):
        return tk.Button(parent, text=text, command=cmd, bg=CARD, fg=FG,
                         activebackground="#404249", activeforeground=FG,
                         relief="flat", font=("Segoe UI", 9), padx=12, pady=4)

    def open_setup():
        win = dialog("Setup hooks")
        tk.Label(win, text="Wire Claude Code's hooks to this EXE", bg=BG, fg=FG,
                 font=("Segoe UI Semibold", 10)).grid(row=0, column=0,
                                                      columnspan=3, sticky="w")
        tk.Label(win, text="Pick the folder you run Claude Code in. Anything already in "
                           "settings.json is\nkept — only this app's entries are added or "
                           "updated — and a .bak copy is saved.",
                 bg=BG, fg=DIM, font=("Segoe UI", 8), justify="left").grid(
            row=1, column=0, columnspan=3, sticky="w", pady=(2, 10))

        dir_var = tk.StringVar(value=load_config().get("project_dir", ""))
        tk.Entry(win, textvariable=dir_var, bg=CARD, fg=FG, insertbackground=FG,
                 relief="flat", width=58, font=("Consolas", 9)).grid(
            row=2, column=0, columnspan=2, sticky="w", ipady=3)

        def browse():
            d = filedialog.askdirectory(parent=win, title="Choose your project folder")
            if d:
                dir_var.set(os.path.normpath(d))

        dlg_btn(win, "Browse…", browse).grid(row=2, column=2, sticky="w", padx=(8, 0))

        result = tk.Label(win, text="", bg=BG, fg=YELLOW, font=("Segoe UI", 9),
                          wraplength=520, justify="left")
        result.grid(row=4, column=0, columnspan=3, sticky="w", pady=(12, 0))

        def apply(remove=False, glob=False):
            d = os.path.expanduser("~") if glob else dir_var.get().strip()
            if not d or not os.path.isdir(d):
                result.config(text="Choose a folder first.", fg=YELLOW)
                return
            try:
                path = wire_hooks(d, exe_path(), remove=remove)
            except Exception as exc:
                result.config(text=f"Failed: {exc}", fg=RED)
                return
            if not glob:
                save_config(project_dir=d)
            if remove:
                result.config(text=f"Removed this app's hooks from {path}", fg=GREEN)
            else:
                result.config(text=f"Wired 5 hooks into {path}\n\nRestart your Claude "
                                   "Code session in that folder — hooks load at session "
                                   "start, and it will ask you to approve them.", fg=GREEN)

        row = tk.Frame(win, bg=BG)
        row.grid(row=3, column=0, columnspan=3, sticky="w", pady=(10, 0))
        dlg_btn(row, "Wire hooks here", apply).pack(side="left")
        dlg_btn(row, "Wire globally (all projects)",
                lambda: apply(glob=True)).pack(side="left", padx=(8, 0))
        dlg_btn(row, "Remove hooks",
                lambda: apply(remove=True)).pack(side="left", padx=(8, 0))

        lnk = os.path.join(os.environ.get("APPDATA", TEMP), "Microsoft", "Windows",
                           "Start Menu", "Programs", "Startup", "Claude RPC.lnk")
        login_var = tk.BooleanVar(value=os.path.exists(lnk))

        def toggle_login():
            result.config(text=set_run_at_login(exe_path(), login_var.get()), fg=GREEN)

        tk.Checkbutton(win, text="Start the daemon automatically when I log in",
                       variable=login_var, command=toggle_login, bg=BG, fg=DIM,
                       selectcolor=CARD, activebackground=BG, activeforeground=FG,
                       font=("Segoe UI", 9)).grid(row=5, column=0, columnspan=3,
                                                  sticky="w", pady=(12, 0))

    def open_doctor():
        win = dialog("Check my setup")
        txt = tk.Text(win, width=88, height=22, bg=CARD, fg=FG, relief="flat",
                      font=("Segoe UI", 9), state="disabled", padx=12, pady=10,
                      wrap="word", spacing1=2, spacing3=2)
        txt.grid(row=0, column=0, sticky="ew")
        txt.tag_configure(OK, foreground=GREEN)
        txt.tag_configure(WARN, foreground=YELLOW)
        txt.tag_configure(BAD, foreground=RED)
        txt.tag_configure("fix", foreground=DIM, lmargin1=22, lmargin2=22)

        def render(results):
            marks = {OK: "OK   ", WARN: "!    ", BAD: "X    "}
            txt.config(state="normal")
            txt.delete("1.0", "end")
            for level, headline, fix in results:
                txt.insert("end", marks[level] + headline + "\n", level)
                if fix:
                    txt.insert("end", fix + "\n", "fix")
                txt.insert("end", "\n")
            bad = sum(1 for lvl, _, _ in results if lvl == BAD)
            warn = sum(1 for lvl, _, _ in results if lvl == WARN)
            if bad:
                txt.insert("end", f"{bad} thing(s) need fixing.", BAD)
            elif warn:
                txt.insert("end", "Working, with some notes above.", WARN)
            else:
                txt.insert("end", "Everything checks out.", OK)
            txt.config(state="disabled")

        def run():
            render([(WARN, "Checking…", "")])

            def work():
                results = run_checks(exe_path(), load_config().get("project_dir", ""))
                root.after(0, lambda: render(results))

            threading.Thread(target=work, daemon=True).start()

        dlg_btn(win, "Re-run", run).grid(row=1, column=0, sticky="w", pady=(10, 0))
        run()

    setup_btn = styled_btn("Setup hooks…", open_setup)
    setup_btn.pack(side="left", padx=(8, 0))
    styled_btn("Check my setup", open_doctor).pack(side="left", padx=(8, 0))

    idrow = tk.Frame(root, bg=BG)
    idrow.grid(row=3, column=0, columnspan=2, sticky="ew", pady=(12, 0))
    tk.Label(idrow, text="Application ID", bg=BG, fg=DIM,
             font=("Segoe UI", 9)).pack(side="left")
    id_var = tk.StringVar(value=get_app_id())
    tk.Entry(idrow, textvariable=id_var, bg=CARD, fg=FG, insertbackground=FG,
             relief="flat", width=24, font=("Consolas", 9)).pack(side="left",
                                                                 padx=(8, 8))
    id_msg = tk.Label(idrow, text="", bg=BG, fg=YELLOW, font=("Segoe UI", 8))

    def save_id():
        val = id_var.get().strip()
        if not val.isdigit():
            id_msg.config(text="App ID must be a number (from discord.com/developers)")
            return
        try:
            save_app_id(val)
        except OSError as exc:
            id_msg.config(text=f"Could not save: {exc}")
            return
        if os.environ.get("DISCORD_APP_ID", "").strip() not in ("", val):
            id_msg.config(text="Saved — but the DISCORD_APP_ID env var overrides it")
        else:
            id_msg.config(text="Saved")
        if daemon_pid():  # restart so the daemon reconnects with the new ID
            stop_daemon()
            start_daemon()

    tk.Button(idrow, text="Save", command=save_id, bg=CARD, fg=FG,
              activebackground="#404249", activeforeground=FG, relief="flat",
              font=("Segoe UI", 9), padx=12, pady=2).pack(side="left")
    id_msg.pack(side="left", padx=(8, 0))

    log_box = tk.Text(root, height=6, width=52, bg=CARD, fg=DIM, relief="flat",
                      font=("Consolas", 8), state="disabled", padx=8, pady=6)
    log_box.grid(row=4, column=0, columnspan=2, sticky="ew", pady=(12, 0))

    def fmt_elapsed(since):
        if not since:
            return "—"
        s = max(0, int(time.time()) - int(since))
        h, rem = divmod(s, 3600)
        m, s = divmod(rem, 60)
        return f"{h}:{m:02}:{s:02}" if h else f"{m}:{s:02}"

    def refresh():
        pid = daemon_pid()
        hb = read_heartbeat()
        if pid:
            daemon_val.config(text=f"● Running (pid {pid})", fg=GREEN)
            if hb.get("discord") == "connected":
                discord_val.config(text="Connected", fg=GREEN)
            elif hb.get("discord") == "no_app_id":
                discord_val.config(text="Waiting for Application ID — enter it below", fg=YELLOW)
            else:
                discord_val.config(text="Waiting for Discord…", fg=YELLOW)
            model, activity = hb.get("model"), hb.get("activity")
            if model or activity:
                activity_val.config(text=f"{model or 'Claude'} — {activity or '—'}", fg=FG)
                timer_val.config(text=fmt_elapsed(hb.get("since")), fg=FG)
            else:
                activity_val.config(text="Waiting for first hook event…", fg=DIM)
                timer_val.config(text="—", fg=DIM)
            start_btn.config(state="disabled")
            stop_btn.config(state="normal")
        else:
            daemon_val.config(text="○ Stopped", fg=RED)
            discord_val.config(text="—", fg=DIM)
            activity_val.config(text="—", fg=DIM)
            timer_val.config(text="—", fg=DIM)
            start_btn.config(state="normal")
            stop_btn.config(state="disabled")
            if not get_app_id():
                activity_val.config(text="Enter your Application ID below and Save", fg=YELLOW)

        try:
            with open(LOG_FILE, "r", encoding="utf-8") as f:
                tail = "".join(f.readlines()[-6:])
        except OSError:
            tail = ""
        log_box.config(state="normal")
        log_box.delete("1.0", "end")
        log_box.insert("1.0", tail or "(no daemon log yet)")
        log_box.config(state="disabled")

        root.after(1000, refresh)

    refresh()
    root.mainloop()
    return 0


# ------------------------------------------------------------------- dispatch

def main() -> int:
    if len(sys.argv) < 2:
        return ui_mode()

    mode = sys.argv[1].lower()
    if mode == "daemon":
        return daemon_mode()
    if mode == "ui":
        return ui_mode()
    if mode in VALID_STATUSES:
        hook_mode(mode)
        return 0

    log(f"error: unknown mode '{mode}'")
    return 2


if __name__ == "__main__":
    sys.exit(main())
