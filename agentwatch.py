#!/usr/bin/env python3
"""Live Antigravity agent status on a Bangle.js 2.

Tails Antigravity's on-disk agent transcripts and pushes state transitions
(working / waiting / done / idle) to the watch the moment they happen, over a
persistent BLE connection held by BangleBridge.app (daemon mode, fed through
a named pipe). The watch runs the bundled `agentwatch` app (watchapp/), which
draws a status screen and buzzes per state; without the app installed, `done`
still falls back to a plain buzz + message.

Usage:
  python3 agentwatch.py                  # watch transcripts, push states live
  python3 agentwatch.py install-app      # install the watch app over BLE
  python3 agentwatch.py set working      # push a state by hand
  python3 agentwatch.py test             # push a test 'done'
  python3 agentwatch.py --dry-run        # print instead of BLE
"""

import argparse
import base64
import glob
import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
BRIDGE_APP = os.path.join(HERE, "tools", "BangleBridge.app")
FIFO = os.path.join(HERE, "tools", "bridge.cmd")
DAEMON_LOG = os.path.join(HERE, "tools", "daemon.log")
WATCH_APP = os.path.join(HERE, "watchapp", "agentwatch.app.js")

TRANSCRIPT_ROOTS = [
    os.path.expanduser("~/.gemini/antigravity-ide/brain"),
    os.path.expanduser("~/.gemini/antigravity/brain"),
]
TRANSCRIPT_GLOB = "*/.system_generated/logs/transcript.jsonl"

STATES = ("idle", "working", "waiting", "attention", "done")
# substrings in a step's status that mean "agent is waiting on the user"
WAITING_MARKERS = ("USER", "PENDING", "AWAIT", "APPROV")

# optional done-summaries (enabled with --summarize; needs a Gemini API key
# from --gemini-key, $GEMINI_API_KEY, or tools/gemini.key — never committed)
GEMINI_KEY_FILE = os.path.join(HERE, "tools", "gemini.key")
GEMINI_MODEL = "gemini-3.1-flash-lite"


# ---------------------------------------------------------------- bridge I/O

def build_bridge_if_needed():
    """Compile the Swift bridge and assemble BangleBridge.app on first run."""
    binary = os.path.join(BRIDGE_APP, "Contents", "MacOS", "banglebridge")
    if os.path.exists(binary):
        return
    tools = os.path.join(HERE, "tools")
    print("building BangleBridge (first run, needs Xcode command line tools)...")
    subprocess.run(
        ["swiftc", "-O", "banglebridge.swift", "-o", "banglebridge",
         "-framework", "CoreBluetooth",
         "-Xlinker", "-sectcreate", "-Xlinker", "__TEXT",
         "-Xlinker", "__info_plist", "-Xlinker", "Info.plist"],
        cwd=tools, check=True)
    os.makedirs(os.path.dirname(binary), exist_ok=True)
    subprocess.run(["cp", os.path.join(tools, "BangleBridge-Info.plist"),
                    os.path.join(BRIDGE_APP, "Contents", "Info.plist")], check=True)
    subprocess.run(["cp", os.path.join(tools, "banglebridge"), binary], check=True)
    subprocess.run(["codesign", "-s", "-", "-f", BRIDGE_APP], check=True)
    print("built — macOS will show a one-time Bluetooth permission prompt; "
          "click Allow")


def ensure_daemon(args):
    build_bridge_if_needed()
    if not os.path.exists(FIFO):
        os.mkfifo(FIFO)
    if subprocess.run(["pgrep", "-f", "banglebridge daemon"],
                      capture_output=True).returncode == 0:
        return
    subprocess.run(["open", "-n", BRIDGE_APP, "--args",
                    "daemon", args.name, "--cmd", FIFO, "--log", DAEMON_LOG],
                   check=True)
    print("bridge daemon launched — waiting for watch connection...")
    deadline = time.time() + 20
    while time.time() < deadline:
        time.sleep(0.5)
        try:
            with open(DAEMON_LOG) as f:
                if "READY" in f.read():
                    print("bridge READY")
                    return
        except OSError:
            pass
    print("warning: watch not connected yet (bridge keeps scanning in "
          "the background; states will flow once it connects)")


