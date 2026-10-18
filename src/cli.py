"""ubd — headless control of Unity Builder Dash for agents.

Exit codes: 0 ok · 1 build failed/cancelled · 2 usage/unknown project ·
3 busy (another Unity build) · 4 GUI running without agent API · 5 API error.
"""
import argparse, datetime, json, os, subprocess, sys, time
import urllib.request, urllib.error

from .config import load_config, load_builds_log, APP_DIR
from .agent_state import read_state, pid_alive, gui_processes, LOGS_DIR
ADB = os.path.expanduser("~/.local/bin/adb") if os.path.exists(os.path.expanduser("~/.local/bin/adb")) else "adb"

EXIT_OK, EXIT_FAILED, EXIT_USAGE, EXIT_BUSY, EXIT_OLD_GUI, EXIT_API = 0, 1, 2, 3, 4, 5


def _port():
    from .agent_api import api_port
    return api_port()


def api(method, path, body=None, timeout=20):
    url = f"http://127.0.0.1:{_port()}{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"content-type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try: return e.code, json.loads(e.read() or b"{}")
        except Exception: return e.code, {"error": str(e)}


def api_owner():
    """'gui' / 'headless' if someone serves the API, else None."""
    try:
        code, h = api("GET", "/health", timeout=2)
        if code == 200 and h.get("app") == "unity-builder-dash":
            return h.get("owner") or "unknown"
    except Exception:
        pass
    return None


def out(args, obj, text):
    print(json.dumps(obj, ensure_ascii=False, indent=1) if args.json else text)


def fmt_secs(s):
    m, s = divmod(int(s or 0), 60)
    return f"{m}:{s:02d}"


def status_obj():
    if api_owner():
        try:
            return api("GET", "/status")[1]
        except Exception:
            pass
    st = read_state()
    if st.get("running") and not pid_alive(st.get("pid")):
        st["running"] = False
        st["result"] = st.get("result") or "lost"
    if st.get("started_at"):
        st["elapsed_sec"] = int((st.get("finished_at") or time.time()) - st["started_at"])
    return st


# ── commands ──

def cmd_projects(args):
    cfg = load_config()
    items = [{"name": p["name"], "path": p["path"], "targets": p.get("targets", [])}
             for p in cfg.get("projects", [])]
    out(args, {"projects": items},
        "\n".join(f"{p['name']:<14} {','.join(p['targets']):<12} {p['path']}" for p in items))
    return EXIT_OK


def cmd_status(args):
    from .pipeline import busy_reason
    st = status_obj()
    owner = api_owner()
    st["api"] = owner
    st["gui_pids"] = gui_processes()
    st["busy_reason"] = busy_reason() if not st.get("running") else None
    if args.json:
        out(args, st, "")
        return EXIT_OK
    lines = [f"api:      {owner or 'off'}  (127.0.0.1:{_port()})",
             f"gui:      {'running ' + str(st['gui_pids']) if st['gui_pids'] else 'not running'}"]
    if st.get("project"):
        state = "RUNNING" if st.get("running") else (st.get("result") or "idle")
        lines += [f"build:    {st['project']} / {st['target']} ({st.get('action')}, {st.get('source')})  {state}",
                  f"stage:    {st.get('stage')}",
                  f"progress: {int((st.get('progress') or 0) * 100)}%",
                  f"elapsed:  {fmt_secs(st.get('elapsed_sec'))}",
                  f"log:      {st.get('log_path')}"]
    else:
        lines.append("build:    none tracked yet")
    if st["busy_reason"]:
        lines.append(f"busy:     {st['busy_reason']}")
    print("\n".join(lines))
    return EXIT_OK


def _tail(path, n):
    from .agent_api import tail_file
    return tail_file(path, n)


def cmd_log(args):
    st = status_obj()
    path = st.get("log_path")
    if not path or not os.path.isfile(path):
        print("no build log yet", file=sys.stderr)
        return EXIT_USAGE
    for ln in _tail(path, args.tail):
        print(ln)
    if args.follow:
        return follow(path, from_end=True)
    return EXIT_OK


