"""Build pipeline shared by GUI, headless runner and CLI (no GTK widgets)."""
import os, subprocess, time

from gi.repository import GLib

from . import ios_remote
from .constants import TARGET_INFO, resolve_build_method
from .config import (find_apk, get_unity_for_project, get_build_number,
                     load_history, save_history, save_build_entry)
from .agent_state import tracker, read_state, pid_alive, unity_batch_processes
ADB = os.path.expanduser("~/.local/bin/adb") if os.path.exists(os.path.expanduser("~/.local/bin/adb")) else "adb"


# action_id → (needs_unity, needs_zip, needs_scp, osa_arg_of_device, label)
# {PREFIX} becomes "run"/"install" depending on cfg.ios_remote.run_with_test.
IOS_ACTIONS = {
    "full":          (True,  True,  True,  lambda dev: f"{{PREFIX}}Full:{dev}", "Full build"),
    "xcode":         (False, False, False, lambda dev: f"{{PREFIX}}Full:{dev}", "Xcode build"),
    "build_only":    (False, False, False, lambda dev: f"{{PREFIX}}:{dev}",     "Build & install (no refresh)"),
    "without_xcode": (True,  True,  True,  lambda _: "unpack",                  "Build without Xcode"),
    "archive":       (False, True,  False, lambda _: None,                      "Pack zip"),
    "unpack":        (False, False, False, lambda _: "unpack",                  "Unpack on Mac"),
    "all":           (False, True,  True,  lambda _: "unpack",                  "Pack & unpack"),
    "stop":          (False, False, False, lambda _: "stop",                    "Stop"),
    "clear_cache":   (False, False, False, lambda _: "clearCache",              "Clear .pcm cache"),
    "add_widget":    (False, False, False, lambda _: "addWidget",               "Add widget"),
    "clear_build":   (False, False, False, lambda _: "clearBuild",              "Clean build"),
    "update_pod":    (False, False, False, lambda _: "updatePod",               "Update Pod"),
    "open_xcode":    (False, False, False, lambda _: "openXcode",               "Open in Xcode"),
    # Release pipeline: archive → validate → distribute, on the unpacked iOS/ on the Mac.
    "archive_app":   (False, False, False, lambda _: "archiveApp",              "Archive (App Store)"),
    "validate_app":  (False, False, False, lambda _: "validateApp",             "Validate archive"),
    "distribute_app":(False, False, False, lambda _: "distributeApp",           "Distribute to App Store"),
}

AGENT_IOS_ACTIONS = ("full", "xcode", "build_only", "without_xcode")
IOS_MODES = ("incremental", "clean")
SYNC_OSA = {"unpack": "syncUnpack"}


def ios_mode_of(cfg, mode=None):
    m = mode or (cfg.get("ios_remote") or {}).get("transfer_mode") or "incremental"
    return m if m in IOS_MODES else "incremental"


def find_project(cfg, name):
    low = (name or "").lower()
    for p in cfg.get("projects", []):
        if p["name"].lower() == low:
            return p
    return None


def default_ios_device(cfg):
    devs = ios_remote.get_devices(cfg)
    return devs[0][1] if devs else "iPhone 12 mini"


def resolve_ios_action(cfg, action_id, device, mode=None):
    """→ (needs_unity, needs_zip, needs_scp, osa_arg, label, status_text) or None.

    Incremental mode swaps zip+scp+unpack for rsync (needs_scp == "rsync").
    """
    spec = IOS_ACTIONS.get(action_id)
    if not spec:
        return None
    needs_unity, needs_zip, needs_scp, osa_fn, label = spec
    osa_arg = osa_fn(device)
    if osa_arg and ios_mode_of(cfg, mode) == "incremental":
        orig = osa_arg
        if needs_zip and needs_scp:
            needs_zip, needs_scp = False, "rsync"
            osa_arg = SYNC_OSA.get(osa_arg, osa_arg)
        osa_arg = osa_arg.replace("Full:", "Sync:")
        if osa_arg != orig:
            label += " (incremental)"
    if osa_arg and "{PREFIX}" in osa_arg:
        prefix = "run" if (cfg.get("ios_remote") or {}).get("run_with_test") else "install"
        osa_arg = osa_arg.replace("{PREFIX}", prefix)
    dev_label = ""
    for lbl, name in ios_remote.get_devices(cfg):
        if name == device:
            dev_label = f" — {lbl}"
            break
    return needs_unity, needs_zip, needs_scp, osa_arg, label, f"iOS: {label}{dev_label}"


