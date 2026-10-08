# Unity Builder Dash — control for agents

An agent can start Unity builds and watch them without the GUI: through the `ubd` CLI or the
local HTTP API on `127.0.0.1:47816` (same style as the home-kit-dash hub: `/health`,
self-description at `/commands.json`, JSON responses, localhost only, no auth).

## Main rule: one Unity build at a time

Parallel builds run the machine out of memory (OOM). `ubd build` refuses (exit code 3) when:
- the dashboard or the headless runner is already building;
- any `Unity -batchmode` process is running on the machine (including ones not started by the dashboard);
- the target project is open in the Unity Editor (it holds `Temp/UnityLockfile`).

Do not bypass this check and do not start Unity manually in parallel. Wait with `ubd status`.

## Who runs the build

- **GUI is running** → `ubd build` sends the command to the window (`POST /build`); the build shows
  up in the dashboard log and progress bar like a normal one.
- **GUI is not running** → `ubd` starts a headless runner in the background (same `BuildWorker` and
  iOS pipeline zip → scp → xcodebuild). While building, it serves the same API.
- **Old GUI without the API** → exit code 4: restart the dashboard.

## Commands

Run `~/DEV/unity-builder-dash/ubd <command>` (or `python3 -m src.cli` from the repo folder).
Every command accepts `--json` for machine-readable output.

| Command | What it does |
|---|---|
| `ubd projects` | projects from the config: name, targets, path |
| `ubd status` | whether a build is running: project, target, stage, progress, time, log path, busy reason |
| `ubd log [--tail N] [-f]` | tail of the current/last build log; `-f` follows until the end |
| `ubd history [N]` | last N builds (date, result, duration, build number) |
| `ubd devices` | adb devices (serial) and iOS devices from the config (xcodebuild name) |
| `ubd ios-status` | whether the Mac is reachable over SSH, and its battery |
| `ubd build <project> <android\|ios> [options]` | start a build |
| `ubd cancel` | cancel the current build (for iOS also sends `stop` to the Mac) |

`build` options:
- `--scripts-only` — C# scripts only (fast, for iterations);
- `--aab` — Android App Bundle for Google Play;
- `--no-increment` — do not bump the build number;
- `--install <device>` — android: adb serial, runs `adb install -r` after the build;
  ios: xcodebuild device name (defaults to the first one in the config);
- `--ios-action full|xcode|build_only|without_xcode` — what to do for iOS (default `full`:
  Unity → zip → scp → unpack → build and install on the Mac);
- `--wait` / `-w` — stream the log and exit with the build result code;
- `--dry-run` — only print what would run and through whom; starts nothing.

## Examples

```bash
cd ~/DEV/unity-builder-dash
./ubd build KartotekaAR ios --dry-run          # check the plan
./ubd build KartotekaAR ios --wait             # full iOS build of KartotekaAR
./ubd build KartotekaMR android --scripts-only --install 2G0YC1ZG4P026Z
./ubd status && ./ubd log --tail 50
```

## Exit codes

| Code | Meaning |
|---|---|
| 0 | ok (with `--wait` — the build succeeded) |
| 1 | build failed or was cancelled; `ios-status`: Mac unreachable |
| 2 | bad arguments, unknown project or target |
| 3 | busy: a Unity build is already running |
| 4 | GUI is running but has no agent API (old version) — restart it |
| 5 | API error |

## HTTP API (`127.0.0.1:47816`; port via `agent_api_port` in the config or `UBD_API_PORT`)

- `GET /health` → `{ok, app, owner: gui|headless, pid}`
- `GET /commands.json` → command manifest (for voice-dash and other agents)
- `GET /status`, `GET /projects`, `GET /log?tail=N`, `GET /history?n=N`
- `POST /build` `{project, target, scripts_only?, aab?, increment?, install?, ios_action?}` →
  200 `{ok, log_path, status}` · 400 bad request · 409 busy
- `POST /cancel`

The API is up only while the GUI or the headless runner is running; without them
`ubd status/log/history` read the files directly.

## Where things are

- `logs/agent_status.json` — state of the current/last build (what `status` returns);
- `logs/run_<project>_<target>_<time>.log` — live build log (last 15 kept);
- `logs/<project>_<time>.log` — full Unity log (as before);
- `logs/headless_<time>.out` — stdout of the headless runner;
- `builds_log.json` — build history (same as the History tab).

## Don't

- Print the contents of `config.json` to chat or logs: it holds passwords (Mac, FTP, Apple).
  Reading keys from code is fine; showing values is not.
- Restart or kill the dashboard or Unity while someone else is building.
- Start a second build bypassing `ubd`.