def follow(path, from_end=True):
    """Stream the log until the build finishes. Exit code reflects the result."""
    with open(path, "r", errors="replace") as f:
        if from_end:
            f.seek(0, 2)
        idle = 0
        while True:
            line = f.readline()
            if line:
                sys.stdout.write(line); sys.stdout.flush()
                continue
            st = status_obj()
            if st.get("log_path") != path or not st.get("running"):
                rest = f.read()
                if rest: sys.stdout.write(rest)
                if st.get("log_path") == path:
                    res = st.get("result")
                    print(f"[ubd] {res}  {fmt_secs(st.get('elapsed_sec'))}")
                    return EXIT_OK if res == "success" else EXIT_FAILED
                return EXIT_OK
            time.sleep(1)


def cmd_history(args):
    builds = [b for b in load_builds_log() if "target" in b][-args.n:]
    out(args, {"builds": builds}, "\n".join(
        f"{b.get('date','')}  {b.get('project',''):<12} {b.get('target',''):<8} "
        f"{'OK  ' if b.get('success') else 'FAIL'} {fmt_secs(b.get('duration'))}"
        f"  build {b.get('build') or '-'}" + (f"  {b['apk_size_mb']} MB" if b.get('apk_size_mb') else "")
        for b in builds) or "no builds")
    return EXIT_OK


def cmd_devices(args):
    from . import ios_remote
    android = []
    try:
        r = subprocess.run([ADB, "devices", "-l"], capture_output=True, text=True, timeout=8)
        for ln in r.stdout.splitlines()[1:]:
            parts = ln.split()
            if len(parts) >= 2:
                model = next((x.split(":", 1)[1] for x in parts if x.startswith("model:")), "")
                android.append({"serial": parts[0], "state": parts[1], "model": model})
    except Exception as e:
        android.append({"error": str(e)})
    ios = [{"name": n, "display_name": d} for d, n in ios_remote.get_devices(load_config())]
    lines = ["android (adb serial — for --install):"]
    lines += [f"  {d.get('serial')}  {d.get('state')}  {d.get('model')}" for d in android] or ["  none"]
    lines.append("ios (xcodebuild name — for --install):")
    lines += [f"  {d['name']}  ({d['display_name']})" for d in ios]
    out(args, {"android": android, "ios": ios}, "\n".join(lines))
    return EXIT_OK


def cmd_ios_status(args):
    from . import ios_remote
    remote = ios_remote.get_remote_cfg(load_config())
    if not remote.get("mac_ip"):
        out(args, {"configured": False}, "Mac not configured (ios_remote.mac_ip empty)")
        return EXIT_FAILED
    ok = ios_remote.test_connection(remote, notify=False)
    batt = ios_remote.get_mac_battery(remote) if ok else None
    res = {"configured": True, "host": f"{remote['mac_user']}@{remote['mac_ip']}",
           "reachable": ok, "battery": batt}
    txt = f"mac:      {res['host']}  {'reachable' if ok else 'UNREACHABLE (asleep / off network?)'}"
    if batt:
        txt += (f"\nbattery:  {batt['percent']}%  {batt['state']}"
                f"  {'AC' if batt['ac'] else 'on battery'}")
    out(args, res, txt)
    return EXIT_OK if ok else EXIT_FAILED


def _build_req(args):
    req = {"project": args.project, "target": args.target,
           "scripts_only": args.scripts_only, "aab": args.aab,
           "increment": not args.no_increment, "install": args.install,
           "ios_action": args.ios_action, "ios_mode": args.ios_mode}
    return req


