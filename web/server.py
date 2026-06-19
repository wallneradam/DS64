#!/usr/bin/env python3
"""DS64 web control panel (aiohttp; apt: python3-aiohttp).

Serves a small page to pair a controller and tune the bridge. It reads/writes
the shared config file (the joyd.py daemon live-reloads it) and reports the
daemon's status. The browser opens one Server-Sent Events stream (/api/events)
instead of polling, and the server probes the controller and the U64 only while
a client is connected -- with no open tab the appliance stays radio-quiet, which
keeps the shared Wi-Fi/Bluetooth radio from wedging the BT firmware. Run as root
(writes /etc/ds64, drives bluetoothctl).

  sudo python3 web/server.py            # listens on http://<pi>:8080
"""
import asyncio
import glob
import ipaddress
import json
import os
import re
import socket
import subprocess
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor

from aiohttp import web

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
    "ps_menu": True,
    "circle_left": True,
    "square_f1": True,
    "touchpad_mouse": True,
    "mouse_sensitivity_x": 0.15,
    "mouse_sensitivity_y": 0.2,
    "touchpad_two_finger_right": True,
    "mouse_invert_x": False,
    "mouse_invert_y": False,
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


# ---------------------------------------------------------------------------
# SSE event stream + shared state watcher
#
# The browser opens ONE long-lived /api/events stream instead of polling. A
# single watcher coroutine runs only while >=1 client is connected; it reads the
# daemon status file locally and probes the controller (bluetoothctl) and the
# U64 (Wi-Fi) on a shared timer, pushing to clients only when something changes.
# With no client connected nothing is probed, so the shared radio stays quiet.
# ---------------------------------------------------------------------------

STATUS_POLL = 0.25        # how often the watcher re-reads the local status file (s)
U64_INTERVAL = 12.0       # how often the U64 is probed over Wi-Fi, shared by all clients (s)
HEARTBEAT_SECS = 5.0      # idle SSE keepalive; also bounds how fast a gone client is noticed (s)

_UNSET = object()

CLIENTS = set()           # set[asyncio.Queue]: one queue per connected SSE client
LAST = {}                 # channel -> (signature, payload): last broadcast, for diff + replay
WATCHER = None            # asyncio.Task | None
NOTIFY = None             # asyncio.Event: set by commands to force an immediate re-check
EXECUTOR = None           # ThreadPoolExecutor for blocking helpers (bluetoothctl, urllib)
_evid = 0


async def off(fn, *args):
    """Run a blocking helper off the event loop so it never stalls the server."""
    return await asyncio.get_running_loop().run_in_executor(EXECUTOR, fn, *args)


def _status_payload_and_sig():
    """The /api/status body plus a change-signature that ignores the heartbeat
    timestamp, so an idle daemon rewriting status.json does not spam clients. The
    server computes `live` here (own clock vs the daemon's ts) instead of leaving
    it to the browser, so an idle-but-alive daemon needs no periodic push."""
    cfg = read_config()
    st = read_status()
    live = bool(st and (time.time() - st.get("ts", 0)) < 5)
    payload = {"config": cfg, "status": st, "live": live}
    sig = {"config": cfg, "live": live,
           "status": {k: v for k, v in st.items() if k != "ts"} if st else None}
    return payload, sig


def sse_frame(channel, obj):
    global _evid
    _evid += 1
    return ("id: %d\nevent: %s\ndata: %s\n\n"
            % (_evid, channel, json.dumps(obj))).encode()


def broadcast_if_changed(channel, payload, signature=None):
    """Queue `payload` to every client iff its signature changed since last time."""
    sig = payload if signature is None else signature
    prev = LAST.get(channel)
    if prev is not None and prev[0] == sig:
        return
    LAST[channel] = (sig, payload)
    frame = (channel, payload)
    for q in list(CLIENTS):
        try:
            q.put_nowait(frame)
        except asyncio.QueueFull:
            try:
                q.get_nowait()          # drop oldest, then keep the newest
            except asyncio.QueueEmpty:
                pass
            try:
                q.put_nowait(frame)
            except asyncio.QueueFull:
                pass