def ios_post_build(cfg, proj, needs_zip, needs_scp, osa_arg, log_cb, stage_cb,
                   run_remote, done, status_text="iOS remote"):
    """Zip → wake Mac → scp → osascript. Call on a background thread.

    stage_cb(text) is invoked via GLib.idle_add. run_remote(osa_arg, done) must
    start a RemoteRunner whose done_cb is `done`; otherwise done(ok) is idle-added.
    """
    def stage(t):
        GLib.idle_add(stage_cb, t)
        tracker.stage(t)

    stage(status_text)
    ok = True
    build_dir = proj.get("build_dir") or os.path.join(proj["path"], "Builds")

    if needs_zip:
        stage(f"{status_text} — zipping")
        try:
            ios_remote.make_ios_zip(build_dir, log_cb=log_cb)
        except Exception as e:
            GLib.idle_add(log_cb, f"Zip failed: {e}\n")
            ok = False

    # A sleeping MacBook drops sshd; wake it (and start caffeinate) before scp/ssh.
    if ok and (needs_scp or osa_arg is not None):
        stage(f"{status_text} — waking Mac")
        ok = ios_remote.wake_mac(ios_remote.get_remote_cfg(cfg), log_cb=log_cb)

    if ok and needs_scp == "rsync":
        stage(f"{status_text} — syncing")
        stamp = f"v1|{get_unity_for_project(cfg, proj) or ''}"
        ok = ios_remote.rsync_to_mac(build_dir, ios_remote.get_remote_cfg(cfg), stamp, log_cb=log_cb)
    elif ok and needs_scp:
        stage(f"{status_text} — uploading")
        zip_path = os.path.join(build_dir, "iOS.zip")
        ok = ios_remote.scp_to_mac(zip_path, ios_remote.get_remote_cfg(cfg), log_cb=log_cb)

    if ok and osa_arg is not None:
        stage(f"{status_text} — Mac")
        run_remote(osa_arg, done)
        return

    GLib.idle_add(done, ok)


def record_build(proj, target, ok, start_time):
    """Persist ETA history + builds_log entry. Returns duration in seconds."""
    name = proj["name"]
    duration = int(time.time() - start_time) if start_time else 0
    if ok and start_time:
        h = load_history()
        h[f"{name}_{target}"] = duration
        save_history(h)
    apk = find_apk(proj) if ok and target == "android" else None
    try:
        apk_size = os.path.getsize(apk) if apk and os.path.isfile(apk) else None
    except OSError:
        apk_size = None
    save_build_entry(name, target, ok, duration, apk_size,
                     get_build_number(proj["path"], target))
    return duration


def install_apk(proj, device, log_cb):
    """adb install -r of the project's APK. Blocking; call off the UI thread."""
    apk = find_apk(proj)
    if not apk:
        log_cb("Install: APK not found\n")
        return False
    cmd = [ADB] + (["-s", device] if device and device != "default" else []) + \
          ["install", "-r", apk]
    log_cb(f"Installing {os.path.basename(apk)} → {device}...\n")
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        log_cb((r.stdout + r.stderr).strip() + "\n")
        return r.returncode == 0 and "Success" in r.stdout
    except Exception as e:
        log_cb(f"Install error: {e}\n")
        return False


def parse_build_request(cfg, req):
    """Validate an API/CLI build request → (error_or_None, params)."""
    proj = find_project(cfg, req.get("project"))
    if not proj:
        return f"unknown project: {req.get('project')}", None
    target = (req.get("target") or "").lower()
    if target not in proj.get("targets", []):
        return f"{proj['name']} has no target '{target}' (targets: {proj.get('targets')})", None
    ios_action = req.get("ios_action") or "full"
    if target == "ios" and ios_action not in AGENT_IOS_ACTIONS:
        return f"ios_action must be one of {AGENT_IOS_ACTIONS}", None
    if req.get("ios_mode") and req["ios_mode"] not in IOS_MODES:
        return f"ios_mode must be one of {IOS_MODES}", None
    p = {
        "proj": proj, "target": target,
        "increment": bool(req.get("increment", True)),
        "scripts_only": bool(req.get("scripts_only", False)),
        "aab": bool(req.get("aab", False)) and target == "android",
        "install": req.get("install") or None,
        "ios_action": ios_action,
        "ios_mode": ios_mode_of(cfg, req.get("ios_mode")),
    }
    p["device"] = (p["install"] or default_ios_device(cfg)) if target == "ios" else None
    if target == "ios":
        known = [n for _l, n in ios_remote.get_devices(cfg)]
        if p["device"] not in known:
            return f"unknown iOS device '{p['device']}' (known: {known})", None
        if not ios_remote.get_remote_cfg(cfg).get("mac_ip"):
            return "ios_remote.mac_ip is empty in config", None
    unity = get_unity_for_project(cfg, proj)
    if resolve_ios_action(cfg, ios_action, p["device"] or "")[0] or target == "android":
        if not unity or not os.path.isfile(unity):
            return "Unity editor not found (check Settings)", None
    return None, p


