"""Remote iOS build pipeline: zip → scp → ssh osascript on Mac.

Replaces the Windows-side of CrazyMegaBuilder for Linux hosts. The Mac side
(ios_build.scpt) runs unchanged except for a small patch that skips the SMB
mount if iOS.zip is already on the Desktop (see server/ folder).
"""
import os, signal, socket, subprocess, threading, zipfile
from gi.repository import GLib


# ── Defaults ──

DEFAULT_REMOTE = {
    "mac_ip": "",
    "mac_user": "pavel",
    "mac_auth": "key",                 # "key" | "password"
    "mac_key_path": "~/.ssh/id_ed25519",
    "mac_password": "",
    "mac_work_dir": "/Users/pavel/Desktop",  # Mac-side base: .scpt, iOS.zip, iOS/ all live here
    "progress_port": 8080,
    # When True, ssh is spawned in an external terminal emulator instead of
    # being captured in-app. Mirrors CMB's "Окно терминала" toggle.
    "external_terminal": False,
    # Widget config — piped through as env vars to add_widget_dependency.rb
    # and sed-substituted into ios_build.applescript by patch_scpt.sh.
    "widget_bundle_id":     "com.example.myapp.widget",
    "widget_team_id":       "XXXXXXXXXX",
    "widget_target_name":   "URLImageWidget",
    "widget_folder_name":   "kartoteka.widget",
    "widget_app_group_id":  "group.com.example.myapp",
    # Devices: list of {"name": <xcodebuild destination>, "display_name": <UI>}
    "devices": [
        {"name": "iPhone 12 mini", "display_name": "iPhone 12 mini"},
        {"name": "iPad Pro",       "display_name": "iPad Pro"},
    ],
    # SMB (Windows-host legacy — leave empty on Linux)
    "smb_user":       "",
    "smb_password":   "",
    "smb_build_path": "",
    # Release pipeline (App Store: archive / validate / distribute).
    # apple_id + apple_app_password authenticate altool against App Store
    # Connect; release_team_id signs the archive (empty → xcodebuild infers).
    "apple_id":            "",
    "apple_app_password":  "",
    "release_team_id":     "",
}


def get_devices(cfg):
    """Return [(display_name, name), ...] from cfg.ios_remote.devices."""
    devs = (cfg.get("ios_remote") or {}).get("devices") or DEFAULT_REMOTE["devices"]
    out = []
    for d in devs:
        name = d.get("name") or ""
        disp = d.get("display_name") or name
        if name:
            out.append((disp, name))
    return out


def _has_tool(name):
    import shutil
    return shutil.which(name) is not None


# ── SSH key setup helpers ──

def generate_ssh_key(key_path="~/.ssh/id_ed25519", log_cb=None):
    """Create an ed25519 key at key_path if missing. Returns True on success."""
    key_path = os.path.expanduser(key_path)
    if os.path.exists(key_path):
        if log_cb: GLib.idle_add(log_cb, f"Key exists: {key_path}\n")
        return True
    ssh_dir = os.path.dirname(key_path)
    os.makedirs(ssh_dir, exist_ok=True)
    try: os.chmod(ssh_dir, 0o700)
    except OSError: pass
    try:
        r = subprocess.run(
            ["ssh-keygen", "-t", "ed25519", "-f", key_path, "-N", "", "-q"],
            capture_output=True, text=True, timeout=30)
        if r.returncode == 0:
            if log_cb: GLib.idle_add(log_cb, f"Generated key: {key_path}\n")
            return True
        if log_cb: GLib.idle_add(log_cb, f"ssh-keygen failed: {r.stderr}\n")
    except Exception as e:
        if log_cb: GLib.idle_add(log_cb, f"ssh-keygen error: {e}\n")
    return False


def copy_key_to_mac(remote, password, log_cb=None):
    """Run ssh-copy-id to install our public key on the Mac. Requires sshpass."""
    if not _has_tool("sshpass"):
        if log_cb:
            GLib.idle_add(log_cb,
                "sshpass not installed. Install it first:\n"
                "  Arch:   sudo pacman -S sshpass\n"
                "  Debian: sudo apt install sshpass\n")
        return False
    if not password:
        if log_cb: GLib.idle_add(log_cb, "Mac password is empty\n")
        return False
    if not remote.get("mac_ip"):
        if log_cb: GLib.idle_add(log_cb, "Mac IP is empty\n")
        return False

    key_path = os.path.expanduser(remote.get("mac_key_path", "~/.ssh/id_ed25519"))
    pub = key_path + ".pub"
    if not os.path.exists(pub):
        if not generate_ssh_key(key_path, log_cb):
            return False

    cmd = ["sshpass", "-p", password, "ssh-copy-id",
           "-i", pub,
           "-o", "StrictHostKeyChecking=accept-new",
           f'{remote["mac_user"]}@{remote["mac_ip"]}']
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if r.returncode == 0:
            if log_cb:
                GLib.idle_add(log_cb,
                    f"Key installed on {remote['mac_ip']} — SSH key auth ready\n")
            return True
        err = (r.stderr or r.stdout).strip()
        if log_cb: GLib.idle_add(log_cb, f"ssh-copy-id failed: {err}\n")
    except Exception as e:
        if log_cb: GLib.idle_add(log_cb, f"ssh-copy-id error: {e}\n")
    return False