def fifo_send(data: bytes, args) -> None:
    """Write bytes to the daemon's FIFO, starting the daemon if needed."""
    for attempt in (1, 2):
        try:
            fd = os.open(FIFO, os.O_WRONLY | os.O_NONBLOCK)
            try:
                os.write(fd, data)
            finally:
                os.close(fd)
            return
        except OSError:
            if attempt == 1:
                ensure_daemon(args)
                time.sleep(1.0)
            else:
                raise


def js_line(js: str) -> bytes:
    # \x10 suppresses echo. (No \x03 here: a Ctrl-C landing while the
    # previous line is mid-flash-write can abort the following statement —
    # use unstick() explicitly before a batch instead.)
    return b"\x10" + js.encode() + b"\n"


def unstick(args) -> None:
    """Clear any stuck/partial console input before a command batch."""
    fifo_send(b"\x03\n", args)
    time.sleep(0.5)


def send_state(state: str, msg: str, args, quiet: bool = False,
               fallback: bool = False) -> None:
    stamp = time.strftime("%H:%M:%S")
    if not quiet:
        print(f"[{stamp}] STATE: {state}" + (f" — {msg}" if msg else ""))
    if args.dry_run:
        return
    m = json.dumps(msg or "")
    # states only reach the Antigravity app; if it isn't open they are
    # silently dropped — no stray notifications on other watch screens
    js = f'typeof _AW=="function"&&_AW({json.dumps(state)},{m})'
    if fallback:  # explicit `test`: stay visible even without the app open
        js = (f'if(typeof _AW=="function"){{_AW({json.dumps(state)},{m})}}'
              f'else{{Bangle.buzz(600);E.showMessage({m},"Antigravity")}}')
    try:
        fifo_send(js_line(js), args)
    except OSError as e:
        print(f"[{stamp}] bridge send failed: {e}", file=sys.stderr)


# ------------------------------------------------------------- app installer

def storage_lines(name: str, data: bytes):
    """JS lines that stream `data` into watch storage file `name`."""
    total = len(data)
    b64 = base64.b64encode(data).decode()
    lines = []
    step = 144  # base64 chars per line; must be a multiple of 4
    for i in range(0, len(b64), step):
        chunk = b64[i:i + step]
        off = i // 4 * 3
        if i == 0:
            lines.append(f'require("Storage").write("{name}",atob("{chunk}"),0,{total})')
        else:
            lines.append(f'require("Storage").write("{name}",atob("{chunk}"),{off})')
    return lines


class WatchLog:
    """Tail of the daemon log, for reading the watch's replies."""

    def __init__(self):
        self.pos = os.path.getsize(DAEMON_LOG) if os.path.exists(DAEMON_LOG) else 0

    def wait_for(self, marker: str, timeout: float):
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                with open(DAEMON_LOG) as f:
                    f.seek(self.pos)
                    chunk = f.read()
            except OSError:
                chunk = ""
            for line in chunk.splitlines():
                if marker in line:
                    self.pos += chunk.index(marker) + len(marker)
                    return line
            time.sleep(0.15)
        return None


def send_acked(js: str, ack: str, log: WatchLog, args,
               timeout: float = 5.0, tries: int = 3):
    """Send one JS line and wait until the watch itself echoes the ack."""
    line = b"\x10" + (js + f';Bluetooth.println("{ack}")').encode() + b"\n"
    for attempt in range(tries):
        fifo_send(line, args)
        if log.wait_for(ack, timeout):
            return True
        print(f"  no ack for {ack} (attempt {attempt + 1}/{tries}), retrying...")
    return False


APP_MARKER = os.path.join(HERE, "tools", ".app-installed")


