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
import subprocess
import sys
import time

TEMP = os.environ.get("TEMP") or "."
STATE_FILE = os.path.join(TEMP, "claude_rpc")
MODEL_FILE = os.path.join(TEMP, "claude_rpc_model")
HEARTBEAT_FILE = os.path.join(TEMP, "claude_rpc_daemon.json")
LOG_FILE = os.path.join(TEMP, "claude_rpc_daemon.log")
CONFIG_FILE = os.path.join(os.environ.get("APPDATA") or TEMP, "claude-rpc.json")


def get_app_id() -> str:
    """Env var wins; otherwise the ID saved from the UI."""
    app_id = (os.environ.get("DISCORD_APP_ID") or "").strip()
    if app_id:
        return app_id
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            return str(json.load(f).get("app_id") or "").strip()
    except Exception:
        return ""


def save_app_id(app_id: str) -> None:
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump({"app_id": app_id.strip()}, f)

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


def ui_mode() -> int:
    import tkinter as tk

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
