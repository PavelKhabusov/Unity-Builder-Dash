# Unity Builder Dash

![Python](https://img.shields.io/badge/Python-3-3776AB?logo=python&logoColor=white)
![GTK4](https://img.shields.io/badge/GTK4-Libadwaita-4A86CF?logo=gnome&logoColor=white)
![Unity](https://img.shields.io/badge/Unity-6+-000000?logo=unity&logoColor=white)

A native GNOME (GTK4 + Libadwaita) desktop application for building and deploying Unity projects from the command line, without opening the Unity Editor.

## Features

- Build multiple Unity projects (Android APK, iOS Xcode) with one click
- **Build All** — sequential builds across all configured projects
- **Project Health Check** — scan projects without opening Unity: version, Cloud ID, build scenes, compilation errors, git status
- **Deploy to device** via [APK Dash](https://github.com/PavelKhabusov/APK-Dash) integration
- **Build progress** — real-time log with colored output (errors, warnings, stages), progress bar, ETA based on previous builds
- **Dynamic config** — add/remove projects, auto-detect Unity editor and APK Dash paths
- Automatic lockfile cleanup, build folder management
- Native GNOME look & feel with dark theme support

## Requirements

- Python 3.10+
- GTK4 and Libadwaita (`python3-gi`, `gir1.2-adw-1`)
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
# Edit config.json with your Unity path and projects
```

3. Run:
```bash
./build.py
```

On first launch with no config, the Settings dialog opens automatically with auto-detection of Unity Editor path.

## Unity BuildScript

The app calls `BuildScript.BuildAndroid` or `BuildScript.BuildiOS` via Unity's `-executeMethod`. Place this script in `Assets/Editor/BuildScript.cs` of each project:

```csharp
using UnityEditor;
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
            locationPathName = "Builds/MyApp.apk",
            target = BuildTarget.Android,
            options = BuildOptions.None
        });

        if (report.summary.result != BuildResult.Succeeded)
            EditorApplication.Exit(1);
    }

    [MenuItem("Build/iOS Xcode")]
    public static void BuildiOS() {
        var report = BuildPipeline.BuildPlayer(new BuildPlayerOptions {
            scenes = GetScenes(),
            locationPathName = "Builds/iOS",
            target = BuildTarget.iOS,
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
| `unity` | Path to Unity Editor binary |
| `apk_dash` | Path to APK Dash script (optional) |
| `projects[].name` | Display name |
| `projects[].path` | Unity project root |
| `projects[].desc` | Short description |
| `projects[].build_dir` | Output folder for builds |
| `projects[].targets` | Array: `"android"`, `"ios"` |

## Project structure

```
unity-builder-dash/
  build.py                  — Entry point
  config.json               — User config (gitignored)
  config.example.json       — Config template
  build_history.json        — Build duration history for ETA (gitignored)
  unity-builder-dash.desktop — GNOME desktop entry
  src/
    __init__.py             — GTK/Adw version requirements
    constants.py            — App metadata, target info, log patterns
    config.py               — Config/history I/O, project scanner, auto-detect
    worker.py               — BuildWorker — runs Unity in a background thread
    settings_dialog.py      — Settings UI — edit paths, manage projects
    window.py               — Main window — cards, log, progress, actions
```

## License

MIT
