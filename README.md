# clauderpc — Discord Rich Presence for Claude Code

While you work in Claude Code, your Discord profile shows a live activity banner (the slot where games appear) with three lines:

1. **App name** — whatever you name the Discord application (e.g. "Claude Code")
2. **Details** — the model name (e.g. "Fable 5")
3. **State + timer** — what Claude is doing ("Reasoning" / "Running tool" / "Idle") and elapsed time since the last state change

Sibling project: [codexrpc](https://github.com/cabibbz/codexrpc) does the same for OpenAI Codex (CLI + desktop app) via PID attachment.

## How it works

Claude Code hooks are short-lived processes that exit immediately, so a hook can't hold the persistent socket Discord RPC requires. One binary does both jobs:

- `claude-rpc.exe daemon` — runs continuously, owns the Discord RPC connection, polls the state file every 15 s (roughly Discord's update throttle) and pushes changes.
- `claude-rpc.exe thinking|tool|idle` — invoked by hooks; reads the hook JSON from stdin, writes `<status>|<model>` to `%TEMP%\claude_rpc`, exits in ~65 ms.
- `claude-rpc.exe` (no arguments / double-click) — status UI: daemon state, Discord connection, current activity with live timer, Start/Stop buttons, log tail.

**Model name detail:** in Claude Code 2.1.x, only the `SessionStart` hook receives the model on stdin (as an ID like `claude-fable-5`). The hook mode prettifies it ("Fable 5"), caches it in `%TEMP%\claude_rpc_model`, and every other event reads that cache. That's why the hook config includes a `SessionStart` entry.

## 1. Create the Discord application

1. Go to <https://discord.com/developers/applications> → **New Application**. The application **name** is what Discord shows as the activity title ("Playing …"), so name it something like `Claude Code`.
2. Open **Rich Presence → Art Assets** → **Add Image(s)** and upload a **512×512 PNG** with the asset name **exactly `claude`** (the daemon references `large_image="claude"`). Save changes. New assets can take several minutes to propagate.
3. Copy the **Application ID** from **General Information**.

## 2. Build the EXE

Requires Python 3.11+ on **Windows** (PyInstaller can't cross-compile — a Windows binary must be built on Windows):

```powershell
py -m pip install pypresence pyinstaller
py -m PyInstaller --onedir --noconsole --name claude-rpc --noconfirm claude_rpc.py
```

Output: `dist\claude-rpc\claude-rpc.exe` (plus its `_internal` folder — keep them together).

`--noconsole` means no console window ever flashes — not for hooks, not for the daemon. Because there's no console, the daemon logs to `%TEMP%\claude_rpc_daemon.log` instead of stdout.

> **Why `--onedir` instead of `--onefile`:** onefile unpacks itself to a temp directory on every launch. Measured: ~500 ms per hook invocation with onefile vs ~65 ms with onedir. Hooks fire on every prompt and every tool call, and `PreToolUse` runs synchronously before each tool, so that half-second is very noticeable.

## 3. Set the Application ID

**In the app:** double-click `claude-rpc.exe`, paste the Application ID into the field at the bottom, hit **Save**. It's stored in `%APPDATA%\claude-rpc.json`; a running daemon is restarted automatically, and a daemon started without an ID waits and picks it up the moment you save.

Optional override via environment variable (takes precedence over the saved ID):

```powershell
setx DISCORD_APP_ID <your-application-id>
```

## 4. Hook config

Copy [`settings.example.json`](settings.example.json) into your project's `.claude/settings.json` (project-local activation) or merge into `%USERPROFILE%\.claude\settings.json` (global), replacing the EXE path with your **absolute path**. Mapping:

| Event | Status pushed |
|---|---|
| `SessionStart` | `idle` (also captures the model name) |
| `UserPromptSubmit` | `thinking` |
| `PreToolUse` | `tool` |
| `PostToolUse` | `thinking` |
| `Stop` | `idle` |

Schema verified against Claude Code 2.1.218: `UserPromptSubmit` and `Stop` take no matcher; the others use `"matcher": "*"`. Restart your Claude Code session after wiring — it loads hooks at session start and will ask you to approve them.

## 5. Run at login

No-admin option — a shortcut in the Startup folder (`shell:startup`):

```powershell
$ws = New-Object -ComObject WScript.Shell
$lnk = $ws.CreateShortcut("$env:APPDATA\Microsoft\Windows\Start Menu\Programs\Startup\Claude RPC.lnk")
$lnk.TargetPath = "C:\path\to\dist\claude-rpc\claude-rpc.exe"
$lnk.Arguments = "daemon"
$lnk.Save()
```

Task Scheduler alternative (requires an elevated shell — `ONLOGON` triggers can't be created unelevated):

```powershell
schtasks /Create /TN "Claude RPC" /TR "C:\path\to\dist\claude-rpc\claude-rpc.exe daemon" /SC ONLOGON /RL LIMITED /F
```

Keep it interactive (run-only-when-logged-on) — Discord RPC uses a per-user named pipe, so the daemon must run in your logon session. Run the `setx` from step 3 first; processes started from a shell that predates the `setx` won't see the variable.

## 6. Verifying the connection

- **The status UI** — double-click the EXE: Daemon and Discord rows should both be green, and Activity should update as hooks fire.
- **Daemon log** — `%TEMP%\claude_rpc_daemon.log` shows `connected to Discord RPC` and `presence -> <model> / <state>` lines as hooks fire. Repeated `Discord not reachable` lines mean Discord isn't running or the pipe is blocked.
- **Discord client** — your own profile (click your avatar) should show the activity card within ~15 s of a state change.
- **No image on the card?** Asset name must be exactly `claude`, and freshly uploaded assets take a few minutes to go live.
- **No card at all?** Discord Settings → **Activity Privacy** → enable activity display/sharing. Also confirm `DISCORD_APP_ID` is set in the environment the daemon started from.
- **State file** — `%TEMP%\claude_rpc` should contain e.g. `thinking|Fable 5` after you submit a prompt; if it does but the banner doesn't change, the problem is on the daemon/Discord side.

## Showing this and codexrpc at once

One Discord client displays only **one** local RPC activity. Run a second Discord client (PTB/Canary) logged into the same account and each client carries one banner — your profile shows both cards. See the [codexrpc README](https://github.com/cabibbz/codexrpc) for pipe selection details.