def app_bundle():
    """(js lines to install, fingerprint of what they install)."""
    import hashlib
    with open(WATCH_APP, "rb") as f:
        src = f.read()
    lines = storage_lines("agentwatch.app.js", src)
    info = {"id": "agentwatch", "name": "Antigravity", "type": "app",
            "src": "agentwatch.app.js", "version": "0.5"}
    icon = b""
    icon_path = os.path.join(HERE, "watchapp", "agentwatch.img")
    if os.path.exists(icon_path):
        with open(icon_path, "rb") as f:
            icon = f.read()
        lines += storage_lines("agentwatch.img", icon)
        info["icon"] = "agentwatch.img"
    # single-quoted JS string: Espruino's console chokes on \"-escaped
    # quotes inside \x10 lines (silently drops the whole line)
    lines.append(f"require(\"Storage\").write(\"agentwatch.info\",'{json.dumps(info)}')")
    fp = hashlib.sha1(src + icon + json.dumps(info).encode()).hexdigest()
    return lines, len(src), len(icon), fp


def beacon_wait(log_pos: int, prefix: str, timeout: float):
    """Wait for a 'DEVICE: <prefix>...' beacon in the daemon log."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with open(DAEMON_LOG) as f:
                f.seek(log_pos)
                for ln in f.read().splitlines():
                    if f"DEVICE: {prefix}" in ln:
                        return ln.split("DEVICE:")[1].split()[0].strip()
        except OSError:
            pass
        time.sleep(2)
    return None


def install_app(args):
    lines, src_len, icon_len, fp = app_bundle()
    log = WatchLog()
    if send_acked('1', "AWOK", log, args, timeout=4, tries=2):
        # verified path: the watch acks every line
        print(f"uploading app ({src_len}B) + icon ({icon_len}B) with per-line acks...")
        for i, l in enumerate(lines):
            if not send_acked(l, f"AK{i}", log, args, tries=3):
                sys.exit(f"upload failed at line {i + 1}/{len(lines)} — aborting "
                         "(nothing launched; re-run install-app to retry)")
            if (i + 1) % 10 == 0:
                print(f"  {i + 1}/{len(lines)} lines confirmed")
        fifo_send(js_line('Bluetooth.println("INFO="+require("Storage").read("agentwatch.info"))'), args)
        line = log.wait_for("INFO=", 6)
        if not line or "Antigravity" not in line:
            sys.exit(f"verification failed: info readback was {line!r} — "
                     "not launching; re-run install-app")
        print(f"verified on-watch: {line.split('INFO=', 1)[1]}")
    else:
        # open-loop path: stream slowly, then have the watch broadcast a
        # verification beacon in its advertising name (no return channel
        # needed — some Mac Bluetooth stacks eat the UART notifications)
        print("no console echo — using open-loop install with beacon verification")
        unstick(args)
        payload = b"".join(js_line(l) for l in lines)
        print(f"streaming {len(payload)} bytes ({len(lines)} lines)...")
        fifo_send(payload, args)
        wait = len(payload) / 600 + 0.15 * len(lines) + 5
        print(f"waiting ~{int(wait)}s for transfer + flash writes...")
        time.sleep(wait)
        probe = ('var _i=(require("Storage").read("agentwatch.info")||"");'
                 'NRF.setAdvertising({},{name:"V"+((_i.indexOf("Antigravity")>=0)?"Y":"N")});'
                 'setTimeout(function(){NRF.setAdvertising({},{name:"Bangle.js"});},12000);'
                 'NRF.disconnect()')
        log_pos = os.path.getsize(DAEMON_LOG)
        fifo_send(js_line(probe), args)
        beacon = beacon_wait(log_pos, "V", 40)
        if beacon != "VY":
            sys.exit(f"verification failed (beacon: {beacon or 'none'}) — "
                     "not launching; re-run install-app")
        print("verified on-watch via beacon")
        # wait for the daemon to reconnect before launching
        ready = open(DAEMON_LOG).read().count("READY")
        deadline = time.time() + 90
        while time.time() < deadline:
            time.sleep(3)
            if open(DAEMON_LOG).read().count("READY") > ready:
                break
        time.sleep(2)
    fifo_send(js_line('load("agentwatch.app.js")'), args)
    print("launched")
    with open(APP_MARKER, "w") as f:
        f.write(fp)


def ensure_app(args):
    """Install the watch app if this exact version isn't known to be on it."""
    _, _, _, fp = app_bundle()
    try:
        with open(APP_MARKER) as f:
            if f.read().strip() == fp:
                return
    except OSError:
        pass
    print("watch app missing or outdated — installing...")
    install_app(args)


