#!/usr/bin/env python3
"""DS64 web control panel (Python standard library only).

Serves a small page to pair a controller and tune the bridge. It reads/writes
the shared config file (the joyd.py daemon live-reloads it) and reports the
daemon's status. Run as root (writes /etc/ds64, drives bluetoothctl).

  sudo python3 web/server.py            # listens on http://<pi>:8080
"""
import glob
import ipaddress
import json
import os
import re
import subprocess
import sys
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

CONFIG = os.environ.get("DS64_CONFIG", "/etc/ds64/config.json")
STATUS = os.environ.get("DS64_STATUS", "/run/ds64/status.json")
LISTEN_PORT = int(os.environ.get("DS64_PORT", "8080"))
HERE = os.path.dirname(os.path.abspath(__file__))
PAIR_SCRIPT = os.path.join(os.path.dirname(HERE), "scripts", "pair-ds4.sh")

DEFAULTS = {
    "port": 2,
    "mode": "auto",
    "idle_timeout": 2.0,
    "u64_host": "192.168.5.64",
    "active_color": [0, 255, 0],
    "idle_color": [0, 0, 255],
    "triangle_menu": True,
    "circle_left": True,
}


def read_config():
    cfg = dict(DEFAULTS)
    try:
        with open(CONFIG) as f:
            cfg.update(json.load(f))
    except FileNotFoundError:
        pass
    return cfg


def write_config(cfg):
    os.makedirs(os.path.dirname(CONFIG), exist_ok=True)
    tmp = CONFIG + ".tmp"
    with open(tmp, "w") as f:
        json.dump(cfg, f, indent=2)
    os.replace(tmp, CONFIG)


def read_status():
    try:
        with open(STATUS) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


CONTROLLER_RE = r"Wireless Controller|DualShock|DualSense|DUALSHOCK|Sony"


def _bt(*args, timeout=15):
    return subprocess.run(["bluetoothctl", *args],
                          capture_output=True, text=True, timeout=timeout)


def _known_controllers():
    """All known devices that look like a PlayStation controller -> [(mac, name)]."""
    out = _bt("devices", timeout=10).stdout
    res = []
    for line in out.splitlines():
        m = re.match(r"Device (\S+)\s+(.+)", line.strip())
        if m and re.search(CONTROLLER_RE, m.group(2), re.I):
            res.append((m.group(1), m.group(2)))
    return res


def _info_flag(mac, flag):
    info = _bt("info", mac, timeout=10).stdout
    return bool(re.search(r"^\s*%s:\s*yes\b" % flag, info, re.I | re.M))


def _info_text(mac):
    """Contents of the on-disk BlueZ info file for a device, or '' if none."""
    for adapter in glob.glob("/var/lib/bluetooth/*/"):
        try:
            with open(os.path.join(adapter, mac, "info")) as f:
                return f.read()
        except OSError:
            continue
    return ""


def _is_persistent(mac):
    """A bond reconnects after a power-off only if BOTH are on disk: the link key
    (so encryption/auth works -- DS4 pairing leaves it un-persisted with store_hint=0
    unless we capture it) AND Trusted=true (so BlueZ auto-authorizes the HID input
    service without an agent; otherwise auth_callback denies every reconnect and the
    controller drops). bluetoothctl's 'Bonded: yes' only reflects in-RAM state."""
    info = _info_text(mac)
    return "[LinkKey]" in info and re.search(r"^Trusted=true", info, re.M) is not None


def bonded_controller():
    """The single known controller, if any: {mac, name, connected, persistent};
    else None. `persistent` says whether the bond survives a power-off."""
    for mac, name in _known_controllers():
        return {"mac": mac, "name": name,
                "connected": _info_flag(mac, "Connected"),
                "persistent": _is_persistent(mac)}
    return None


def forget_controllers():
    """Remove every known controller bond + cache, then flush to disk."""
    removed = [mac for mac, _ in _known_controllers()]
    for mac in removed:
        _bt("remove", mac, timeout=15)
    subprocess.run(["sync"], timeout=10)
    return {"ok": True, "removed": removed}


def disconnect_controllers():
    """Drop the active link to every known controller without touching the bond
    (so a PS press reconnects it). Useful to clear a stuck/zombie connection."""
    macs = [mac for mac, _ in _known_controllers()]
    for mac in macs:
        _bt("disconnect", mac, timeout=15)
    return {"ok": True, "disconnected": macs}


U64_VERSION_PATH = "/v1/version"   # GET -> {"version": ...}; confirms the Ultimate REST API
U64_INFO_PATH = "/v1/info"         # GET -> product/firmware/hostname for a friendly label


def _u64_probe(host, timeout=0.6):
    """Return (ok, info). `ok` is True if `host` answers the Ultimate REST API
    (GET /v1/version parses as JSON); `info` adds product/hostname from /v1/info."""
    host = (host or "").strip()
    if not host:
        return False, {}
    base = "http://%s" % host
    try:
        with urllib.request.urlopen(base + U64_VERSION_PATH, timeout=timeout) as r:
            json.loads(r.read().decode())
    except Exception:
        return False, {}
    info = {}
    try:
        with urllib.request.urlopen(base + U64_INFO_PATH, timeout=timeout) as r:
            info = json.loads(r.read().decode())
    except Exception:
        pass
    return True, info