def _find_terminal():
    """Return (cmd, exec_flag) for the first available terminal emulator."""
    for term, flag in [("gnome-terminal", "--"), ("konsole", "-e"),
                       ("xfce4-terminal", "-x"), ("alacritty", "-e"),
                       ("kitty", "--"), ("xterm", "-e")]:
        try:
            if subprocess.run(["which", term], capture_output=True,
                              timeout=2).returncode == 0:
                return term, flag
        except Exception:
            continue
    return None, None

# Fixed device list (matches CrazyMegaBuilder). Labels shown in dropdown,
# values are the osascript target names expected by ios_build.scpt.
DEVICES = [
    ("iPhone 12 mini", "RuniPhone12miniFull"),
    ("iPhone 13 mini", "RuniPhone13miniFull"),
    ("iPad Pro",       "RuniPadProFull"),
]


def get_remote_cfg(cfg):
    r = dict(DEFAULT_REMOTE)
    r.update(cfg.get("ios_remote", {}) or {})
    # Derive Mac paths from mac_work_dir. Explicit overrides in config still win.
    work = (r.get("mac_work_dir") or "/Users/pavel/Desktop").rstrip("/")
    r["mac_work_dir"] = work
    r.setdefault("mac_script_path", f"{work}/ios_build.scpt")
    r.setdefault("mac_zip_dest",    f"{work}/iOS.zip")
    return r


# ── Zip ──

def ios_build_subdir(build_dir):
    """Find the iOS Xcode project folder inside build_dir.

    Unity's BuildScript.BuildiOS writes to '{build_dir}/iOS' (lowercase 'i').
    CrazyMegaBuilder originally used 'iOS' (uppercase). Accept either.
    """
    for name in ("iOS", "iOS", "ios"):
        p = os.path.join(build_dir, name)
        if os.path.isdir(p):
            return p, name
    return None, None


def make_ios_zip(build_dir, log_cb=None):
    """Zip {build_dir}/iOS/ → {build_dir}/iOS.zip with 'iOS/' as archive root.

    The archive is laid out so `unzip iOS.zip` on the Mac produces an 'iOS/'
    directory directly, which ios_build.scpt expects at /Users/pavel/Desktop/iOS/.
    """
    src, _name = ios_build_subdir(build_dir)
    if not src:
        raise FileNotFoundError(
            f"iOS Xcode project not found in {build_dir} (expected iOS/ subfolder)")
    zip_path = os.path.join(build_dir, "iOS.zip")
    if os.path.exists(zip_path):
        os.remove(zip_path)

    if log_cb:
        GLib.idle_add(log_cb, f"Zipping {src} → iOS.zip...\n")

    # Archive root name is always 'iOS' so the Mac side unzips to Desktop/iOS/
    # regardless of whether Unity wrote 'iOS' (Kartoteka) or 'iOS' (CMB legacy).
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=1) as zf:
        for root, _dirs, files in os.walk(src):
            for f in files:
                full = os.path.join(root, f)
                rel = os.path.relpath(full, src)
                zf.write(full, os.path.join("iOS", rel))

    size_mb = os.path.getsize(zip_path) / (1024 * 1024)
    if log_cb:
        GLib.idle_add(log_cb, f"  Done ({size_mb:.0f} MB)\n")
    return zip_path


# ── SSH / SCP primitives ──

def _ssh_common_opts(remote):
    opts = ["-o", "StrictHostKeyChecking=accept-new",
            "-o", "ConnectTimeout=10",
            "-o", "ServerAliveInterval=30"]
    if remote["mac_auth"] == "key" and remote.get("mac_key_path"):
        opts += ["-i", os.path.expanduser(remote["mac_key_path"])]
    return opts


def _wrap_sshpass(remote, cmd):
    """If password auth is configured, prepend sshpass."""
    if remote["mac_auth"] == "password" and remote.get("mac_password"):
        return ["sshpass", "-p", remote["mac_password"]] + cmd
    return cmd


def _write_client_ip_cmd(work_dir):
    """Shell snippet (Mac-side) that updates host_ip in config.json.

    Host runs this via SSH before every action. Uses $SSH_CLIENT (set by
    sshd) — no host-side IP detection. Creates config.json if missing.
    """
    return (
        f'mkdir -p "{work_dir}" && '
        f'IP=$(echo "$SSH_CLIENT" | awk \'{{print $1}}\') && '
        f'python3 -c "import json, os; p=\'{work_dir}/config.json\'; '
        f'd=json.load(open(p)) if os.path.isfile(p) else {{}}; '
        f'd[\'host_ip\']=\'$IP\'; open(p,\'w\').write(json.dumps(d,indent=2))"'
    )