# ------------------------------------------------------------- state machine

class StateTracker:
    """Derives one global agent state from all transcript files."""

    def __init__(self, roots, settle: float, idle_after: float):
        self.roots = roots
        self.settle = settle
        self.idle_after = idle_after
        self.offsets = {}         # path -> consumed byte offset
        self.last_step = {}       # path -> last parsed step (any source)
        self.last_model = {}      # path -> last step with source == MODEL
        self.last_model_time = {} # path -> when we observed that model step
        self.last_append = {}     # path -> time of last observed growth
        self.started = time.time()

    def files(self):
        for root in self.roots:
            yield from glob.glob(os.path.join(root, TRANSCRIPT_GLOB))

    def poll(self):
        now = time.time()
        for path in self.files():
            try:
                size = os.path.getsize(path)
            except OSError:
                continue
            if path not in self.offsets:
                fresh = os.path.getmtime(path) >= self.started - 5
                self.offsets[path] = 0 if fresh else size
            if size < self.offsets[path]:
                self.offsets[path] = 0
            if size > self.offsets[path]:
                with open(path, "r", errors="replace") as f:
                    f.seek(self.offsets[path])
                    new = f.readlines()
                    self.offsets[path] = f.tell()
                for line in new:
                    try:
                        step = json.loads(line)
                    except ValueError:
                        continue
                    self.last_step[path] = step
                    if step.get("source") == "MODEL":
                        self.last_model[path] = step
                        self.last_model_time[path] = now
                if new:
                    self.last_append[path] = now
        return self.state(now)

    def state(self, now):
        """Returns (state, raw final response content or '')."""
        active = [p for p, t in self.last_append.items()
                  if now - t < self.idle_after]
        if not active:
            return "idle", ""
        newest = max(active, key=lambda p: self.last_append[p])
        step = self.last_step.get(newest) or {}
        status = str(step.get("status", "")).upper()
        if status != "DONE" and any(m in status for m in WAITING_MARKERS):
            return "waiting", ""
        # 'done' keys off the last MODEL step, so trailing SYSTEM/ephemeral
        # steps after the final response can't delay or mask it
        m = self.last_model.get(newest)
        if (m and m.get("type") == "PLANNER_RESPONSE"
                and str(m.get("status", "")).upper() == "DONE"
                and now - self.last_model_time[newest] >= self.settle):
            content = m.get("content", "")
            # a turn that ends with a question is the editor asking the
            # user something, not a finished task
            if clean_markdown(content).endswith("?"):
                return "attention", content
            return "done", content
        return "working", ""


def clean_markdown(text: str) -> str:
    t = re.sub(r"```.*?```", " ", text, flags=re.S)
    t = re.sub(r"`([^`]*)`", r"\1", t)
    t = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", t)
    t = re.sub(r"\$[^$\n]*\$", " ", t)          # inline LaTeX
    t = re.sub(r"[*_#>|~]+", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def question_snippet(content: str, limit: int = 120) -> str:
    """The final question of the response, trimmed for the watch screen."""
    t = clean_markdown(content)
    cut = t.rfind("?", 0, len(t) - 1)  # start after any previous question
    tail = t[cut + 1:].strip() if cut != -1 else t
    return tail[-limit:] if len(tail) > limit else tail


def get_gemini_key(args) -> str:
    if getattr(args, "gemini_key", None):
        return args.gemini_key
    if os.environ.get("GEMINI_API_KEY"):
        return os.environ["GEMINI_API_KEY"]
    try:
        with open(GEMINI_KEY_FILE) as f:
            return f.read().strip()
    except OSError:
        return ""


def summarize(content: str, key: str) -> str:
    """One fast Gemini call: agent's final response -> watch-sized summary."""
    prompt = ("In 12 words or fewer, plain text, no markdown, summarize for "
              "a tiny smartwatch screen what the coding agent just "
              "accomplished:\n\n" + clean_markdown(content)[:6000])
    body = json.dumps({
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"maxOutputTokens": 40,
                             "thinkingConfig": {"thinkingBudget": 0}},
    }).encode()
    req = urllib.request.Request(
        f"https://generativelanguage.googleapis.com/v1beta/models/"
        f"{GEMINI_MODEL}:generateContent?key={key}",
        data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=8) as r:
        d = json.load(r)
    return d["candidates"][0]["content"]["parts"][0]["text"].strip()


