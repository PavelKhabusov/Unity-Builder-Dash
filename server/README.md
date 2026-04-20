# iOS Remote Build Server (Mac side)

This folder contains the files that live **on the Mac** to make the iOS popup
in Unity Builder Dash work. The host (Linux, Windows, or another Mac) uploads
the built Xcode project as `IOS.zip` via SCP, then invokes `IOSbuild.scpt`
over SSH. The script unpacks the zip, opens Xcode, builds the archive, and
installs it on the attached iOS device.

## Contents

| File | Purpose |
|---|---|
| `IOSbuild.scpt` | Compiled AppleScript. The one-command orchestrator on the Mac. |
| `add_widget_dependency.rb` | Adds the WidgetKit extension target to the Unity-exported Xcode project. Invoked by `IOSbuild.scpt` when "Добавить виджет" is pressed. |
| `patch_scpt.sh` | Decompiles `IOSbuild.scpt`, wraps the legacy SMB-mount block in an existence check, recompiles. Run once on the Mac. |

## Install on the Mac (one-time)

### 1. Enable Remote Login (SSH)

System Settings → General → Sharing → **Remote Login: ON**. Allow your user.

### 2. Set up passwordless SSH from the host

On the **Linux/Windows host** (not the Mac):

```bash
ssh-keygen -t ed25519           # if you don't have a key yet
ssh-copy-id pavel@<mac-ip>      # paste Mac password once
ssh pavel@<mac-ip> echo ok      # should print 'ok' without asking for password
```

If you must keep password auth, set `mac_auth: "password"` in Unity Builder
Dash's `config.json` and install `sshpass` on the host:

```bash
# Linux (Arch)
sudo pacman -S sshpass
# Linux (Debian/Ubuntu)
sudo apt install sshpass
# Windows
# Use sshpass from WSL or install via Chocolatey/Scoop
```

### 3. Copy the server files to the Mac

From the repo root on the host:

```bash
scp server/IOSbuild.scpt            pavel@<mac-ip>:~/Desktop/
scp server/add_widget_dependency.rb pavel@<mac-ip>:~/Desktop/
scp server/patch_scpt.sh            pavel@<mac-ip>:~/Desktop/
```

### 4. Install Mac dependencies

On the Mac (Terminal):

```bash
# Xcode Command Line Tools
xcode-select --install

# Ruby gem for add_widget_dependency.rb (needed for the "Добавить виджет" button)
sudo gem install xcodeproj --no-document

# CocoaPods (needed for "Обновить Pod")
sudo gem install cocoapods --no-document
```

Xcode itself must be installed from the App Store and opened at least once to
accept the license.

### 5. Patch `IOSbuild.scpt` to skip SMB mount

On the Mac:

```bash
cd ~/Desktop
bash patch_scpt.sh
```

This makes the AppleScript use the SCP'd `/Users/pavel/Desktop/IOS.zip`
directly instead of trying to SMB-mount a share on the host. The Windows flow
still works — if `IOS.zip` is absent when the script runs, the SMB mount is
attempted as before.

A backup of the original is saved as `IOSbuild.scpt.bak`.

### 6. Grant automation permissions

`osascript` needs permission to drive Terminal, Xcode, Finder, and System
Events. The first time you trigger an action from Unity Builder Dash, macOS
will prompt — approve each one. You can also pre-grant them in:

System Settings → Privacy & Security → **Automation** and **Accessibility**.
Add `ssh`/`sshd-keygen-wrapper` to Automation if needed.

### 7. Test

From the host, open a project in Unity Builder Dash, click the iOS button,
enter the Mac's IP, click **Соединить** — should say "Connected". Then try
**Полностью** with a real device plugged in.

## How the actions map to the Mac

| Popup button | Host does | Mac side (`osascript` argument) |
|---|---|---|
| Сборка → Полностью | Unity build → zip → scp | `RuniPhone12miniFull` / `RuniPadProFull` / etc. |
| Сборка → Xcode | (skip all — reuses prior zip on Mac) | same as above |
| Сборка → Без Xcode | Unity build → zip → scp | `unpack` |
| Архив → Архивировать | zip locally | — |
| Архив → Разархивировать | — | `unpack` |
| Архив → Всё | zip → scp | `unpack` |
| Доп → Остановить | kill active SSH | `stop` |
| Доп → Очистить кэш .pcm Xcode | — | `clearCache` |
| Доп → Добавить виджет | — | `addWidget` |
| Доп → Очистить сборку | — | `clearBuild` |
| Menu → Обновить Pod | — | `updatePod` |

## Windows host notes

The original `CrazyMegaBuilder` Unity Editor window targets Windows and uses
an SMB share (often set up via XAMPP or Windows file sharing) so the Mac
could mount a Windows folder and read `IOS.zip` from there. With this repo,
the host instead **pushes `IOS.zip` via SCP**, which also works from Windows
10+ (SSH client is included). XAMPP is no longer required.

If you still prefer the legacy SMB flow (e.g. for an existing Windows
setup), don't run `patch_scpt.sh` — the unmodified `IOSbuild.scpt` keeps
mounting the share.

## Troubleshooting

**"Permission denied" on SSH** — re-run `ssh-copy-id`, verify Remote Login
is ON, and that the user matches `mac_user` in `config.json`.

**`osascript` hangs with no output** — macOS is probably waiting for an
Automation permission prompt. Open Terminal on the Mac, run `osascript
~/Desktop/IOSbuild.scpt RuniPhone12miniFull` directly, approve prompts, then
retry from the host.

**"IOS.zip not found" on Mac** — the scp step failed silently; check
`mac_zip_dest` in config and that the host can write to that path.

**Widget build fails** — make sure `gem install xcodeproj` completed and the
widget source folder is at `/Users/pavel/Desktop/kartoteka.widget/Widgets/`.