def _build_mac_config(remote):
    """Assemble the dict that becomes $WORK_DIR/config.json on the Mac."""
    return {
        "host_ip": "",  # filled by _write_client_ip_cmd on every action
        "widget_bundle_id":    remote.get("widget_bundle_id")    or "com.example.myapp.widget",
        "widget_team_id":      remote.get("widget_team_id")      or "XXXXXXXXXX",
        "widget_target":       remote.get("widget_target_name")  or "URLImageWidget",
        "widget_folder":       remote.get("widget_folder_name")  or "kartoteka.widget",
        "widget_app_group_id": remote.get("widget_app_group_id") or "group.com.example.myapp",
        "devices":             remote.get("devices") or [],
        # Release pipeline secrets — read by ios_build.scpt via readConfigKey().
        "apple_id":            remote.get("apple_id")           or "",
        "apple_app_password":  remote.get("apple_app_password") or "",
        "release_team_id":     remote.get("release_team_id")    or "",
    }


def test_connection(remote, log_cb=None, notify=True):
    """Try `ssh user@host echo ok` — returns True on success.

    When `notify=True` (explicit Connect button), also writes our host IP into
    $WORK_DIR/ip_address.txt so progress callbacks from the Mac AppleScript
    can reach us, and fires a native macOS notification as visual/audible
    confirmation on the Mac.

    When `notify=False` (auto-probe for status indicator), just does a silent
    `echo ok` — no Mac notification, no IP write. Keeps probes unobtrusive.
    """
    if not remote.get("mac_ip"):
        if log_cb: GLib.idle_add(log_cb, "Mac IP is empty\n")
        return False
    # Always write the client IP — silent probes too, so mac_console.app
    # and any in-flight .scpt have a current value. Notifications stay
    # opt-in (noisy macOS banner / Glass sound on the Mac).
    base = f'{_write_client_ip_cmd(remote["mac_work_dir"])} && echo ok'
    if notify:
        remote_cmd = (
            f'{base} && osascript -e '
            '\'display notification "Unity Builder Dash connected" '
            'with title "iOS Remote" sound name "Glass"\''
        )
    else:
        remote_cmd = base
    cmd = ["ssh"] + _ssh_common_opts(remote) + [
        "-o", "BatchMode=yes" if remote["mac_auth"] == "key" else "BatchMode=no",
        f'{remote["mac_user"]}@{remote["mac_ip"]}', remote_cmd]
    cmd = _wrap_sshpass(remote, cmd)
    if log_cb and notify:
        GLib.idle_add(log_cb, f"Testing SSH to {remote['mac_user']}@{remote['mac_ip']}...\n")
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        ok = r.returncode == 0 and "ok" in r.stdout
        if log_cb and notify:
            msg = "Connected (notification shown on Mac)\n" if ok \
                  else f"Failed: {r.stderr.strip() or r.stdout.strip()}\n"
            GLib.idle_add(log_cb, msg)
        return ok
    except Exception as e:
        if log_cb and notify:
            GLib.idle_add(log_cb, f"SSH error: {e}\n")
        return False


def _ssh_probe(remote, command, timeout, connect_timeout):
    """Single SSH attempt running `command`. Returns (ok, stdout+stderr)."""
    cmd = ["ssh"] + _ssh_common_opts(remote) + [
        "-o", f"ConnectTimeout={connect_timeout}",
        "-o", "BatchMode=yes" if remote["mac_auth"] == "key" else "BatchMode=no",
        f'{remote["mac_user"]}@{remote["mac_ip"]}', command]
    cmd = _wrap_sshpass(remote, cmd)
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.returncode == 0, (r.stdout + r.stderr)
    except Exception as e:
        return False, str(e)


# ── Mac keep-awake (LaunchAgent) ──
#
# Tested fact: an Apple Silicon MacBook on Wi-Fi + battery CANNOT be woken over
# the network. Asleep it answers ICMP from the network coprocessor but sshd is
# down, and an incoming connection does not wake the full system (verified: 132s
# of ping+SSH probing never brought sshd up). So the only reliable way to build
# remotely is to stop the Mac from sleeping in the first place. We install a
# user LaunchAgent that runs `caffeinate -i -s` forever (KeepAlive=true), so the
# system never idle-sleeps while the Mac is a build host. The user enables it
# once before stepping away; the screen still dims/locks normally.

KEEPAWAKE_LABEL = "com.unitybuilderdash.keepawake"


def _keepawake_plist_path(remote):
    return f'/Users/{remote["mac_user"]}/Library/LaunchAgents/{KEEPAWAKE_LABEL}.plist'


def _keepawake_plist_xml():
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
        '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">'
        '<plist version="1.0"><dict>'
        f'<key>Label</key><string>{KEEPAWAKE_LABEL}</string>'
        '<key>ProgramArguments</key><array>'
        '<string>/usr/bin/caffeinate</string>'
        '<string>-i</string><string>-s</string>'
        '</array>'
        '<key>RunAtLoad</key><true/>'
        '<key>KeepAlive</key><true/>'
        '</dict></plist>'
    )