async def watcher_loop():
    """One shared poller, alive only while CLIENTS is non-empty. Status is a cheap
    local read every STATUS_POLL; bluetoothctl runs only when the connected
    controller changes (or a command forces it); the U64 is probed on its own slow
    Wi-Fi timer. Everything is pushed through broadcast_if_changed, so steady state
    sends nothing."""
    print("watcher: started -- probing while a client is connected", file=sys.stderr)
    loop = asyncio.get_running_loop()
    last_u64 = 0.0
    last_ctrl_key = _UNSET
    try:
        while CLIENTS:
            woken = NOTIFY.is_set()
            NOTIFY.clear()

            payload, sig = _status_payload_and_sig()
            broadcast_if_changed("status", payload, sig)

            ctrl_key = (payload["status"] or {}).get("controller")
            if woken or ctrl_key != last_ctrl_key:
                last_ctrl_key = ctrl_key
                try:
                    ctrl = await off(bonded_controller)
                except subprocess.TimeoutExpired:
                    ctrl = None
                broadcast_if_changed("controller", {"controller": ctrl})

            if loop.time() - last_u64 >= U64_INTERVAL:
                last_u64 = loop.time()
                broadcast_if_changed("u64", await off(u64_status))

            try:
                await asyncio.wait_for(NOTIFY.wait(), timeout=STATUS_POLL)
            except asyncio.TimeoutError:
                pass
    except asyncio.CancelledError:
        pass
    finally:
        LAST.clear()
        print("watcher: stopped -- no clients, radio quiet", file=sys.stderr)


def ensure_watcher():
    """Start the shared poller unless it is already running. The loop exits on its
    own once CLIENTS empties (it re-checks `while CLIENTS`), so a client reconnecting
    before the old loop has exited simply keeps it alive -- never two probers at once,
    which is the whole point of not cancelling it from the outside mid-probe."""
    global WATCHER
    if WATCHER is None or WATCHER.done():
        WATCHER = asyncio.ensure_future(watcher_loop())


async def sse_handler(request):
    resp = web.StreamResponse(status=200, headers={
        "Content-Type": "text/event-stream; charset=utf-8",
        "Cache-Control": "no-cache, no-transform",
        "X-Accel-Buffering": "no",
    })
    await resp.prepare(request)
    # If a client vanishes WITHOUT a clean close (laptop sleeps, Wi-Fi drops) there is
    # no FIN, so writes succeed into the void and the watcher would keep probing the
    # U64 for minutes (until TCP gives up). TCP_USER_TIMEOUT fails the socket ~30s after
    # our keepalive writes go unacknowledged, so the watcher goes quiet promptly.
    sock = request.transport.get_extra_info("socket") if request.transport else None
    if sock is not None:
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_USER_TIMEOUT, 30000)
        except (OSError, AttributeError):
            pass
    q = asyncio.Queue(maxsize=32)
    CLIENTS.add(q)
    ensure_watcher()
    try:
        for channel in ("status", "controller", "u64"):
            entry = LAST.get(channel)
            if entry is not None:
                await resp.write(sse_frame(channel, entry[1]))
        # A small keepalive write to a gone client does not reliably raise (aiohttp
        # buffers it), so poll the transport for the close as well; whichever fires
        # first ends the stream and lets the watcher go quiet.
        while not (request.transport is None or request.transport.is_closing()):
            try:
                channel, obj = await asyncio.wait_for(q.get(), timeout=HEARTBEAT_SECS)
                await resp.write(sse_frame(channel, obj))
            except asyncio.TimeoutError:
                await resp.write(b": keepalive\n\n")
    except (asyncio.CancelledError, ConnectionResetError, RuntimeError):
        pass
    finally:
        CLIENTS.discard(q)          # the watcher sees the empty set next tick and exits itself
    return resp


# ---------------------------------------------------------------------------
# HTTP API (GET reads kept for fallback/debugging; the browser uses /api/events)
# ---------------------------------------------------------------------------

async def serve_index(request):
    return web.FileResponse(os.path.join(HERE, "index.html"),
                            headers={"Content-Type": "text/html; charset=utf-8"})


async def get_config(request):
    return web.json_response(read_config())


async def get_status(request):
    payload, _ = _status_payload_and_sig()
    return web.json_response(payload)


async def get_controller(request):
    try:
        return web.json_response({"controller": await off(bonded_controller)})
    except subprocess.TimeoutExpired:
        return web.json_response({"controller": None, "message": "bluetoothctl timed out"})


async def get_u64(request):
    return web.json_response(await off(u64_status))


async def _json_body(request):
    raw = await request.read()
    return json.loads(raw or b"{}")