def u64_status():
    """Reachability of the configured U64 address, for the web UI's live badge."""
    host = read_config().get("u64_host", "")
    ok, info = _u64_probe(host, timeout=1.5)
    return {"host": host, "reachable": ok,
            "product": info.get("product"), "hostname": info.get("hostname")}


def _local_ipv4s():
    """[(ip, prefixlen)] for each non-loopback IPv4 interface (via `ip -4 -o addr`)."""
    try:
        out = subprocess.run(["ip", "-4", "-o", "addr", "show"],
                             capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        out = ""
    res = []
    for line in out.splitlines():
        m = re.search(r"\binet (\d+\.\d+\.\d+\.\d+)/(\d+)\b", line)
        if m and not m.group(1).startswith("127."):
            res.append((m.group(1), int(m.group(2))))
    return res


def _scan_hosts(max_hosts=510):
    """Every host on each local subnet (never wider than a /24 around us), de-duped."""
    hosts, seen = [], set()
    for ip, prefix in _local_ipv4s():
        net = ipaddress.ip_network("%s/%d" % (ip, max(prefix, 24)), strict=False)
        for h in net.hosts():
            s = str(h)
            if s in seen:
                continue
            seen.add(s)
            hosts.append(s)
            if len(hosts) >= max_hosts:
                return hosts
    return hosts


def detect_u64():
    """Scan the local network for a C64 Ultimate (no mDNS in the firmware, so we
    probe /v1/version). Returns every match, the configured host probed first;
    the UI offers the first hit to fill in. Read-only -- safe to call anytime."""
    cur = read_config().get("u64_host", "").strip()
    candidates = ([cur] if cur else []) + [h for h in _scan_hosts() if h != cur]

    def probe(h):
        ok, info = _u64_probe(h, timeout=0.6)
        return {"host": h, "product": info.get("product"),
                "hostname": info.get("hostname")} if ok else None

    found = []
    if candidates:
        with ThreadPoolExecutor(max_workers=min(80, len(candidates))) as ex:
            for res in ex.map(probe, candidates):
                if res:
                    found.append(res)
    return {"found": found, "scanned": len(candidates)}


def pair_controller():
    """Run the durable-pairing script (single-controller, captures + persists the
    link key) and report the result. The verbose log is returned for the UI to show
    on demand; the short verdict comes from whether the bond is now on disk."""
    try:
        r = subprocess.run(["bash", PAIR_SCRIPT],
                           capture_output=True, text=True, timeout=120)
        log = (r.stdout or "").rstrip()
        if r.stderr:
            log += ("\n" + r.stderr.rstrip())
    except subprocess.TimeoutExpired:
        log = "pairing timed out"
    ctrl = bonded_controller()
    ok = bool(ctrl and ctrl.get("persistent"))
    return {"ok": ok, "controller": ctrl, "log": log.strip()}


class Handler(BaseHTTPRequestHandler):
    def _json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _file(self, path, ctype):
        try:
            with open(path, "rb") as f:
                body = f.read()
        except OSError:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            self._file(os.path.join(HERE, "index.html"), "text/html; charset=utf-8")
        elif self.path == "/api/config":
            self._json(read_config())
        elif self.path == "/api/status":
            self._json({"config": read_config(), "status": read_status()})
        elif self.path == "/api/controller":
            try:
                self._json({"controller": bonded_controller()})
            except subprocess.TimeoutExpired:
                self._json({"controller": None, "message": "bluetoothctl timed out"})
        elif self.path == "/api/u64":
            self._json(u64_status())
        else:
            self.send_error(404)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            data = json.loads(raw or b"{}")
        except ValueError:
            self._json({"ok": False, "message": "bad JSON"}, 400)
            return

        if self.path == "/api/config":
            cfg = read_config()
            if "port" in data and int(data["port"]) in (1, 2):
                cfg["port"] = int(data["port"])
            if data.get("mode") in ("auto", "manual"):
                cfg["mode"] = data["mode"]
            if "idle_timeout" in data:
                cfg["idle_timeout"] = max(0.5, min(300.0, float(data["idle_timeout"])))
            if "u64_host" in data and str(data["u64_host"]).strip():
                cfg["u64_host"] = str(data["u64_host"]).strip()
            for flag in ("triangle_menu", "circle_left"):
                if flag in data:
                    cfg[flag] = bool(data[flag])
            write_config(cfg)
            self._json({"ok": True, "config": cfg})
        elif self.path == "/api/pair":
            try:
                self._json(pair_controller())
            except subprocess.TimeoutExpired:
                self._json({"ok": False, "message": "pairing timed out"}, 504)
        elif self.path == "/api/forget":
            try:
                self._json(forget_controllers())
            except subprocess.TimeoutExpired:
                self._json({"ok": False, "message": "forget timed out"}, 504)
        elif self.path == "/api/disconnect":
            try:
                self._json(disconnect_controllers())
            except subprocess.TimeoutExpired:
                self._json({"ok": False, "message": "disconnect timed out"}, 504)
        elif self.path == "/api/detect_u64":
            self._json(detect_u64())
        else:
            self.send_error(404)

    def log_message(self, *_):
        pass  # quiet


def main():
    if not os.path.exists(CONFIG):
        write_config(dict(DEFAULTS))
    httpd = ThreadingHTTPServer(("0.0.0.0", LISTEN_PORT), Handler)
    print("DS64 web panel on http://0.0.0.0:%d" % LISTEN_PORT, file=sys.stderr)
    httpd.serve_forever()


if __name__ == "__main__":
    main()