def set_mac_keep_awake(remote, enable, log_cb=None):
    """Install/remove a LaunchAgent that pins the Mac awake via caffeinate.

    enable=True  → write the plist, load it, caffeinate starts now and on every
                   login/reboot until disabled. The Mac will not idle-sleep.
    enable=False → unload and delete the plist; the Mac returns to normal sleep.

    Returns True on success. Requires the Mac to be reachable RIGHT NOW (you
    can't toggle this on a Mac that's already asleep — wake it via lid/keyboard
    first, which is exactly why this is a "set it before you leave" switch).
    """
    if not remote.get("mac_ip"):
        if log_cb: GLib.idle_add(log_cb, "Mac IP is empty\n")
        return False
    plist = _keepawake_plist_path(remote)
    if enable:
        import base64 as _b64
        b64 = _b64.b64encode(_keepawake_plist_xml().encode()).decode()
        # Write plist, (re)load it. bootout first so a re-enable picks up changes
        # without erroring on "already loaded". `|| true` keeps it idempotent.
        cmd = (
            f'mkdir -p "$(dirname {plist})" && '
            f'echo {b64} | base64 -d > "{plist}" && '
            f'launchctl bootout gui/$(id -u)/{KEEPAWAKE_LABEL} 2>/dev/null || true; '
            f'launchctl bootstrap gui/$(id -u) "{plist}" && echo enabled'
        )
        ok, out = _ssh_probe(remote, cmd, timeout=15, connect_timeout=8)
        ok = ok and "enabled" in out
        if log_cb:
            GLib.idle_add(log_cb,
                "Mac keep-awake ENABLED — it won't sleep until you disable it.\n"
                if ok else f"Failed to enable keep-awake: {out.strip()}\n")
        return ok
    else:
        # bootout stops launchd from respawning it, then kill ONLY the agent's
        # bare `caffeinate -i -s` (anchored with $ so a build's
        # `caffeinate -i -s xcodebuild …` is spared — disabling keep-awake mid
        # build must not kill the running build's own no-sleep wrapper).
        cmd = (
            f'launchctl bootout gui/$(id -u)/{KEEPAWAKE_LABEL} 2>/dev/null || true; '
            f'rm -f "{plist}"; '
            f"pkill -f 'caffeinate -i -s$' 2>/dev/null || true; echo disabled"
        )
        ok, out = _ssh_probe(remote, cmd, timeout=15, connect_timeout=8)
        ok = ok and "disabled" in out
        if log_cb:
            GLib.idle_add(log_cb,
                "Mac keep-awake DISABLED — normal sleep restored.\n"
                if ok else f"Failed to disable keep-awake: {out.strip()}\n")
        return ok


def get_mac_keep_awake(remote):
    """Return True if the keep-awake LaunchAgent is currently loaded.

    Returns None if the Mac is unreachable (asleep / offline / not configured).
    """
    if not remote.get("mac_ip"):
        return None
    ok, out = _ssh_probe(
        remote, f'launchctl print gui/$(id -u)/{KEEPAWAKE_LABEL} >/dev/null 2>&1 '
                f'&& echo on || echo off',
        timeout=10, connect_timeout=6)
    if not ok:
        return None
    return "on" in out


def wake_mac(remote, log_cb=None, max_wait=30):
    """Best-effort: confirm the Mac is reachable over SSH before a remote build.

    Network wake does NOT work on Apple Silicon + Wi-Fi + battery (proven), so
    this does a short retry — enough to catch a Mac that's mid-darkwake or just
    woke — and otherwise tells the user to wake it / enable keep-awake. It also
    refreshes a bounded `caffeinate` so the Mac can't doze between scp and the
    end of the build even if the persistent keep-awake agent isn't installed.

    Returns True if reachable, False otherwise.
    """
    if not remote.get("mac_ip"):
        if log_cb: GLib.idle_add(log_cb, "Mac IP is empty\n")
        return False
    ip = remote["mac_ip"]

    ok, _ = _ssh_probe(remote, "echo ok", timeout=10, connect_timeout=8)
    if not ok:
        if log_cb:
            GLib.idle_add(log_cb, f"Mac not responding — retrying {ip} ({max_wait}s)...\n")
        import time as _t
        deadline = _t.monotonic() + max_wait
        while _t.monotonic() < deadline and not ok:
            try:
                subprocess.run(["ping", "-c", "2", "-i", "0.3", "-W", "1", ip],
                               capture_output=True, timeout=4)
            except Exception:
                pass
            ok, _ = _ssh_probe(remote, "echo ok", timeout=8, connect_timeout=6)
        if not ok:
            if log_cb:
                GLib.idle_add(log_cb,
                    "Mac is asleep and won't wake over Wi-Fi (Apple Silicon "
                    "limitation). Wake it manually (lid/keyboard), or enable "
                    '"Keep Mac awake" before stepping away.\n')
            return False

    # Ensure the Mac stays awake for the WHOLE build, not just the SSH session.
    # A detached `caffeinate &` over SSH is unreliable (it gets reaped when the
    # ssh session ends, and on Apple Silicon `-i` alone doesn't block system
    # sleep on battery) — proven: a build died mid-`pod install` with the Mac
    # asleep. The LaunchAgent is the only thing that reliably pins it awake, so
    # if the user hasn't enabled keep-awake we load it now. It persists until
    # explicitly disabled — matching "the Mac stays awake while it's a build
    # host". The user's checkbox reflects this state on next popup open.
    if get_mac_keep_awake(remote) is not True:
        if log_cb:
            GLib.idle_add(log_cb,
                "Keep-awake not active — enabling it so the Mac can't sleep "
                "during the build.\n")
        set_mac_keep_awake(remote, True, log_cb)
    return True


