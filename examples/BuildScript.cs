// Place this file at Assets/Editor/BuildScript.cs in your Unity project.
//
// Unity Builder Dash invokes the static methods below via -executeMethod,
// chosen from the cross of three toggles:
//   - Auto-increment (top bar +/− toggle)
//   - Scripts Only   (top bar fast-forward toggle)
//   - AAB            (project context menu → "Build AAB (Google Play)")
//
// Method-name resolution lives in src/constants.py:resolve_build_method.
// AAB takes priority over Scripts Only — they're mutually exclusive.
//
// All ten entry points (per-target):
//
//   | Target  | Full, incr.        | Full, no-incr.            | Scripts Only, incr.            | Scripts Only, no-incr. | AAB, incr.              | AAB, no-incr. |
//   |---------|--------------------|---------------------------|--------------------------------|------------------------|-------------------------|---------------|
//   | Android | BuildAndroid       | BuildAndroidNoIncrement   | BuildAndroidScriptsOnlyIncr.   | BuildAndroidScriptsOnly| BuildAndroidAABIncrement| BuildAndroidAAB|
//   | iOS     | BuildiOS           | BuildiOSNoIncrement       | BuildiOSScriptsOnlyIncrement   | BuildiOSScriptsOnly    | (n/a)                   | (n/a)          |

using UnityEditor;
using UnityEditor.Build.Reporting;
using System.Linq;

public static class BuildScript {
    private static string[] GetScenes() =>
        EditorBuildSettings.scenes.Where(s => s.enabled).Select(s => s.path).ToArray();

    private static void RunBuild(BuildPlayerOptions options) {
        var report = BuildPipeline.BuildPlayer(options);
        if (report.summary.result == BuildResult.Succeeded)
            UnityEngine.Debug.Log("[Build] OK");
        else {
            UnityEngine.Debug.LogError("[Build] FAILED");
            EditorApplication.Exit(1);
        }
    }

    // ── Android APK ──
    [MenuItem("Build/Android APK")]
    public static void BuildAndroid() => DoBuildAndroid(true);
    public static void BuildAndroidNoIncrement() => DoBuildAndroid(false);

    [MenuItem("Build/Android APK (Scripts Only)")]
    public static void BuildAndroidScriptsOnly() => DoBuildAndroid(false, scriptsOnly: true);
    public static void BuildAndroidScriptsOnlyIncrement() => DoBuildAndroid(true, scriptsOnly: true);

    private static void DoBuildAndroid(bool increment, bool scriptsOnly = false) {
        if (increment) PlayerSettings.Android.bundleVersionCode++;
        EditorUserBuildSettings.exportAsGoogleAndroidProject = false;
        EditorUserBuildSettings.buildAppBundle = false;
        RunBuild(new BuildPlayerOptions {
            scenes = GetScenes(),
            locationPathName = "Builds/MyApp",
            target = BuildTarget.Android,
            options = scriptsOnly ? BuildOptions.BuildScriptsOnly : BuildOptions.None
        });
    }

    // ── Android AAB (Google Play) ──
    [MenuItem("Build/Android AAB")]
    public static void BuildAndroidAABIncrement() => DoBuildAndroidAAB(true);
    public static void BuildAndroidAAB() => DoBuildAndroidAAB(false);

    private static void DoBuildAndroidAAB(bool increment) {
        if (increment) PlayerSettings.Android.bundleVersionCode++;
        EditorUserBuildSettings.exportAsGoogleAndroidProject = false;
        EditorUserBuildSettings.buildAppBundle = true;
        RunBuild(new BuildPlayerOptions {
            scenes = GetScenes(),
            locationPathName = $"Builds/{PlayerSettings.productName}.aab",
            target = BuildTarget.Android,
            options = BuildOptions.None
        });
    }

    // ── iOS ──
    [MenuItem("Build/iOS Xcode")]
    public static void BuildiOS() => DoBuildiOS(true);
    public static void BuildiOSNoIncrement() => DoBuildiOS(false);

    [MenuItem("Build/iOS Xcode (Scripts Only)")]
    public static void BuildiOSScriptsOnly() => DoBuildiOS(false, scriptsOnly: true);
    public static void BuildiOSScriptsOnlyIncrement() => DoBuildiOS(true, scriptsOnly: true);

    private static void DoBuildiOS(bool increment, bool scriptsOnly = false) {
        if (increment) {
            int current = int.TryParse(PlayerSettings.iOS.buildNumber, out var n) ? n : 0;
            PlayerSettings.iOS.buildNumber = (current + 1).ToString();
        }
        RunBuild(new BuildPlayerOptions {
            scenes = GetScenes(),
            locationPathName = "Builds/iOS",
            target = BuildTarget.iOS,
            options = scriptsOnly ? BuildOptions.BuildScriptsOnly : BuildOptions.None
        });
    }
}
