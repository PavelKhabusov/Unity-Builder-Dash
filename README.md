# Unity Builder Dash

![Python](https://img.shields.io/badge/Python-3-3776AB?logo=python&logoColor=white)
![GTK4](https://img.shields.io/badge/GTK4-Libadwaita-4A86CF?logo=gnome&logoColor=white)
![Unity](https://img.shields.io/badge/Unity-6+-000000?logo=unity&logoColor=white)

A native GNOME (GTK4 + Libadwaita) desktop application for building and deploying Unity projects from the command line, without opening the Unity Editor.

## Features

- **Build** multiple Unity projects (Android APK, iOS Xcode) with one click
- **Build All** — sequential builds across all configured projects
- **Build History** — interactive chart (duration + APK size), filterable by project, with log viewer
- **Project Health Check** — version, Cloud ID, build scenes, compilation errors, git status
- **Deploy to device** via [APK Dash](https://github.com/PavelKhabusov/APK-Dash) integration
- **Upload to server** — FTP upload with per-project host, directory, and rename pattern
- **Build progress** — real-time log with colored output, progress bar, ETA based on previous builds
- **Log search & filter** — find text in build output, toggle word wrap
- **Per-project settings** — Unity version override, custom build directory, hide ADB for faster builds
- **Theme** — System / Dark / Light with live preview in settings
- **Auto-increment toggle** — build with or without version bump
- **Open in Unity** — launch editor from context menu
- **Desktop notifications** — get notified when build completes
- **GNOME native** — Adwaita widgets, dark theme, .desktop launcher

## Requirements

- Python 3.10+
- GTK4 and Libadwaita
- Unity Editor with Android/iOS build support
- [APK Dash](https://github.com/PavelKhabusov/APK-Dash) (optional, for device deployment)

### Arch Linux
```bash
sudo pacman -S python-gobject gtk4 libadwaita
```

### Ubuntu / Debian
```bash
sudo apt install python3-gi gir1.2-gtk-4.0 gir1.2-adw-1
```

## Setup

1. Clone the repository:
```bash
git clone https://github.com/PavelKhabusov/Unity-Builder-Dash.git
cd Unity-Builder-Dash
```

2. Copy and edit the config:
```bash
cp config.example.json config.json
```

3. Run:
```bash
./build.py
```

On first launch with no config, Settings opens automatically with auto-detection of Unity Editor path.

## Unity BuildScript

Place this in `Assets/Editor/BuildScript.cs` of each project:

```csharp
using UnityEditor;
using UnityEditor.Build;
using UnityEditor.Build.Reporting;
using System.Linq;

public static class BuildScript {

    private static string[] GetScenes() =>
        EditorBuildSettings.scenes
            .Where(s => s.enabled)
            .Select(s => s.path)
            .ToArray();

    [MenuItem("Build/Android APK")]
    public static void BuildAndroid() {
        PlayerSettings.Android.bundleVersionCode++;
        EditorUserBuildSettings.exportAsGoogleAndroidProject = false;

        var report = BuildPipeline.BuildPlayer(new BuildPlayerOptions {
            scenes = GetScenes(),
            locationPathName = "Builds/MyApp",
            target = BuildTarget.Android,
            options = BuildOptions.None
        });

        if (report.summary.result == BuildResult.Succeeded)
            UnityEngine.Debug.Log("[Build] OK");
        else {
            UnityEngine.Debug.LogError("[Build] FAILED");
            EditorApplication.Exit(1);
        }
    }

    // Without version increment (called when toggle is off)
    public static void BuildAndroidNoIncrement() {
        EditorUserBuildSettings.exportAsGoogleAndroidProject = false;
        var report = BuildPipeline.BuildPlayer(new BuildPlayerOptions {
            scenes = GetScenes(),
            locationPathName = "Builds/MyApp",
            target = BuildTarget.Android,
            options = BuildOptions.None
        });
        if (report.summary.result != BuildResult.Succeeded)
            EditorApplication.Exit(1);
    }
}
```

## Install as GNOME app

```bash
cp unity-builder-dash.desktop ~/.local/share/applications/
# Edit Exec= path in the .desktop file to match your location
```

## Config

`config.json` (gitignored, created from `config.example.json`):

| Field | Description |
|-------|-------------|
| `unity` | Path to Unity Editor binary (default) |
| `apk_dash` | Path to APK Dash script (optional) |
| `theme` | `"system"`, `"dark"`, or `"light"` |
| `projects[].name` | Display name |
| `projects[].path` | Unity project root |
| `projects[].desc` | Short description |
| `projects[].build_dir` | Output folder for builds |
| `projects[].targets` | Array: `"android"`, `"ios"` |
| `projects[].unity` | Per-project Unity Editor override (optional) |
| `projects[].hide_adb` | Hide ADB during build to skip device scan (~2 min faster) |
| `projects[].upload.host` | FTP host for upload |
| `projects[].upload.user` | FTP username |
| `projects[].upload.password` | FTP password (optional) |
| `projects[].upload.remote_dir` | Remote directory |
| `projects[].upload.rename_pattern` | Rename pattern, e.g. `{name}_mq3_{build}.apk` |

## Project structure

```
unity-builder-dash/
  build.py                    — Entry point, theme, adb safety
  config.json                 — User config (gitignored)
  config.example.json         — Config template
  build_history.json          — ETA history (gitignored)
  builds_log.json             — Full build log entries (gitignored)
  unity-builder-dash.desktop  — GNOME desktop entry
  icons/
    ubd-android-symbolic.svg  — Android build icon
    ubd-apple-symbolic.svg    — iOS build icon
  logs/                       — Unity build logs (gitignored)
  src/
    __init__.py               — GTK/Adw version requirements
    constants.py              — App metadata, target info, log patterns
    config.py                 — Config/history I/O, project scanner, upload, auto-detect
    worker.py                 — BuildWorker — runs Unity in a background thread
    settings_dialog.py        — Settings UI — projects, paths, upload, theme
    dialogs.py                — History dialog with chart, health check dialog
    window.py                 — Main window — project rows, log, progress, actions
```

## License

MIT