def scp_to_mac(zip_path, remote, log_cb=None):
    """Upload zip to {mac_zip_dest}. Returns True on success."""
    dest = f'{remote["mac_user"]}@{remote["mac_ip"]}:{remote["mac_zip_dest"]}'
    cmd = ["scp"] + _ssh_common_opts(remote) + [zip_path, dest]
    cmd = _wrap_sshpass(remote, cmd)
    if log_cb: GLib.idle_add(log_cb, f"Uploading to {remote['mac_ip']}...\n")
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        if r.returncode == 0:
            if log_cb: GLib.idle_add(log_cb, "  Upload done\n")
            return True
        if log_cb:
            GLib.idle_add(log_cb, f"  Upload failed: {r.stderr.strip()}\n")
        return False
    except Exception as e:
        if log_cb: GLib.idle_add(log_cb, f"  Upload error: {e}\n")
        return False


# ── Remote osascript runner ──

class RemoteRunner:
    """Runs `ssh user@mac osascript <script> <target>`, streams output to log_cb.

    Keeps a reference to the subprocess so it can be killed via `stop()`.
    One instance per run; create a new one for each action.
    """
    def __init__(self, remote, log_cb=None, done_cb=None, progress_cb=None,
                 log_bulk_cb=None):
        self.remote = remote
        self.log_cb = log_cb
        self.log_bulk_cb = log_bulk_cb
        self.done_cb = done_cb
        self.progress_cb = progress_cb
        self.process = None
        self.cancelled = False

    def run(self, target_arg):
        """Start SSH + osascript in a background thread."""
        threading.Thread(target=self._run, args=(target_arg,), daemon=True).start()

    def _release_config_cmd(self):
        """Shell snippet that merges the current release secrets into the Mac's
        config.json — so editing Apple ID / password / team in Settings takes
        effect without reinstalling the server. Only emitted for release actions
        (archiveApp/validateApp/distributeApp) to avoid pushing secrets every
        run. Returns '' for non-release actions. Base64-encoded to dodge quoting.
        """
        import base64 as _b64, json as _json
        r = self.remote
        rel = {k: r.get(k) or "" for k in
               ("apple_id", "apple_app_password", "release_team_id")}
        # Mac login password — needed to unlock the login keychain in the SSH
        # session so exportArchive/altool can read the Distribution cert and
        # the Xcode account (otherwise "User interaction is not allowed" →
        # "No Accounts"). Reuse mac_password (already configured for SSH auth).
        rel["mac_unlock_password"] = r.get("mac_password") or ""
        b64 = _b64.b64encode(_json.dumps(rel).encode()).decode()
        wd = r["mac_work_dir"]
        return (
            f'python3 -c "import json,os,base64; p=\'{wd}/config.json\'; '
            f'd=json.load(open(p)) if os.path.isfile(p) else {{}}; '
            f'd.update(json.loads(base64.b64decode(\'{b64}\'))); '
            f'open(p,\'w\').write(json.dumps(d,indent=2))"'
        )

    def _build_cmd(self, target_arg):
        r = self.remote
        script = r["mac_script_path"]
        osa = (f"osascript '{script}' '{target_arg}'" if target_arg
               else f"osascript '{script}'")
        # Refresh ip_address.txt every run so Mac always knows where to POST
        # progress back, even if our IP changed (DHCP, VPN, laptop move).
        remote_cmd = f"{_write_client_ip_cmd(r['mac_work_dir'])} && {osa}"
        # For release actions, also push the latest Apple ID / password / team
        # into config.json right before osascript reads them.
        if target_arg in ("archiveApp", "validateApp", "distributeApp"):
            remote_cmd = (f"{_write_client_ip_cmd(r['mac_work_dir'])} && "
                          f"{self._release_config_cmd()} && {osa}")
        cmd = ["ssh"] + _ssh_common_opts(r) + [
            f'{r["mac_user"]}@{r["mac_ip"]}', remote_cmd]
        return _wrap_sshpass(r, cmd)

    def _run(self, target_arg):
        cmd = self._build_cmd(target_arg)
        external = bool(self.remote.get("external_terminal"))
        if self.log_cb:
            GLib.idle_add(self.log_cb, f"→ osascript {target_arg or '(no arg)'}\n")

        if external:
            term, flag = _find_terminal()
            if not term:
                if self.log_cb:
                    GLib.idle_add(self.log_cb,
                        "No terminal emulator found — falling back in-app\n")
                external = False

        # Batch SSH stdout at ~20 Hz. During Xcode build, the .scpt emits
        # thousands of lines via `log` / `do shell script "xcodebuild …"` —
        # per-line idle_add floods the GTK main thread and trips GNOME's
        # "not responding / Force Quit or Wait" watchdog. Prefer log_bulk_cb
        # so LogView batches them under one begin/end_user_action.
        import time as _t
        pending = []
        last_flush = [0.0]
        def flush():
            if not pending: return
            batch = pending[:]
            pending.clear()
            last_flush[0] = _t.monotonic()
            bulk = self.log_bulk_cb
            cb = self.log_cb
            def _deliver(lines=batch):
                try:
                    if bulk is not None:
                        bulk(lines)
                    elif cb is not None:
                        for ln in lines:
                            try: cb(ln)
                            except Exception: pass
                except Exception: pass
                return False
            GLib.idle_add(_deliver)

        try:
            if external:
                full = [term, flag] + cmd
                self.process = subprocess.Popen(full, preexec_fn=os.setsid)
                self.process.wait()
                ok = (self.process.returncode == 0) and not self.cancelled
            else:
                self.process = subprocess.Popen(
                    cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    text=True, bufsize=1, preexec_fn=os.setsid)
                for line in self.process.stdout:
                    if self.cancelled:
                        break
                    pending.append(line)
                    if _t.monotonic() - last_flush[0] >= 0.05:
                        flush()
                flush()
                self.process.wait()
                ok = (self.process.returncode == 0) and not self.cancelled
        except Exception as e:
            if self.log_cb:
                GLib.idle_add(self.log_cb, f"SSH error: {e}\n")
            ok = False
        finally:
            flush()
        if self.done_cb:
            GLib.idle_add(self.done_cb, ok)

    def stop(self):
        self.cancelled = True
        if self.process:
            try:
                os.killpg(os.getpgid(self.process.pid), signal.SIGTERM)
            except (ProcessLookupError, OSError):
                pass


