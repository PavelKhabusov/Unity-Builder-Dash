# iOS Remote Build Server (Mac side)

Files that live **on the Mac** to make the iOS popup in Unity Builder Dash work.
The host (Linux/Windows/another Mac) pushes `iOS.zip` to the Mac via SCP, then
invokes `ios_build.scpt` over SSH. The script unzips, runs `pod install`,
integrates the widget, builds via `xcodebuild`, and installs on the iOS device.

## Contents

| File | Purpose |
|---|---|
| `ios_build.applescript` | **Source** with `{{PLACEHOLDERS}}`. Compiled to `ios_build.scpt` on the Mac at install time. |
| `add_widget_dependency.rb` | Adds the WidgetKit extension target to the Unity-exported Xcode project. Fully env-driven (BUNDLE_ID / TEAM_ID / WIDGET_TARGET_NAME come from your `config.json`). |
| `patch_scpt.sh` | Substitutes placeholders in `ios_build.applescript` with env-var values and runs `osacompile` to produce `ios_build.scpt`. |

`ios_build.scpt` is **not** checked in — it's generated from source on your Mac.

## Quick setup

The happy path uses the app's UI — no terminal surgery required.

### 1. On the Mac: enable Remote Login + install Xcode tools

System Settings → General → Sharing → **Remote Login: ON**.

```bash
xcode-select --install                     # command-line tools
sudo gem install xcodeproj --no-document   # needed by add_widget_dependency.rb
sudo gem install cocoapods --no-document   # needed by pod install / updatePod
```

Open Xcode once from `/Applications` to accept the license.

### 2. On the host: configure iOS Settings

Launch Unity Builder Dash → **Settings → iOS** tab. Fill in:

- **Mac IP**, **Mac user**, **Mac password**, **Mac work folder** (e.g. `/Users/you/Builds/iOS`)
- **Widget bundle ID** / **Apple Team ID** / **Widget target name** / **Widget folder name** / **App Group ID**
- **iOS Devices** list — add entries with `name` (exact `xcodebuild -destination 'platform=iOS,name=<this>'`) and a friendly `display_name` for the popup dropdown.

Everything is saved to `config.json` (gitignored, safe to share minus `mac_password`).

### 3. Click **Set up** then **Install on Mac**

Still in Settings → iOS (or via the 🔑 popover in the iOS action popup):

1. **Set up** — generates `~/.ssh/id_ed25519` if missing and runs `ssh-copy-id` to the Mac (needs `sshpass` locally and your Mac password filled in). After this, passwordless SSH works.
2. **Install on Mac** — SCPs `ios_build.applescript`, `add_widget_dependency.rb`, `patch_scpt.sh` into your work folder on the Mac, then runs `patch_scpt.sh` remotely to compile `ios_build.scpt` with your values substituted.

Re-run **Install on Mac** whenever you change widget/work-folder/devices — the `.scpt` gets regenerated.

### 4. Grant macOS automation permissions

First time a remote action fires, the Mac prompts to allow `osascript` to control Terminal / Xcode / Finder / System Events. Approve each.

Pre-grant in: **System Settings → Privacy & Security → Automation** (and **Accessibility**).

### 5. Test

Click the iOS button on a project. In the popup: the status dot next to Mac IP
should turn **green** on auto-probe. Clicking **Connect** additionally shows a
macOS notification banner on the Mac. Then try **Full** with a device plugged
in — xcodebuild output streams into the app's log view in real time.

## Installing `sshpass` (one-time, for Set up & password auth)

```bash
# Arch
sudo pacman -S sshpass
# Debian/Ubuntu
sudo apt install sshpass
# macOS
brew install sshpass
# Windows — from WSL, or Chocolatey/Scoop
```

Only needed for: initial `ssh-copy-id` (Set up button), and running with
`mac_auth: "password"` (i.e. not using an SSH key).

## Commands the Mac understands

Sent as first argument to `osascript ~/<work_dir>/ios_build.scpt <command>`.