def options_of(p):
    return {k: p[k] for k in ("increment", "scripts_only", "aab", "install", "ios_action", "ios_mode")}


def _lockfile_held(project_path):
    """True if a live Unity holds <project>/Temp/UnityLockfile."""
    import fcntl
    lock = os.path.join(project_path or "", "Temp", "UnityLockfile")
    if not os.path.isfile(lock):
        return False
    try:
        with open(lock, "rb") as f:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(f, fcntl.LOCK_UN)
        return False
    except BlockingIOError:
        return True
    except OSError:
        return False


def busy_reason(own_busy=False, project=None):
    """Why a new Unity build must not start now, or None. One build per machine."""
    if own_busy:
        return "a build is already running in this app"
    st = read_state()
    if st.get("running") and pid_alive(st.get("pid")):
        return (f"build {st.get('project')}/{st.get('target')} is running "
                f"(pid {st.get('pid')}, {st.get('source')})")
    procs = unity_batch_processes()
    if procs:
        return f"Unity -batchmode is already running (pid {procs[0]['pid']})"
    if project and _lockfile_held(project.get("path")):
        return f"Unity editor holds {project['name']}/Temp/UnityLockfile (project open in editor?)"
    return None


def build_plan(cfg, proj, target, increment=True, scripts_only=False, aab=False,
               ios_action="full", device=None, install=None, ios_mode=None):
    """Human-readable description of what a build would execute (for --dry-run)."""
    lines = []
    unity = get_unity_for_project(cfg, proj)
    lines.append(f"project:  {proj['name']}  ({proj['path']})")
    lines.append(f"target:   {target}")
    lines.append(f"unity:    {unity or '(not found)'}"
                 + ("" if unity and os.path.isfile(unity) else "  [MISSING]"))
    run_unity = True
    if target == "ios":
        spec = resolve_ios_action(cfg, ios_action, device or default_ios_device(cfg), ios_mode)
        if not spec:
            return [f"unknown iOS action: {ios_action}"]
        run_unity, needs_zip, needs_scp, osa_arg, label, _st = spec
        lines.append(f"ios:      action={ios_action} ({label}), device={device or default_ios_device(cfg)}")
    if run_unity:
        info = TARGET_INFO[target]
        method = resolve_build_method(info["method"], increment, scripts_only,
                                      aab=aab and target == "android")
        bt = "Android" if target == "android" else "iOS"
        lines.append(f"step:     {os.path.basename(unity or 'Unity')} -quit -batchmode "
                     f"-buildTarget {bt} -projectPath {proj['path']} -executeMethod {method}")
    if target == "ios":
        remote = ios_remote.get_remote_cfg(cfg)
        build_dir = proj.get("build_dir") or os.path.join(proj["path"], "Builds")
        if needs_zip:
            lines.append(f"step:     zip {build_dir}/iOS → iOS.zip")
        if needs_scp or osa_arg is not None:
            lines.append(f"step:     wake Mac {remote.get('mac_user')}@{remote.get('mac_ip') or '(no mac_ip)'}")
        if needs_scp == "rsync":
            lines.append(f"step:     rsync -c {build_dir}/iOS → {remote['mac_work_dir']}/iOS")
        elif needs_scp:
            lines.append(f"step:     scp iOS.zip → {remote['mac_zip_dest']}")
        if osa_arg is not None:
            lines.append(f"step:     ssh osascript {remote['mac_script_path']} '{osa_arg}'")
    elif install:
        lines.append(f"step:     adb -s {install} install -r <apk>")
    return lines
