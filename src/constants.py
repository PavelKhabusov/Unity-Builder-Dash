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
    ("Begin MonoManager ReloadAssembly", "(1/7) Compiling scripts..."),
    ("- Completed reload",              "(1/7) Scripts compiled"),
    ("[Stage]", None),
    ("Start importing Assets",          "(2/7) Importing assets..."),
    ("Refresh completed",               "(2/7) Import done"),
    ("Compiling shader",                "(3/7) Compiling shaders..."),
    ("Stripping",                       "(4/7) Stripping code..."),
    ("BuildPlayer",                     "(4/7) Build pipeline..."),
    ("Building scenes",                 "(5/7) Building scenes..."),
    ("Packaging assets",                "(5/7) Packaging..."),
    ("Building Gradle project",         "(6/7) Gradle build..."),
    ("Packing APK",                     "(6/7) Packing APK..."),
    ("Exporting project",               "(6/7) Exporting Xcode..."),
    ("Moving file",                     "(7/7) Finalizing..."),
    ("[Build] OK",                      "(7/7) Build complete!"),
    ("[Build] FAILED",                  "Build failed!"),
]

PROGRESS_RE = re.compile(r'(\d+)\s*/\s*(\d+)')
