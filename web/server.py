#!/usr/bin/env python3
"""DS64 web control panel (aiohttp; apt: python3-aiohttp).

Serves a small page to pair a controller and tune the bridge. It reads/writes
the shared config file (the joyd.py daemon live-reloads it) and reports the
daemon's status. The browser opens one Server-Sent Events stream (/api/events)
instead of polling, and the server probes the controller and the U64 only while
a client is connected -- with no open tab the appliance stays radio-quiet, which
keeps the shared Wi-Fi/Bluetooth radio from wedging the BT firmware. Run as root
(writes /etc/ds64, drives bluetoothctl).

  sudo python3 web/server.py            # listens on http://<pi>/  (port 80)
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
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

from aiohttp import web

CONFIG = os.environ.get("DS64_CONFIG", "/etc/ds64/config.json")
STATUS = os.environ.get("DS64_STATUS", "/run/ds64/status.json")
LISTEN_PORT = int(os.environ.get("DS64_PORT", "80"))
HERE = os.path.dirname(os.path.abspath(__file__))
PAIR_SCRIPT = os.path.join(os.path.dirname(HERE), "scripts", "pair-ds4.sh")

DEFAULTS = {
    "port": 2,
    "mode": "auto",
    "idle_timeout": 2.0,
    "u64_host": "192.168.5.64",
    "u64_password": "",
    "active_color": [0, 255, 0],
    "idle_color": [0, 0, 255],
    "ps_menu": True,
    "circle_left": True,
    "options_f1": True,
    "share_swap": True,
    "touchpad_mouse": True,
    "mouse_sensitivity_x": 0.15,
    "mouse_sensitivity_y": 0.2,
    "touchpad_two_finger_right": True,
    "mouse_invert_x": False,
    "mouse_invert_y": False,
    "ext_mouse_sensitivity": 1.0,
    "ext_mouse_with_touchpad": False,
    "ntp_enabled": False,
    "ntp_offset_minutes": 0,
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


def _u64_headers(password):
    """Auth header for the Ultimate REST API. Once a Network Password is set on the
    U64, the firmware rejects EVERY call (even GET /v1/version) with HTTP 403 unless
    it carries an `X-Password` header (firmware api/routes.h), so attach it whenever
    the user has configured one. Empty -> no header (the firmware skips the check)."""
    return {"X-Password": password} if password else {}


def _u64_locked_403(err):
    """True if `err` is the Ultimate's HTTP 403 -- a Network Password is set and ours
    is missing or wrong. Identified by the firmware's `{"errors":[...]}` JSON body,
    which is positive proof an Ultimate lives here even though we are not authorized."""
    if getattr(err, "code", None) != 403:
        return False
    try:
        obj = json.loads(err.read().decode())
    except Exception:
        return False
    return isinstance(obj, dict) and "errors" in obj


def _u64_probe(host, timeout=0.6, password=""):
    """Identify the Ultimate at `host`. Returns (state, info):
      "ok"     -- answers the REST API (GET /v1/version parses as JSON); `info` adds
                  product/hostname from /v1/info.
      "locked" -- an Ultimate is here but a Network Password is set and ours is
                  missing/wrong (HTTP 403 + the firmware's JSON error body). Enough to
                  fill in the address and prompt for the password; `info` is {}.
      "no"     -- not an Ultimate, or unreachable; `info` is {}."""
    host = (host or "").strip()
    if not host:
        return "no", {}
    base = "http://%s" % host
    headers = _u64_headers(password)
    try:
        req = urllib.request.Request(base + U64_VERSION_PATH, headers=headers)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return ("locked", {}) if _u64_locked_403(e) else ("no", {})
    except Exception:
        return "no", {}
    info = {}
    try:
        req = urllib.request.Request(base + U64_INFO_PATH, headers=headers)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            info = json.loads(r.read().decode())
    except Exception:
        pass
    return "ok", info


# Once the host is HTTP-confirmed to be an Ultimate, ongoing reachability is checked
# with a single ICMP echo instead of a full REST GET -- far less work for both the Pi
# and the Ultimate's lwIP stack on every 12s probe. The HTTP identity check re-runs
# only when the configured host changes or after the U64 has gone unreachable, so a
# reboot is re-confirmed once before we trust ping again (and a fresh page load re-runs
# detection anyway).
_U64_CACHE = {"host": None, "password": None, "info": None}   # last HTTP-confirmed identity


def _ping(host, timeout=1):
    """True if `host` answers one ICMP echo within `timeout` seconds."""
    host = (host or "").strip()
    if not host:
        return False
    try:
        return subprocess.run(
            ["ping", "-c", "1", "-W", str(timeout), host],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=timeout + 1).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _u64_result(host, reachable, info, locked=False):
    info = info or {}
    return {"host": host, "reachable": reachable, "locked": locked,
            "product": info.get("product") if reachable else None,
            "hostname": info.get("hostname") if reachable else None}


def u64_status():
    """Reachability of the configured U64 address, for the web UI's live badge.
    Confirms it really is an Ultimate over HTTP once, then keeps checking with a
    cheap ping; see the _U64_CACHE note above."""
    cfg = read_config()
    host = cfg.get("u64_host", "").strip()
    password = cfg.get("u64_password", "")
    if not host:
        _U64_CACHE.update(host=None, password=None, info=None)
        return _u64_result("", False, None)

    # The cache is keyed on (host, password): changing either re-runs the HTTP probe,
    # so clearing/altering the password can't keep ping-claiming "connected" while the
    # REST API is actually 403-locked (ICMP needs no password).
    if (_U64_CACHE["host"] == host and _U64_CACHE["password"] == password
            and _U64_CACHE["info"] is not None):
        if _ping(host):
            return _u64_result(host, True, _U64_CACHE["info"])
        # Gone -- drop the identity so a recovery re-confirms it is still a U64.
        _U64_CACHE.update(host=None, password=None, info=None)
        return _u64_result(host, False, None)

    # Not yet identified (first contact, host/password changed, or recovering): a quick
    # ping gates the HTTP probe so a dead address costs one ping, not a REST timeout.
    if not _ping(host):
        return _u64_result(host, False, None)
    state, info = _u64_probe(host, timeout=1.5, password=password)
    if state == "ok":
        _U64_CACHE.update(host=host, password=password, info=info)
        return _u64_result(host, True, info)
    # Not usable -- drop any stale identity so the cheap-ping path can't resurrect it.
    _U64_CACHE.update(host=None, password=None, info=None)
    return _u64_result(host, False, None, locked=(state == "locked"))


U64_NTP_EN_PATH = "/v1/configs/Network%20Settings/SNTP%20Enable"  # RAM-only PUT


def _u64_force_ntp_resync(host, password="", timeout=2.0):
    """Make the Ultimate re-read the time NOW by toggling its 'SNTP Enable' off
    then on over the REST config API: the firmware re-runs start_sntp() only on an
    actual value change, so a plain re-set is a no-op -- hence the off->on flip.
    RAM-only (no flash write); best-effort -- an unreachable U64 is logged."""
    host = (host or "").strip()
    if not host:
        return False
    headers = _u64_headers(password)
    try:
        for value in ("Disabled", "Enabled"):
            url = "http://%s%s?value=%s" % (host, U64_NTP_EN_PATH,
                                            urllib.parse.quote(value))
            req = urllib.request.Request(url, method="PUT", headers=headers)
            urllib.request.urlopen(req, timeout=timeout).read()
        return True
    except Exception as ex:
        print("U64 NTP resync failed:", ex, file=sys.stderr)
        return False


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


def primary_ip():
    """The Pi's first non-loopback IPv4, for the UI to show as the NTP server
    address the user types into the C64 (mDNS is absent, so a numeric IP is safest)."""
    ips = _local_ipv4s()
    return ips[0][0] if ips else ""


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
    cfg = read_config()
    cur = cfg.get("u64_host", "").strip()
    password = cfg.get("u64_password", "")
    candidates = ([cur] if cur else []) + [h for h in _scan_hosts() if h != cur]

    def probe(h):
        state, info = _u64_probe(h, timeout=0.6, password=password)
        if state == "no":
            return None
        return {"host": h, "locked": state == "locked",
                "product": info.get("product"), "hostname": info.get("hostname")}

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
# Update check -- is the deployed code behind its git remote?
#
# The appliance is a git checkout (/opt/ds64) updated with `ds64-update`
# (git reset --hard origin/<branch>). We surface a Pi-hole-style "update
# available" hint in the web footer by comparing the local HEAD with the remote
# tip of the tracked branch. `git ls-remote` reads only the ref advertisement --
# no objects fetched, no local repo writes -- so the check is cheap and
# side-effect-free. It runs on a slow shared timer, only while a client is
# connected (like every other probe here), so an idle appliance stays quiet.
# ---------------------------------------------------------------------------

REPO_DIR = os.path.dirname(HERE)
UPDATE_BRANCH = os.environ.get("DS64_UPDATE_BRANCH", "").strip()  # "" = whatever HEAD tracks
UPDATE_INTERVAL = 3600.0   # seconds between remote checks (the first runs on connect)
LAST_UPDATE_CHECK = -1e9   # loop.time() of the last ls-remote; survives watcher restarts


def _git(*args, timeout=15):
    """git in the deploy dir, never blocking on a credential prompt, and immune to
    the 'dubious ownership' refusal (root vs the bind-mounted repo's owner)."""
    return subprocess.run(
        ["git", "-C", REPO_DIR, "-c", "safe.directory=" + REPO_DIR, *args],
        capture_output=True, text=True, timeout=timeout,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"})


def _git_out(*args, timeout=15):
    """stdout of a git command, stripped, or '' on any failure (non-git dir,
    missing git, timeout, network error)."""
    try:
        r = _git(*args, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return ""
    return r.stdout.strip() if r.returncode == 0 else ""


def update_status():
    """Whether the deployed checkout is behind its remote: {available, branch,
    local, remote}. `available` is True when the remote tip differs from HEAD
    (the production Pi resets HEAD hard to the remote, so 'differs' == 'behind').
    Returns {"available": False} for a non-git deploy or an unreachable remote."""
    local = _git_out("rev-parse", "HEAD", timeout=5)
    if not local:
        return {"available": False}
    branch = UPDATE_BRANCH or _git_out("rev-parse", "--abbrev-ref", "HEAD", timeout=5)
    if not branch or branch == "HEAD":
        branch = "main"
    remote = ""
    for line in _git_out("ls-remote", "origin", branch, timeout=20).splitlines():
        sha, _, ref = line.partition("\t")
        if ref.strip() == "refs/heads/" + branch:
            remote = sha.strip()
            break
    if not remote:
        return {"available": False, "branch": branch, "local": local[:7]}
    return {"available": remote != local, "branch": branch,
            "local": local[:7], "remote": remote[:7]}


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
    global LAST_UPDATE_CHECK
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

            # Re-check on the hourly timer, but ALWAYS run once when there is no
            # cached result yet -- otherwise a brief earlier client could have set
            # the throttle timestamp while its cached payload was later dropped,
            # leaving a fresh client with no "update" frame for up to an hour.
            if "update" not in LAST or loop.time() - LAST_UPDATE_CHECK >= UPDATE_INTERVAL:
                LAST_UPDATE_CHECK = loop.time()
                broadcast_if_changed("update", await off(update_status))

            try:
                await asyncio.wait_for(NOTIFY.wait(), timeout=STATUS_POLL)
            except asyncio.TimeoutError:
                pass
    except asyncio.CancelledError:
        pass
    finally:
        # Keep the "update" result cached across watcher restarts so a reconnecting
        # client gets the pill replayed immediately (the throttle would otherwise
        # skip a fresh check). The fast-changing channels are dropped and re-probed.
        for ch in ("status", "controller", "u64"):
            LAST.pop(ch, None)
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
        for channel in ("status", "controller", "u64", "update"):
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


async def get_update(request):
    return web.json_response(await off(update_status))


async def get_netinfo(request):
    """The Pi's IP (for the NTP card to show) plus its current UTC, so the UI can
    display the Pi's own clock -- the base the NTP server serves from -- as a
    health check."""
    return web.json_response({"ip": await off(primary_ip), "utc": time.time()})


async def _json_body(request):
    raw = await request.read()
    return json.loads(raw or b"{}")


async def post_config(request):
    try:
        data = await _json_body(request)
    except ValueError:
        return web.json_response({"ok": False, "message": "bad JSON"}, status=400)
    cfg = read_config()
    old_offset = cfg.get("ntp_offset_minutes")
    old_enabled = cfg.get("ntp_enabled")
    if "port" in data and int(data["port"]) in (1, 2):
        cfg["port"] = int(data["port"])
    if data.get("mode") in ("auto", "manual"):
        cfg["mode"] = data["mode"]
    if "idle_timeout" in data:
        cfg["idle_timeout"] = max(0.5, min(300.0, float(data["idle_timeout"])))
    if "u64_host" in data and str(data["u64_host"]).strip():
        cfg["u64_host"] = str(data["u64_host"]).strip()
    # Network Password for the U64 REST API (X-Password header). Not stripped --
    # spaces are legal password characters; "" disables it. The firmware caps it
    # at 31 chars (CFG_NETWORK_PASSWORD).
    if "u64_password" in data:
        cfg["u64_password"] = str(data["u64_password"])[:31]
    for flag in ("ps_menu", "circle_left", "options_f1", "share_swap", "touchpad_mouse",
                 "touchpad_two_finger_right", "mouse_invert_x", "mouse_invert_y",
                 "ext_mouse_with_touchpad", "ntp_enabled"):
        if flag in data:
            cfg[flag] = bool(data[flag])
    for axis in ("mouse_sensitivity_x", "mouse_sensitivity_y", "ext_mouse_sensitivity"):
        if axis in data:
            cfg[axis] = max(0.02, min(3.0, float(data[axis])))
    if "ntp_offset_minutes" in data:
        cfg["ntp_offset_minutes"] = max(-1440, min(1440, int(round(float(data["ntp_offset_minutes"])))))
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
    # A changed offset, or newly enabling our server, only reaches the C64 on its
    # next (hourly) SNTP poll. When our NTP server is on, force an immediate
    # re-read by toggling the Ultimate's 'SNTP Enable' off->on over REST so the
    # firmware re-runs start_sntp() now.
    offset_changed = "ntp_offset_minutes" in data and cfg["ntp_offset_minutes"] != old_offset
    just_enabled = bool(cfg.get("ntp_enabled")) and not old_enabled
    ntp_resynced = None
    if cfg.get("ntp_enabled") and (offset_changed or just_enabled):
        ntp_resynced = await off(_u64_force_ntp_resync, cfg.get("u64_host", ""),
                                 cfg.get("u64_password", ""))
    return web.json_response({"ok": True, "config": cfg, "ntp_resynced": ntp_resynced})


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
        web.get("/api/update", get_update),
        web.get("/api/netinfo", get_netinfo),
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
