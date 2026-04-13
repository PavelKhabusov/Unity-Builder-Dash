"""Config and history persistence + project utilities."""
import os, json, subprocess

APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_PATH = os.path.join(APP_DIR, "config.json")
HISTORY_PATH = os.path.join(APP_DIR, "build_history.json")

DEFAULT_CONFIG = {
    "unity": "",
    "apk_dash": "",
    "projects": []
}

def load_config():
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH) as f: return json.load(f)
        except: pass
    return dict(DEFAULT_CONFIG)

def save_config(cfg):
    with open(CONFIG_PATH, "w") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)

def load_history():
    if os.path.exists(HISTORY_PATH):
        try:
            with open(HISTORY_PATH) as f: return json.load(f)
        except: pass
    return {}

def save_history(h):
    with open(HISTORY_PATH, "w") as f: json.dump(h, f)

def find_apk(proj):
    d = proj.get("build_dir", os.path.join(proj["path"], "Builds"))
    if not os.path.isdir(d): return None
    apks = [os.path.join(d, f) for f in os.listdir(d) if f.endswith(".apk")]
    return max(apks, key=os.path.getmtime) if apks else None

def get_version(path):
    try:
        with open(os.path.join(path, "ProjectSettings", "ProjectSettings.asset")) as f:
            ver = bld = ""
            for line in f:
                if "bundleVersion:" in line: ver = line.split(":")[1].strip()
                if "AndroidBundleVersionCode:" in line: bld = line.split(":")[1].strip()
        return f"v{ver} ({bld})" if ver else "?"
    except: return "?"

def find_unity():
    base = os.path.expanduser("~/Unity/Hub/Editor")
    if not os.path.isdir(base): return ""
    for v in sorted(os.listdir(base), reverse=True):
        p = os.path.join(base, v, "Editor", "Unity")
        if os.path.isfile(p): return p
    return ""

def find_apk_dash():
    for p in [
        os.path.expanduser("~/.local/share/nautilus/scripts/APK Dash"),
        os.path.join(os.path.dirname(APP_DIR), "apk-dash", "APK Dash"),
    ]:
        if os.path.isfile(p): return p
    return ""

def scan_project(path):
    """Quick health check of a Unity project without opening editor."""
    issues = []
    ok = []

    if not os.path.isdir(path):
        return [("error", "Project folder not found")], []

    ps = os.path.join(path, "ProjectSettings", "ProjectSettings.asset")
    if os.path.isfile(ps):
        with open(ps) as f:
            content = f.read()
        if "cloudProjectId:" in content:
            cid = [l for l in content.splitlines() if "cloudProjectId:" in l]
            val = cid[0].split(":")[1].strip() if cid else ""
            if val: ok.append(f"Cloud ID: {val[:12]}...")
            else: issues.append(("warn", "Cloud Project ID is empty"))
        else:
            issues.append(("warn", "No Cloud Project ID"))

        ver = [l.split(":")[1].strip() for l in content.splitlines() if "bundleVersion:" in l]
        bld = [l.split(":")[1].strip() for l in content.splitlines() if "AndroidBundleVersionCode:" in l]
        if ver: ok.append(f"Version: {ver[0]} (build {bld[0] if bld else '?'})")
    else:
        issues.append(("error", "ProjectSettings.asset missing"))

    lib = os.path.join(path, "Library")
    if os.path.isdir(lib):
        ok.append("Library/ present (cached)")
    else:
        issues.append(("warn", "Library/ missing — first build will be slow"))

    ebs = os.path.join(path, "ProjectSettings", "EditorBuildSettings.asset")
    if os.path.isfile(ebs):
        with open(ebs) as f: ebs_content = f.read()
        enabled = ebs_content.count("enabled: 1")
        disabled = ebs_content.count("enabled: 0")
        if disabled > 0:
            issues.append(("warn", f"{disabled} scene(s) disabled in Build Settings"))
        ok.append(f"{enabled} scene(s) enabled")

    log = os.path.join(lib, "EditorLog.log") if os.path.isdir(lib) else ""
    if os.path.isfile(log):
        try:
            with open(log, errors='replace') as f:
                tail = f.read()[-5000:]
            errs = tail.count(": error CS")
            warns = tail.count(": warning CS")
            if errs > 0:
                issues.append(("error", f"{errs} compilation error(s) in last session"))
            elif warns > 0:
                issues.append(("info", f"{warns} warning(s) in last session"))
            else:
                ok.append("No errors in last session")
        except: pass

    if os.path.exists(os.path.join(path, "Temp", "UnityLockfile")):
        issues.append(("warn", "UnityLockfile present — editor may be open"))

    try:
        r = subprocess.run(["git", "-C", path, "status", "--porcelain"], capture_output=True, text=True, timeout=5)
        changes = len([l for l in r.stdout.strip().splitlines() if l.strip()])
        if changes > 0:
            issues.append(("info", f"{changes} uncommitted change(s)"))
        else:
            ok.append("Git: clean")
        r2 = subprocess.run(["git", "-C", path, "branch", "--show-current"], capture_output=True, text=True, timeout=5)
        branch = r2.stdout.strip()
        if branch: ok.append(f"Branch: {branch}")
    except: pass

    return issues, ok
