"""App-wide constants and patterns."""
import os, re

APP_NAME = "Unity Builder Dash"
APP_ID = "com.PavelKhabusov.UnityBuilderDash"
APP_GITHUB = "https://github.com/PavelKhabusov/Unity-Builder-Dash"
APK_DASH_GITHUB = "https://github.com/PavelKhabusov/APK-Dash"
ICONS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "icons")

TARGET_INFO = {
    "android": {"label": "Android", "method": "BuildScript.BuildAndroid", "icon": "ubd-android-symbolic"},
    "ios":     {"label": "iOS",     "method": "BuildScript.BuildiOS",     "icon": "ubd-apple-symbolic"},
}

SKIP_PATTERNS = [
    "Refreshing native plugins", "Native extension for", "Preloading",
    "[Physics]", "Batchmode", "[UnityConnectServicesConfig]", "[Licensing::",
    "[SignatureVerifier]", "servicesConfig", "disableServices", "disableUserLogin",
    "entitlementCache", "clientConnect", "clientHandshake", "clientResolve",
    "clientUpdate", "licensingService", "enableProxy", "Register platform support",
    "Registered in", "[usbmuxd]", "Android Extension - Scanning",
    "AcceleratorClient", "Using cacheserver", "ImportWorker",
    "monoOptions", "CodeReloadManager",
]

STAGE_PATTERNS = [
    ("Begin MonoManager ReloadAssembly",  "(1/10) Loading assemblies..."),
    ("- Completed reload",                "(1/10) Assemblies loaded"),
    ("Asset Pipeline Refresh",            "(2/10) Importing assets..."),
    ("[Stage]", None),
    ("ScriptCompilation",                 "(3/10) Compiling player scripts..."),
    ("BuildPlayerDataGenerator",          "(4/10) Generating player data..."),
    ("Addressable",                       "(4/10) Building addressables..."),
    ("OVRGradleGeneration",               "(5/10) Meta SDK preprocessing..."),
    ("Compiling shader",                  "(6/10) Compiling shaders..."),
    ("Building scenes",                   "(7/10) Building scenes..."),
    ("Writing asset",                     "(7/10) Writing assets..."),
    ("il2cpp",                            "(8/10) IL2CPP compilation..."),
    ("IL2CPP",                            "(8/10) IL2CPP compilation..."),
    ("Building Gradle project",           "(9/10) Gradle build..."),
    ("Packing APK",                       "(9/10) Packing APK..."),
    ("Exporting project",                 "(9/10) Exporting Xcode..."),
    ("Moving output",                     "(10/10) Finalizing..."),
    ("[Build] OK",                        "(10/10) Build complete!"),
    ("[Build] FAILED",                    "Build failed!"),
]

PROGRESS_RE = re.compile(r'(\d+)\s*/\s*(\d+)')