def install_mac_server(remote, log_cb=None):
    """Copy server/ files to the Mac's Desktop and run patch_scpt.sh there.

    Equivalent to the manual install steps from server/README.md §3+§5, but
    done in one shot after SSH key auth is already working.
    """
    if not remote.get("mac_ip"):
        if log_cb: GLib.idle_add(log_cb, "Mac IP is empty\n")
        return False

    # Locate the server/ folder next to src/ in the app root
    here = os.path.dirname(os.path.abspath(__file__))
    server_dir = os.path.join(os.path.dirname(here), "server")
    files = ["ios_build.applescript", "add_widget_dependency.rb", "patch_scpt.sh",
             "mac_console.applescript"]
    missing = [f for f in files if not os.path.isfile(os.path.join(server_dir, f))]
    if missing:
        if log_cb:
            GLib.idle_add(log_cb, f"Missing in server/: {', '.join(missing)}\n")
        return False

    host = f'{remote["mac_user"]}@{remote["mac_ip"]}'
    dest_dir = remote["mac_work_dir"]

    if log_cb: GLib.idle_add(log_cb, f"Installing on Mac → {dest_dir}/\n")

    # Ensure target folder exists on Mac
    mk_cmd = ["ssh"] + _ssh_common_opts(remote) + [
        host, f'mkdir -p "{dest_dir}"']
    mk_cmd = _wrap_sshpass(remote, mk_cmd)
    try:
        subprocess.run(mk_cmd, capture_output=True, text=True, timeout=15)
    except Exception:
        pass

    scp_cmd = ["scp"] + _ssh_common_opts(remote) + \
        [os.path.join(server_dir, f) for f in files] + [f"{host}:{dest_dir}/"]
    scp_cmd = _wrap_sshpass(remote, scp_cmd)
    try:
        r = subprocess.run(scp_cmd, capture_output=True, text=True, timeout=120)
        if r.returncode != 0:
            if log_cb:
                GLib.idle_add(log_cb, f"scp failed: {r.stderr.strip()}\n")
            return False
        if log_cb:
            GLib.idle_add(log_cb, f"  Copied {len(files)} files\n")
    except Exception as e:
        if log_cb: GLib.idle_add(log_cb, f"scp error: {e}\n")
        return False

    # Write the Mac-side config.json (devices + widget metadata) before
    # patch_scpt.sh runs — so the compiled .scpt and mac_console.app both
    # have live config when first launched.
    import json as _json
    mac_cfg = _build_mac_config(remote)
    cfg_json = _json.dumps(mac_cfg, indent=2, ensure_ascii=False)
    # Base64-encode to avoid quoting hell over SSH
    import base64 as _b64
    cfg_b64 = _b64.b64encode(cfg_json.encode("utf-8")).decode("ascii")
    # Write config.json AND populate host_ip from $SSH_CLIENT in one go, so the
    # Mac always has a usable value right after install (before any action).
    remote_shell = (
        f'echo {cfg_b64} | base64 -d > "{dest_dir}/config.json" && '
        f'IP=$(echo "$SSH_CLIENT" | awk \'{{print $1}}\') && '
        f'python3 -c "import json; p=\'{dest_dir}/config.json\'; '
        f'd=json.load(open(p)); d[\'host_ip\']=\'$IP\'; '
        f'open(p,\'w\').write(json.dumps(d,indent=2))"'
    )
    write_cfg_cmd = ["ssh"] + _ssh_common_opts(remote) + [host, remote_shell]
    write_cfg_cmd = _wrap_sshpass(remote, write_cfg_cmd)
    try:
        subprocess.run(write_cfg_cmd, capture_output=True, text=True, timeout=15)
        if log_cb: GLib.idle_add(log_cb, "  Wrote config.json\n")
    except Exception as e:
        if log_cb: GLib.idle_add(log_cb, f"  config.json write error: {e}\n")

    if log_cb: GLib.idle_add(log_cb, "Compiling ios_build.applescript on Mac...\n")
    # Pass all placeholders so patch_scpt.sh can substitute them and osacompile
    # ios_build.applescript → ios_build.scpt. No decompile/sed trickery needed.
    env = {
        "WORK_DIR":           dest_dir,
        "WIDGET_BUNDLE_ID":   remote.get("widget_bundle_id")    or "com.example.myapp.widget",
        "WIDGET_TEAM_ID":     remote.get("widget_team_id")      or "XXXXXXXXXX",
        "WIDGET_TARGET_NAME": remote.get("widget_target_name")  or "URLImageWidget",
        "WIDGET_FOLDER_NAME": remote.get("widget_folder_name")  or "kartoteka.widget",
        "APP_GROUP_ID":       remote.get("widget_app_group_id") or "group.com.example.myapp",
        "SMB_USER":           remote.get("smb_user")            or "",
        "SMB_PASS":           remote.get("smb_password")        or "",
        "SMB_BUILD_PATH":     remote.get("smb_build_path")      or "",
    }
    env_prefix = " ".join(f'{k}="{v}"' for k, v in env.items())
    patch_cmd = ["ssh"] + _ssh_common_opts(remote) + [
        host, f'{env_prefix} bash "{dest_dir}/patch_scpt.sh"']
    patch_cmd = _wrap_sshpass(remote, patch_cmd)
    try:
        r = subprocess.run(patch_cmd, capture_output=True, text=True, timeout=60)
        if log_cb:
            out = (r.stdout + r.stderr).strip()
            for line in out.splitlines():
                GLib.idle_add(log_cb, f"  {line}\n")
        if r.returncode == 0:
            if log_cb: GLib.idle_add(log_cb, "Mac ready.\n")
            return True
        return False
    except Exception as e:
        if log_cb: GLib.idle_add(log_cb, f"ssh patch error: {e}\n")
        return False


