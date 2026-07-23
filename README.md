# clauderpc — Discord Rich Presence for Claude Code

While you work in Claude Code, your Discord profile shows a live activity banner (the slot where games appear) with three lines:

1. **App name** — whatever you name the Discord application (e.g. "Claude Code")
2. **Details** — the model name (e.g. "Fable 5")
3. **State + timer** — what Claude is doing ("Reasoning" / "Running tool" / "Idle") and elapsed time since the last state change

**Download:** grab claude-rpc-win64.zip from [Releases](https://github.com/cabibbz/clauderpc/releases) — no Python needed. Unzip, keep the EXE next to its `_internal` folder, double-click.

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
py -m PyInstaller --onedir --noconsole --icon claude-rpc.ico --add-data "claude-rpc.ico;." --name claude-rpc --noconfirm claude_rpc.py
```

`--icon` sets the EXE's file icon; `--add-data` ships the same `.ico` inside the bundle so the app window and taskbar entry use it too. To regenerate the icon from other artwork: `py make_icons.py <source-image> claude-rpc.ico preview.png` (add `--tile` to place monochrome art on a white rounded tile so it stays visible on dark taskbars).

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

**The easy way:** in the app, click **Setup hooks…**, pick the folder you run Claude Code in, and click **Wire hooks here** (or **Wire globally** for every project). It writes the absolute path to *this* EXE into all five events.

It merges rather than overwrites: anything already in your `settings.json` — other hooks, `permissions`, `env`, matchers on the same events — is preserved, only this app's entries are added or updated, and a `.bak` copy is saved first. Re-running is safe and idempotent, and it repoints entries left behind by an older install. **Remove hooks** cleanly reverses it, leaving your own hooks untouched.

Then restart your Claude Code session in that folder — hooks load at session start, and it will ask you to approve them.

**The manual way:** copy [`settings.example.json`](settings.example.json) into your project's `.claude/settings.json`, or merge it into `%USERPROFILE%\.claude\settings.json` for all projects, replacing the EXE path with your **absolute path** in all five places. Mapping:

| Event | Status pushed |
|---|---|
| `SessionStart` | `idle` (also captures the model name) |
| `UserPromptSubmit` | `thinking` |
| `PreToolUse` | `tool` |
| `PostToolUse` | `thinking` |
| `Stop` | `idle` |

Schema verified against Claude Code 2.1.218: `UserPromptSubmit` and `Stop` take no matcher; the others use `"matcher": "*"`. Restart your Claude Code session after wiring — it loads hooks at session start and will ask you to approve them.

## 5. Run at login

**The easy way:** tick **Start the daemon automatically when I log in** in the Setup hooks dialog. It manages a Startup-folder shortcut for you, needs no admin rights, and unticking removes it.

Manual equivalent — a shortcut in the Startup folder (`shell:startup`):

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

**Click "Check my setup"** — it runs through every failure mode this thing has and tells you which one you're in, with the fix:

- Application ID present, and where it came from (env var vs saved)
- The Discord app exists (a wrong ID gives a clear "Discord doesn't recognise this" rather than silence), what its **name** is — that name is the banner's title line — and whether the `claude` art asset is actually uploaded
- All five hooks wired in your project, pointing at *this* EXE (it catches entries left at an old path after you move the folder)
- Daemon running, connected to Discord
- Whether a hook has fired recently — this is what tells you the wiring genuinely works, as opposed to looking right in the file
- Whether codex-rpc is also connected, since one Discord client only shows one activity

The checks are read-only and safe to run any time. Only the Discord-app checks need internet; presence itself is entirely local.

Manual checks, if you prefer:

- **The status UI** — double-click the EXE: Daemon and Discord rows should both be green, and Activity should update as hooks fire.
- **Daemon log** — `%TEMP%\claude_rpc_daemon.log` shows `connected to Discord RPC` and `presence -> <model> / <state>` lines as hooks fire. Repeated `Discord not reachable` lines mean Discord isn't running or the pipe is blocked.
- **Discord client** — your own profile (click your avatar) should show the activity card within ~15 s of a state change.
- **No image on the card?** Asset name must be exactly `claude`, and freshly uploaded assets take a few minutes to go live.
- **No card at all?** Discord Settings → **Activity Privacy** → enable activity display/sharing. Also confirm `DISCORD_APP_ID` is set in the environment the daemon started from.
- **State file** — `%TEMP%\claude_rpc` should contain e.g. `thinking|Fable 5` after you submit a prompt; if it does but the banner doesn't change, the problem is on the daemon/Discord side.

## Showing this and codexrpc at once

One Discord client displays only **one** local RPC activity. Run a second Discord client (PTB/Canary) logged into the same account and each client carries one banner — your profile shows both cards. See the [codexrpc README](https://github.com/cabibbz/codexrpc) for pipe selection details.
