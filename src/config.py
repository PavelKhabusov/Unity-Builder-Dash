"""Config, history, build log persistence + project utilities."""
import os, json, subprocess, datetime

APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_PATH = os.path.join(APP_DIR, "config.json")
HISTORY_PATH = os.path.join(APP_DIR, "build_history.json")
BUILDS_LOG_PATH = os.path.join(APP_DIR, "builds_log.json")

DEFAULT_CONFIG = {
    "unity": "",
    "apk_dash": "",
    "theme": "system",  # "system", "dark", "light"
    "upload": {
        "enabled": False,
        "host": "",
        "user": "",
        "remote_dir": "",
        "rename_pattern": "{name}_mq3_{build}.apk"
    },
    "projects": []
}

# ── Config I/O ──

def load_config():
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH) as f:
                cfg = json.load(f)
                # Merge missing defaults
                for k, v in DEFAULT_CONFIG.items():
                    if k not in cfg: cfg[k] = v
                return cfg
        except: pass
    return dict(DEFAULT_CONFIG)

def save_config(cfg):
    with open(CONFIG_PATH, "w") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)

# ── ETA History ──

def load_history():
    if os.path.exists(HISTORY_PATH):
        try:
            with open(HISTORY_PATH) as f: return json.load(f)
        except: pass
    return {}

def save_history(h):
    with open(HISTORY_PATH, "w") as f: json.dump(h, f)

# ── Build Log (history of all builds) ──

def load_builds_log():
    if os.path.exists(BUILDS_LOG_PATH):
        try:
            with open(BUILDS_LOG_PATH) as f: return json.load(f)
        except: pass
    return []

def save_build_entry(project_name, target, success, duration, apk_size=None, build_number=None):
    log = load_builds_log()
    log.append({
        "project": project_name,
        "target": target,
        "success": success,
        "duration": duration,
        "apk_size_mb": round(apk_size / (1024*1024), 1) if apk_size else None,
        "build": build_number,
        "date": datetime.datetime.now().strftime("%Y-%m-%d %H:%M"),
    })
    if test_cases:
        log[-1]["test_cases"] = test_cases
    # Keep last 100
    if len(log) > 100: log = log[-100:]
    with open(BUILDS_LOG_PATH, "w") as f:
        json.dump(log, f, indent=2)

def save_test_entry(project_name, platform, passed, failed, skipped, total, duration, test_cases=None):
    log = load_builds_log()
    log.append({
        "project": project_name,
        "target": f"test-{platform}",
        "type": "test",
        "success": failed == 0,
        "duration": int(duration),
        "passed": passed,
        "failed": failed,
        "skipped": skipped,
        "total": total,
        "date": datetime.datetime.now().strftime("%Y-%m-%d %H:%M"),
    })
    if test_cases:
        log[-1]["test_cases"] = test_cases
    if len(log) > 100: log = log[-100:]
    with open(BUILDS_LOG_PATH, "w") as f:
        json.dump(log, f, indent=2)


# ── Project utilities ──

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

def get_build_number(path):
    try:
        with open(os.path.join(path, "ProjectSettings", "ProjectSettings.asset")) as f:
            for line in f:
                if "AndroidBundleVersionCode:" in line:
                    return line.split(":")[1].strip()
    except: pass
    return None

def get_unity_for_project(cfg, proj):
    """Get Unity path: per-project override or global."""
    return proj.get("unity") or cfg.get("unity", "")

def find_unity():
    base = os.path.expanduser("~/Unity/Hub/Editor")
    if not os.path.isdir(base): return ""
    for v in sorted(os.listdir(base), reverse=True):
        p = os.path.join(base, v, "Editor", "Unity")
        if os.path.isfile(p): return p
    return ""

def list_unity_versions():
    """List all installed Unity versions."""
    base = os.path.expanduser("~/Unity/Hub/Editor")
    if not os.path.isdir(base): return []
    versions = []
    for v in sorted(os.listdir(base), reverse=True):
        p = os.path.join(base, v, "Editor", "Unity")
        if os.path.isfile(p):
            versions.append((v, p))
    return versions

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
        with open(ps) as f: content = f.read()
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
            with open(log, errors='replace') as f: tail = f.read()[-5000:]
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
        r = subprocess.run(["git", "-C", path, "status", "--porcelain"],
                           capture_output=True, text=True, timeout=5)
        changes = len([l for l in r.stdout.strip().splitlines() if l.strip()])
        if changes > 0:
            issues.append(("info", f"{changes} uncommitted change(s)"))
        else:
            ok.append("Git: clean")
        r2 = subprocess.run(["git", "-C", path, "branch", "--show-current"],
                            capture_output=True, text=True, timeout=5)
        branch = r2.stdout.strip()
        if branch: ok.append(f"Branch: {branch}")
    except: pass

    return issues, ok

def upload_apk(cfg, proj, apk_path, log_cb=None, progress_cb=None):
    """Upload APK via SCP. Uses sshpass if password in config, otherwise opens terminal."""
    upload = proj.get("upload", cfg.get("upload", {}))
    if not upload.get("host"): return False

    build_num = get_build_number(proj["path"]) or "0"
    name = proj["name"]
    pattern = upload.get("rename_pattern", "{name}_{build}.apk")
    remote_name = pattern.format(name=name, build=build_num)

    host = upload["host"]
    user = upload.get("user", "")
    remote_dir = upload.get("remote_dir", "")
    password = upload.get("password", "")
    dest = f"{user}@{host}:{remote_dir}/{remote_name}" if user else f"{host}:{remote_dir}/{remote_name}"

    if log_cb: log_cb(f"Uploading {remote_name} → {host}...\n")

    # Upload via FTP using curl (works with Beget and most hosting)
    ftp_url = f"ftp://{host}/{remote_dir}/{remote_name}".replace("//", "/").replace("ftp:/", "ftp://")
    creds = f"{user}:{password}" if password else user

    try:
        proc = subprocess.Popen(
            ["curl", "-T", apk_path, ftp_url,
             "--user", creds, "--ftp-create-dirs", "-#"],
            stderr=subprocess.PIPE, stdout=subprocess.PIPE, text=True)

        # curl -# outputs progress to stderr like: "######## 45.2%"
        last_pct = ""
        for ch in iter(lambda: proc.stderr.read(1), ''):
            if ch == '\r' or ch == '\n':
                if last_pct and progress_cb:
                    # Extract percentage
                    import re as _re
                    m = _re.search(r'(\d+\.?\d*)%', last_pct)
                    if m:
                        pct = float(m.group(1))
                        progress_cb(pct / 100.0)
                last_pct = ""
            else:
                last_pct += ch

        proc.wait()
        if proc.returncode == 0:
            if progress_cb: progress_cb(1.0)
            if log_cb: log_cb(f"Uploaded: {remote_name}\n")
            return True
        else:
            if log_cb: log_cb(f"Upload failed (exit {proc.returncode})\n")
            return False
    except Exception as e:
        if log_cb: log_cb(f"Upload error: {e}\n")
        return False