async def post_config(request):
    try:
        data = await _json_body(request)
    except ValueError:
        return web.json_response({"ok": False, "message": "bad JSON"}, status=400)
    cfg = read_config()
    if "port" in data and int(data["port"]) in (1, 2):
        cfg["port"] = int(data["port"])
    if data.get("mode") in ("auto", "manual"):
        cfg["mode"] = data["mode"]
    if "idle_timeout" in data:
        cfg["idle_timeout"] = max(0.5, min(300.0, float(data["idle_timeout"])))
    if "u64_host" in data and str(data["u64_host"]).strip():
        cfg["u64_host"] = str(data["u64_host"]).strip()
    for flag in ("ps_menu", "circle_left", "square_f1", "touchpad_mouse",
                 "touchpad_two_finger_right", "mouse_invert_x", "mouse_invert_y"):
        if flag in data:
            cfg[flag] = bool(data[flag])
    for axis in ("mouse_sensitivity_x", "mouse_sensitivity_y"):
        if axis in data:
            cfg[axis] = max(0.02, min(3.0, float(data[axis])))
    # The U64 hardwires a USB mouse to control port 1, so the 1351 mouse and
    # a port-1 joystick can't share it. Keep them mutually exclusive,
    # favouring whichever the user just changed: enabling the mouse frees
    # port 1 by moving the joystick to port 2; choosing port 1 for the
    # joystick turns the mouse off.
    if cfg.get("touchpad_mouse") and int(cfg.get("port", 2)) == 1:
        if "touchpad_mouse" in data and bool(data["touchpad_mouse"]):
            cfg["port"] = 2
        else:
            cfg["touchpad_mouse"] = False
    write_config(cfg)
    return web.json_response({"ok": True, "config": cfg})


async def post_pair(request):
    try:
        await _json_body(request)
    except ValueError:
        return web.json_response({"ok": False, "message": "bad JSON"}, status=400)
    try:
        res = await off(pair_controller)
    except subprocess.TimeoutExpired:
        return web.json_response({"ok": False, "message": "pairing timed out"}, status=504)
    if NOTIFY is not None:
        NOTIFY.set()
    return web.json_response(res)


async def post_forget(request):
    try:
        await _json_body(request)
    except ValueError:
        return web.json_response({"ok": False, "message": "bad JSON"}, status=400)
    try:
        res = await off(forget_controllers)
    except subprocess.TimeoutExpired:
        return web.json_response({"ok": False, "message": "forget timed out"}, status=504)
    if NOTIFY is not None:
        NOTIFY.set()
    return web.json_response(res)


async def post_disconnect(request):
    try:
        await _json_body(request)
    except ValueError:
        return web.json_response({"ok": False, "message": "bad JSON"}, status=400)
    try:
        res = await off(disconnect_controllers)
    except subprocess.TimeoutExpired:
        return web.json_response({"ok": False, "message": "disconnect timed out"}, status=504)
    if NOTIFY is not None:
        NOTIFY.set()
    return web.json_response(res)


async def post_detect_u64(request):
    try:
        await _json_body(request)
    except ValueError:
        return web.json_response({"ok": False, "message": "bad JSON"}, status=400)
    return web.json_response(await off(detect_u64))


async def on_startup(app):
    global NOTIFY, EXECUTOR
    NOTIFY = asyncio.Event()
    EXECUTOR = ThreadPoolExecutor(max_workers=4)


async def on_shutdown(app):
    global WATCHER
    if WATCHER is not None:
        WATCHER.cancel()
        WATCHER = None
    CLIENTS.clear()
    if EXECUTOR is not None:
        EXECUTOR.shutdown(wait=False)


def main():
    if not os.path.exists(CONFIG):
        write_config(dict(DEFAULTS))
    app = web.Application()
    app.on_startup.append(on_startup)
    app.on_shutdown.append(on_shutdown)
    app.add_routes([
        web.get("/", serve_index),
        web.get("/index.html", serve_index),
        web.get("/api/config", get_config),
        web.get("/api/status", get_status),
        web.get("/api/controller", get_controller),
        web.get("/api/u64", get_u64),
        web.get("/api/events", sse_handler),
        web.post("/api/config", post_config),
        web.post("/api/pair", post_pair),
        web.post("/api/forget", post_forget),
        web.post("/api/disconnect", post_disconnect),
        web.post("/api/detect_u64", post_detect_u64),
    ])
    print("DS64 web panel on http://0.0.0.0:%d" % LISTEN_PORT, file=sys.stderr)
    web.run_app(app, host="0.0.0.0", port=LISTEN_PORT,
                print=None, access_log=None, handle_signals=True)


if __name__ == "__main__":
    main()