def cmd_build(args):
    from . import pipeline
    cfg = load_config()
    req = _build_req(args)
    err, p = pipeline.parse_build_request(cfg, req)
    if err:
        print(f"error: {err}", file=sys.stderr)
        return EXIT_USAGE
    owner = api_owner()
    guis = gui_processes()

    if args.dry_run:
        print("DRY RUN — nothing is started")
        for ln in pipeline.build_plan(cfg, p["proj"], p["target"], p["increment"],
                                      p["scripts_only"], p["aab"], p["ios_action"],
                                      p["device"], p["install"], p["ios_mode"]):
            print("  " + ln)
        if owner:
            print(f"  via:      POST /build → running {owner} (127.0.0.1:{_port()})")
        elif guis:
            print(f"  via:      REFUSED — GUI {guis} is running without agent API (restart it)")
        else:
            print(f"  via:      headless runner (python3 -m src.cli _headless), log in {LOGS_DIR}")
        reason = pipeline.busy_reason(project=p["proj"])
        print(f"  busy:     {reason or 'no'}")
        return EXIT_OK

    if owner:
        code, res = api("POST", "/build", req)
        if code == 409:
            print(res.get("error"), file=sys.stderr)
            return EXIT_BUSY
        if code != 200:
            print(f"error: {res.get('error') or code}", file=sys.stderr)
            return EXIT_USAGE if code == 400 else EXIT_API
        print(f"started in {owner}: {p['proj']['name']} / {p['target']}  log: {res.get('log_path')}")
        return _maybe_wait(args, res.get("log_path"))

    if guis:
        print(f"GUI is running (pid {guis}) but its agent API is off — old version. "
              "Restart Unity Builder Dash, then retry.", file=sys.stderr)
        return EXIT_OLD_GUI
    reason = pipeline.busy_reason(project=p["proj"])
    if reason:
        print(f"busy: {reason}", file=sys.stderr)
        return EXIT_BUSY

    os.makedirs(LOGS_DIR, exist_ok=True)
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    stdout_path = os.path.join(LOGS_DIR, f"headless_{ts}.out")
    with open(stdout_path, "w") as fo:
        proc = subprocess.Popen([sys.executable, "-m", "src.cli", "_headless", json.dumps(req)],
                                cwd=APP_DIR, stdout=fo, stderr=subprocess.STDOUT,
                                stdin=subprocess.DEVNULL, start_new_session=True)
    for _ in range(40):
        time.sleep(0.25)
        st = read_state()
        if st.get("running") and st.get("pid") == proc.pid:
            print(f"started headless (pid {proc.pid}): {p['proj']['name']} / {p['target']}"
                  f"  log: {st.get('log_path')}")
            return _maybe_wait(args, st.get("log_path"))
        if proc.poll() is not None:
            break
    print(f"headless runner did not start (exit {proc.poll()}), see {stdout_path}", file=sys.stderr)
    with open(stdout_path) as f:
        sys.stderr.write(f.read()[-2000:])
    return EXIT_BUSY if proc.poll() == 3 else EXIT_FAILED


def _maybe_wait(args, log_path):
    if not args.wait or not log_path:
        return EXIT_OK
    for _ in range(20):
        if os.path.isfile(log_path):
            break
        time.sleep(0.5)
    return follow(log_path, from_end=False)


def cmd_cancel(args):
    if api_owner():
        code, res = api("POST", "/cancel")
        out(args, res, "cancelled" if code == 200 else f"error: {res.get('error') or code}")
        return EXIT_OK if code == 200 else EXIT_API
    print("nothing to cancel: no app is serving the agent API", file=sys.stderr)
    return EXIT_FAILED


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["_headless"]:
        from .headless import main as hmain
        return hmain(argv[1])

    ap = argparse.ArgumentParser(prog="ubd", description="Unity Builder Dash — agent CLI")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("projects", help="projects from config")
    sub.add_parser("status", help="current/last build")
    lp = sub.add_parser("log", help="build log tail")
    lp.add_argument("--tail", type=int, default=100)
    lp.add_argument("--follow", "-f", action="store_true")
    hp = sub.add_parser("history", help="recent builds")
    hp.add_argument("n", type=int, nargs="?", default=10)
    sub.add_parser("devices", help="adb + iOS devices")
    sub.add_parser("ios-status", help="Mac reachability + battery")
    sub.add_parser("cancel", help="cancel the running build")
    bp = sub.add_parser("build", help="start a build")
    bp.add_argument("project")
    bp.add_argument("target", choices=["android", "ios"])
    bp.add_argument("--scripts-only", action="store_true")
    bp.add_argument("--aab", action="store_true")
    bp.add_argument("--no-increment", action="store_true")
    bp.add_argument("--install", metavar="DEVICE")
    bp.add_argument("--ios-action", default="full",
                    choices=["full", "xcode", "build_only", "without_xcode"])
    bp.add_argument("--ios-mode", choices=["incremental", "clean"],
                    help="iOS transfer: rsync + incremental Xcode, or zip + clean unpack "
                         "(default: ios_remote.transfer_mode, else incremental)")
    bp.add_argument("--wait", "-w", action="store_true", help="follow log, exit with build result")
    bp.add_argument("--dry-run", action="store_true")
    for p in sub.choices.values():
        p.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
    args = ap.parse_args(argv)
    return {"projects": cmd_projects, "status": cmd_status, "log": cmd_log,
            "history": cmd_history, "devices": cmd_devices, "ios-status": cmd_ios_status,
            "cancel": cmd_cancel, "build": cmd_build}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