# ----------------------------------------------------------------------- cli

def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("command", nargs="?", default="watch",
                   choices=["watch", "test", "set", "install-app", "setup"])
    p.add_argument("value", nargs="?", default=None,
                   help="state name for 'set' (idle|working|waiting|done)")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--name", default="Bangle.js",
                   help="BLE name prefix of the watch")
    p.add_argument("--settle", type=float, default=1.0,
                   help="seconds of silence before 'done' fires (default: 1)")
    p.add_argument("--summarize", action="store_true",
                   help="send a Gemini-generated summary as the done message "
                        "(needs a Gemini API key; off by default)")
    p.add_argument("--gemini-key", default=None,
                   help="Gemini API key (or $GEMINI_API_KEY, or tools/gemini.key)")
    p.add_argument("--idle-after", type=float, default=120.0,
                   help="seconds of silence before returning to idle")
    p.add_argument("--poll", type=float, default=0.5,
                   help="transcript poll interval (default: 0.5)")
    p.add_argument("--root", action="append", dest="roots")
    args = p.parse_args()

    if args.command == "test":
        send_state("done", "This is a test from agentwatch", args, fallback=True)
        return
    if args.command == "set":
        if args.value not in STATES:
            sys.exit(f"usage: agentwatch.py set <{'|'.join(STATES)}> ")
        send_state(args.value, "manual test", args)
        return
    if args.command == "install-app":
        install_app(args)
        return
    if args.command == "setup":
        ensure_daemon(args)
        install_app(args)
        time.sleep(8)
        send_state("idle", "", args)
        print("setup complete — run `python3 agentwatch.py` to go live")
        return

    if not args.dry_run:
        ensure_daemon(args)
        try:
            ensure_app(args)
        except SystemExit as e:
            print(f"note: auto-install skipped ({e}); states will flow once "
                  "the app is on the watch — run install-app to retry",
                  file=sys.stderr)
    roots = TRANSCRIPT_ROOTS + (args.roots or [])
    tracker = StateTracker(roots, settle=args.settle,
                           idle_after=args.idle_after)
    gemini_key = get_gemini_key(args) if args.summarize else ""
    if args.summarize and not gemini_key:
        sys.exit("--summarize needs a Gemini API key "
                 "(--gemini-key, $GEMINI_API_KEY, or tools/gemini.key)")
    print(f"watching for agent activity; settle={args.settle}s "
          f"{'+ summaries' if args.summarize else ''}"
          f"{'(dry run)' if args.dry_run else ''}")
    current = None
    current_msg = ""
    last_sent = 0.0

    def summarize_async(content):
        def bg():
            nonlocal current_msg
            try:
                summ = summarize(content, gemini_key)
            except Exception as e:
                print(f"summary failed: {e}", file=sys.stderr)
                return
            if current == "done":  # agent may have resumed meanwhile
                current_msg = summ
                send_state("done", summ, args)
        threading.Thread(target=bg, daemon=True).start()

    try:
        while True:
            state, content = tracker.poll()
            if state != current:
                current = state
                current_msg = ""
                if state == "attention" and content:
                    # show the actual question right away
                    current_msg = question_snippet(content)
                # buzz immediately; done-summaries follow silently
                send_state(state, current_msg, args)
                last_sent = time.time()
                if state == "done" and args.summarize and content:
                    summarize_async(content)
            elif time.time() - last_sent > 10:
                # silent re-sync so the watch converges after app restarts
                send_state(state, current_msg, args, quiet=True)
                last_sent = time.time()
            time.sleep(args.poll)
    except KeyboardInterrupt:
        print("\nbye")


if __name__ == "__main__":
    main()
