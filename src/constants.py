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
    ("Begin MonoManager ReloadAssembly", "Compiling scripts..."),
    ("- Completed reload", "Scripts compiled"),
    ("[Stage]", None),
    ("Start importing Assets", "Importing assets..."),
    ("Refresh completed", "Import done"),
    ("Building scenes", "Building scenes..."),
    ("Packaging assets", "Packaging..."),
    ("Packing APK", "Packing APK..."),
    ("Building Gradle project", "Gradle build..."),
    ("Moving file", "Finalizing..."),
    ("[Build] OK", "Build complete!"),
    ("[Build] FAILED", "Build failed!"),
    ("Compiling shader", "Compiling shaders..."),
    ("Stripping", "Stripping code..."),
    ("BuildPlayer", "Build pipeline..."),
    ("Exporting project", "Exporting Xcode..."),
]

PROGRESS_RE = re.compile(r'(\d+)\s*/\s*(\d+)')