| Command | Meaning |
|---|---|
| `run:<device>` | `stopTerminal` + `xcodebuild test -destination 'platform=iOS,name=<device>'` |
| `runFull:<device>` | `unpack` + `run:<device>` |
| `unpack` | unzip `iOS.zip`, `pod install`, add widget |
| `stop` | kill active Terminal job |
| `clearCache` | remove Xcode `DerivedData` / `.pcm` |
| `clearBuild` | `xcodebuild clean` + `rm -rf ./build/Build` |
| `updatePod` | full pod reinstall + add widget |
| `addWidget` | re-run `add_widget_dependency.rb` |
| `connectMac-<winIp>-<macIp>` | **Windows-only legacy**: SMB-mount the Windows share + save `winIp` for progress callbacks |

## How the popup buttons map

| Popup button | Host does | osascript arg |
|---|---|---|
| Build → **Full** | Unity iOS build → zip → scp | `runFull:<device>` |
| Build → **Xcode** | *(skip all — reuse existing zip on Mac)* | `runFull:<device>` |
| Build → **No Xcode** | Unity build → zip → scp | `unpack` |
| Archive → **Pack** | zip locally only | *(none — no SSH)* |
| Archive → **Unpack** | *(none)* | `unpack` |
| Archive → **All** | zip → scp | `unpack` |
| Extras → **Clear .pcm cache** | *(none)* | `clearCache` |
| Extras → **Add widget** | *(none)* | `addWidget` |
| Extras → **Clean build** | *(none)* | `clearBuild` |
| Kebab → **Stop** | kill local SSH + runner | `stop` |
| Kebab → **Update Pod** | *(none)* | `updatePod` |

## Log streaming

Every shell command on the Mac is wrapped as
`<cmd> 2>&1 | tee >(nc $IPADDRESS 8080)` so output goes **both** to the Mac's
Terminal window (visible locally on the Mac) **and** over TCP:8080 to Unity
Builder Dash's `ProgressListener`, which renders it in the LogView. Xcode,
pod, gem, ruby, plist-editing — everything streams.

`$IPADDRESS` is read from `$WORK_DIR/ip_address.txt`, which the host rewrites
on every SSH call (using `$SSH_CLIENT` — no IP detection on the host needed).

## Windows host notes

Modern Windows 10+ ships with OpenSSH `scp`, so you can use the same SCP flow
as Linux — no SMB share / XAMPP required. If you still prefer the legacy SMB
flow (e.g. to avoid SCP large-zip overhead on slow networks), fill in the
`smb_user` / `smb_password` / `smb_build_path` fields in `config.json` and
send the `connectMac-<winIp>-<macIp>` command once per session. The Mac
`unpack` handler falls back to copying `iOS.zip` from the SMB mount if no
local zip is found in the work folder.

## Troubleshooting

**"Permission denied (publickey,password)" on SSH** — re-run **Set up** from Settings → iOS (requires the Mac password in that tab + `sshpass` locally), or run `ssh-copy-id pavel@<mac-ip>` manually.

**Mac status dot stays grey / red** — open the iOS popup, click **Connect** (notification + audible feedback on Mac confirms the round trip), check the inline log for the exact error.

**`osascript` hangs with no output** — macOS is waiting for an Automation permission prompt. Open Terminal on the Mac directly: `osascript ~/<work_dir>/ios_build.scpt 'run:iPhone 12 mini'`, approve the prompts, then retry from the host.

**"iOS.zip not found" on Mac** — the SCP step failed silently; check `mac_work_dir` in Settings → iOS matches an existing folder and that SSH has write permission there.

**Widget build fails** — verify `gem install xcodeproj` completed on the Mac and that your widget source folder sits at `$WORK_DIR/$WIDGET_FOLDER/Widgets/` (e.g. `/Users/pavel/Desktop/Kartoteka/kartoteka.widget/Widgets/`). Copy the folder there manually if moving from the old `~/Desktop/kartoteka.widget/` path.

**Progress bar doesn't fill during build** — the Mac side sends progress via `nc $IPADDRESS:8080`. Check: firewall on the host isn't blocking 8080, and `$WORK_DIR/ip_address.txt` on the Mac contains the host's IP (auto-written by every SSH invocation via `$SSH_CLIENT`).

**Changed `mac_work_dir` and things break** — click **Install on Mac** again in Settings to re-deploy + recompile `ios_build.scpt` with the new paths.