def open_ssh_terminal(remote, log_cb=None):
    """Open an interactive SSH session to the Mac in an external terminal."""
    if not remote.get("mac_ip"):
        if log_cb: GLib.idle_add(log_cb, "Mac IP is empty\n")
        return
    term, flag = _find_terminal()
    if not term:
        if log_cb:
            GLib.idle_add(log_cb, "No terminal emulator found (gnome-terminal/konsole/xterm)\n")
        return
    ssh_cmd = ["ssh"] + _ssh_common_opts(remote) + [
        f'{remote["mac_user"]}@{remote["mac_ip"]}']
    ssh_cmd = _wrap_sshpass(remote, ssh_cmd)
    try:
        subprocess.Popen([term, flag] + ssh_cmd, start_new_session=True)
        if log_cb: GLib.idle_add(log_cb, f"Opened {term} with SSH\n")
    except Exception as e:
        if log_cb: GLib.idle_add(log_cb, f"Failed to open terminal: {e}\n")


# ── Progress listener (TCP:8080) ──

class ProgressListener:
    """Listens on TCP port for progress strings from the Mac-side AppleScript.

    The .scpt sends lines like 'Stage [3/10]: Xcode archive' via netcat to the
    host's IP. We decode '[n/m]' for the progress bar and pass lines to log_cb.
    """
    def __init__(self, port, log_cb=None, progress_cb=None, log_bulk_cb=None):
        self.port = port
        # log_cb: per-line fallback. log_bulk_cb: gets the whole batch list at
        # once → used to drive LogView.append_lines under a single GTK
        # user-action (one layout pass per flush instead of per line).
        self.log_cb = log_cb
        self.log_bulk_cb = log_bulk_cb
        self.progress_cb = progress_cb
        self._sock = None
        self._thread = None
        self._running = False

    def _free_port_if_orphaned(self):
        """If a previous instance of this app left a listener behind, kill it.

        Matches only processes whose cmdline looks like our own (`build.py`
        under this repo, or the ubd module) so we never shoot innocent
        bystanders. No-op on platforms without lsof.
        """
        try:
            r = subprocess.run(["lsof", "-ti", f"TCP:{self.port}", "-sTCP:LISTEN"],
                               capture_output=True, text=True, timeout=3)
            pids = [p.strip() for p in r.stdout.splitlines() if p.strip().isdigit()]
        except Exception:
            return
        my_pid = str(os.getpid())
        repo_hint = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        for pid in pids:
            if pid == my_pid:
                continue
            try:
                with open(f"/proc/{pid}/cmdline", "rb") as f:
                    cmd = f.read().replace(b"\x00", b" ").decode("utf-8", "replace")
            except Exception:
                continue
            if "build.py" in cmd and repo_hint in cmd:
                try:
                    os.kill(int(pid), signal.SIGTERM)
                    if self.log_cb:
                        GLib.idle_add(self.log_cb,
                            f"Killed orphaned listener pid {pid}\n")
                    # Give the kernel a moment to release the socket
                    import time as _t; _t.sleep(0.3)
                except Exception:
                    pass

    def start(self):
        if self._running:
            return
        for attempt in (1, 2):
            try:
                self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                self._sock.bind(("0.0.0.0", self.port))
                self._sock.listen(1)
                self._sock.settimeout(1.0)
                break
            except OSError as e:
                try: self._sock.close()
                except Exception: pass
                self._sock = None
                if attempt == 1 and getattr(e, "errno", None) == 98:
                    # EADDRINUSE — probably our own zombie; try to reclaim.
                    self._free_port_if_orphaned()
                    continue
                if self.log_cb:
                    GLib.idle_add(self.log_cb, f"Progress listener failed: {e}\n")
                return
            except Exception as e:
                if self.log_cb:
                    GLib.idle_add(self.log_cb, f"Progress listener failed: {e}\n")
                return
        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def _loop(self):
        import re, time as _time
        progress_re = re.compile(r"\[\s*(\d+)\s*/\s*(\d+)")
        bee_re = re.compile(r"^\[\s*\d+\s*/\s*\d+\s+\d+[ms]?\]")

        # Batch ALL incoming lines in a list and flush to GTK at ~20 Hz. This
        # turns 1000s of idle_add calls per build into ~40, and lets LogView
        # group the inserts under begin/end_user_action (single layout pass).
        pending_lines = []     # list of strings (already newline-terminated)
        pending_frac = [None]  # most recent progress fraction
        pending_bee = [None]   # last seen Bee line (coalesced — only latest matters)
        last_flush = [0.0]

        def flush():
            # Inject the latest Bee line at the end so it's the last visible one
            if pending_bee[0] is not None:
                pending_lines.append(pending_bee[0] + "\n")
                pending_bee[0] = None
            if pending_lines and (self.log_bulk_cb or self.log_cb):
                batch = pending_lines[:]
                pending_lines.clear()
                # Single idle_add per batch — main-thread wake count drops
                # from per-line to ~20 Hz. Prefer log_bulk_cb so LogView can
                # hand the whole batch to append_lines() under one
                # begin/end_user_action (one GTK layout pass per flush).
                bulk = self.log_bulk_cb
                cb = self.log_cb
                def _deliver(lines=batch):
                    try:
                        if bulk is not None:
                            bulk(lines)
                        elif cb is not None:
                            for ln in lines:
                                try: cb(ln)
                                except Exception: pass
                    except Exception: pass
                    return False
                GLib.idle_add(_deliver)
            if pending_frac[0] is not None and self.progress_cb:
                GLib.idle_add(self.progress_cb, pending_frac[0])
                pending_frac[0] = None
            last_flush[0] = _time.monotonic()
            return False

        while self._running:
            try:
                client, _addr = self._sock.accept()
            except socket.timeout:
                if pending_lines or pending_bee[0] or pending_frac[0]:
                    flush()
                continue
            except OSError:
                break
            try:
                with client:
                    buf = b""
                    while self._running:
                        chunk = client.recv(4096)
                        if not chunk:
                            break
                        buf += chunk
                        while b"\n" in buf:
                            line, buf = buf.split(b"\n", 1)
                            text = line.decode("utf-8", errors="replace").strip()
                            if not text:
                                continue
                            m = progress_re.search(text)
                            if m:
                                cur, total = int(m.group(1)), int(m.group(2))
                                if total > 0:
                                    pending_frac[0] = min(cur / total, 1.0)
                            if bee_re.match(text):
                                pending_bee[0] = text  # coalesce — keep only the latest
                            else:
                                pending_lines.append(text + "\n")
                        # Time-based flush, roughly 20 Hz
                        if _time.monotonic() - last_flush[0] >= 0.05:
                            flush()
                    flush()  # on disconnect drain whatever's left
            except Exception:
                pass

    def stop(self):
        self._running = False
        if self._sock:
            try: self._sock.close()
            except: pass
            self._sock = None
