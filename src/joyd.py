#!/usr/bin/env python3
"""DS64 bridge: a game controller as a Commodore 64 Ultimate joystick.

Reads a Bluetooth/USB game controller and drives the U64 over a USB HID keyboard
gadget (/dev/hidg0). With the U64 'Joystick Swapper' set to a WASD mode, the
keystrokes W/A/S/D/RETURN become a real joystick.

The controller's touchpad becomes a Commodore 1351 proportional mouse. The
gadget exposes two independent HID interfaces on one USB device: a boot keyboard
(/dev/hidg0) and a mouse (/dev/hidg1). The U64 host installs a driver and an
interrupt pipe per interface and polls each independently, emulating the mouse
as a 1351 on control port 1 -- so one controller is both a joystick (port 2)
and a mouse (port 1) at the same time.

A real USB mouse plugged into the Pi is forwarded through the SAME hidg1, so the
U64 still sees only one device (our gadget) -- a second separate device on the
U64's own USB host corrupts the keyboard pipe and drops the held joystick, but a
mouse hanging off the Pi cannot. When a USB mouse is present it takes over the
1351 and the touchpad steps aside; `ext_mouse_with_touchpad` (the square button /
the web panel) lets both drive the cursor at once. Left/right/middle buttons are
all forwarded.

Directions: either analog stick OR the D-pad. Fire: X / L1 / R1 / L2 / R2.
Optional extra buttons (each toggled in the config):
  PS button -> toggle the U64 menu on release (via the menu_button API)
  circle    -> Left (the "back" direction inside the U64 menu)
  options   -> F1 (a C64 function key, sent over the keyboard gadget)
  share     -> swap the joystick between control port 1 and 2 (writes the config)
  square    -> toggle "use the touchpad alongside a USB mouse" (writes the config)

Modes (from the shared config file, live-reloaded):
  auto   - any real input switches the U64 into WASD mode; after `idle_timeout`
           seconds with no input it switches back to Normal (so W/A/S/D are free
           to type again). The triangle button toggles it; after a triangle-off
           any input turns it straight back on.
  manual - the triangle button toggles WASD mode on/off; input alone never
           switches it.

The controller lightbar shows the state: green while WASD is active, blue idle.
"""
import errno
import glob
import json
import os
import queue
import sys
import threading
import time
import selectors
import urllib.parse
import urllib.request

import evdev
from evdev import ecodes as E

HIDG_KBD = "/dev/hidg0"     # boot-keyboard interface (8-byte reports, no report ID)
HIDG_MOUSE = "/dev/hidg1"   # mouse interface (3-byte reports, no report ID)
CONFIG = os.environ.get("DS64_CONFIG", "/etc/ds64/config.json")
STATUS = os.environ.get("DS64_STATUS", "/run/ds64/status.json")

W, A, S, D, RET = 0x1a, 0x04, 0x16, 0x07, 0x28
F1 = 0x3a   # USB HID keycode for F1 -> the firmware maps it to the C64 F1 key
DEAD = 64   # analog stick distance from center (128) to count as a direction
TRIG = 64   # L2/R2 analog trigger threshold (rest 0 .. full 255)
TICK = 0.15      # seconds between idle/config checks
HEARTBEAT = 2.0  # refresh status.json at least this often, so the web UI sees us alive
BATT_INTERVAL = 10.0  # re-read the controller battery sysfs at most this often (changes slowly)
MENU_DEBOUNCE = 0.4  # ignore PS->menu presses this close together (no double-toggle)

FIRE_BTNS = {E.BTN_SOUTH, E.BTN_TL, E.BTN_TR, E.BTN_TL2, E.BTN_TR2}  # X, L1, R1, L2, R2

# hidg0 write errno values that mean the C64 USB host is absent (off / not yet
# enumerated) rather than a real fault -- the bridge must survive these so it
# keeps reading the controller and updating the heartbeat until the C64 returns.
HID_HOST_ABSENT = (errno.ESHUTDOWN, errno.ENODEV, errno.EIO)

DEFAULTS = {
    "port": 2,                # 1 or 2 (which control port the joystick appears on)
    "mode": "auto",           # "auto" | "manual"
    "idle_timeout": 2.0,      # seconds of no input before auto switches back to Normal
    "u64_host": "192.168.5.64",
    "active_color": [0, 255, 0],   # lightbar while WASD active (green)
    "idle_color": [0, 0, 255],     # lightbar while idle/normal (blue)
    "ps_menu": True,               # PS button -> toggle the U64 menu (menu_button API)
    "circle_left": True,           # circle -> Left (back in the U64 menu)
    "options_f1": True,            # options -> F1 (a C64 function key)
    "share_swap": True,            # share -> swap the joystick port 1 <-> 2
    "touchpad_mouse": True,             # touchpad -> Commodore 1351 mouse (port 1)
    "mouse_sensitivity_x": 0.15,        # horizontal scale on the raw touchpad deltas
    "mouse_sensitivity_y": 0.2,         # vertical scale (the pad is short in Y -> a touch faster)
    "touchpad_two_finger_right": True,  # two fingers -> right mouse button
    "mouse_invert_x": False,            # flip horizontal motion if it feels reversed
    "mouse_invert_y": False,            # flip vertical motion if it feels reversed
    "ext_mouse_sensitivity": 1.0,       # scale on a USB mouse's relative deltas (one value, no X/Y split)
    "ext_mouse_with_touchpad": False,   # also drive the 1351 from the touchpad while a USB mouse is on
}

SWAPPER_PATH = "/v1/configs/U64%20Specific%20Settings/Joystick%20Swapper"


def load_config():
    cfg = dict(DEFAULTS)
    try:
        with open(CONFIG) as f:
            cfg.update(json.load(f))
    except FileNotFoundError:
        pass
    except Exception as ex:
        print("config read error:", ex, file=sys.stderr)
    return cfg


def write_config(cfg):
    """Atomically persist the shared config the same way (path + format) the web
    panel does, so a daemon-side change is indistinguishable from a web edit."""
    os.makedirs(os.path.dirname(CONFIG), exist_ok=True)
    tmp = CONFIG + ".tmp"
    with open(tmp, "w") as f:
        json.dump(cfg, f, indent=2)
    os.replace(tmp, CONFIG)


def find_controller():
    for path in evdev.list_devices():
        dev = evdev.InputDevice(path)
        keys = dev.capabilities().get(E.EV_KEY, [])
        if E.BTN_SOUTH in keys or E.BTN_GAMEPAD in keys:
            return dev
    return None


def find_leds():
    """Locate the controller's lightbar LED class devices, if any.

    Two kernel layouts exist:
      * DualShock 4 (hid-playstation): separate ``*:red``/``*:green``/``*:blue``
        channels plus a ``*:global`` master brightness.
      * DualSense (hid-playstation): a single multicolor LED ``*:rgb:indicator``
        with a ``multi_intensity`` ("R G B") file and a master ``brightness``.
    """
    for dev in glob.glob("/sys/class/leds/*:rgb:*"):
        return {
            "kind": "multi",
            "intensity": dev + "/multi_intensity",
            "brightness": dev + "/brightness",
        }
    for red in glob.glob("/sys/class/leds/*:red"):
        base = red.rsplit(":", 1)[0]
        return {
            "kind": "channels",
            "red": base + ":red/brightness",
            "green": base + ":green/brightness",
            "blue": base + ":blue/brightness",
            "global": base + ":global/brightness",
        }
    return None


def find_battery(dev):
    """Locate the controller's battery as a sysfs power_supply node, or None.

    The hid-playstation driver (DS4 and DS5 alike) exposes the pad battery as
    ``/sys/class/power_supply/ps-controller-battery-<mac>``, where ``<mac>``
    matches the evdev device's ``uniq``. Prefer that exact node; fall back to
    any controller battery (covers the older hid-sony naming) if the uniq is
    unavailable or does not match.
    """
    mac = (dev.uniq or "").lower()
    if mac:
        exact = "/sys/class/power_supply/ps-controller-battery-" + mac
        if os.path.isdir(exact):
            return exact
    for pat in ("ps-controller-battery-*", "sony_controller_battery_*"):
        for path in glob.glob("/sys/class/power_supply/" + pat):
            return path
    return None


def find_touchpad():
    """Locate the controller's touchpad as a separate evdev node.

    The hid-playstation driver exposes the DS4/DS5 touchpad as its own input
    device (its name ends in "Touchpad"), distinct from the gamepad node. It uses
    multitouch protocol B (ABS_MT_SLOT/TRACKING_ID/POSITION_X/Y) plus BTN_LEFT
    for the physical pad click.
    """
    for path in evdev.list_devices():
        try:
            dev = evdev.InputDevice(path)
        except OSError:
            continue
        if "touchpad" not in dev.name.lower():
            continue
        abs_codes = [c for c, _ in dev.capabilities().get(E.EV_ABS, [])]
        if E.ABS_MT_POSITION_X in abs_codes:
            return dev
    return None


def find_mouse():
    """Locate an external relative-pointing mouse plugged into the Pi, or None.

    A real USB mouse reports relative motion (EV_REL REL_X/REL_Y) plus BTN_LEFT;
    the DS4/DS5 touchpad is absolute (EV_ABS/ABS_MT, no REL_X) so it is matched by
    find_touchpad() instead and skipped here. Non-matching nodes are closed so a
    periodic rescan does not leak file descriptors."""
    for path in evdev.list_devices():
        try:
            dev = evdev.InputDevice(path)
        except OSError:
            continue
        caps = dev.capabilities()
        rel = caps.get(E.EV_REL, [])
        keys = caps.get(E.EV_KEY, [])
        if (E.REL_X in rel and E.REL_Y in rel and E.BTN_LEFT in keys
                and "touchpad" not in dev.name.lower()):
            return dev
        dev.close()
    return None


class Touchpad:
    """Turn a DS4/DS5 touchpad evdev stream into relative mouse motion + buttons.

    Multitouch protocol B: ABS_MT_SLOT selects a contact slot, ABS_MT_TRACKING_ID
    >= 0 starts a contact (-1 ends it), ABS_MT_POSITION_X/Y give absolute coords.
    The primary (lowest active) slot drives motion as the delta between successive
    positions; lifting a finger drops its saved position so re-touching never
    jumps. A second finger raises the right button; BTN_LEFT (the physical pad
    click) is the left button.
    """

    def __init__(self, dev):
        self.dev = dev
        self._resyncing = False
        self._reset_state()

    def _reset_state(self):
        self.slot = 0
        self.tid = {}     # slot -> tracking id, present only while the finger is down
        self.pos = {}     # slot -> [x, y] latest absolute position
        self.last = {}    # slot -> [x, y] previous position, for the delta
        self.left = False

    def read(self):
        """Drain pending events; return (dx, dy, left, right) for this batch."""
        dx = dy = 0
        for e in self.dev.read():
            if e.type == E.EV_SYN and e.code == E.SYN_DROPPED:
                # The kernel's evdev buffer overflowed and silently discarded
                # events. Our per-slot finger bookkeeping is now unreliable: a lost
                # lift (ABS_MT_TRACKING_ID == -1) leaves a phantom contact, and as
                # the kernel reuses slots the finger count drifts -- a one-finger
                # touch reads as two (right button) while a two-finger touch can
                # read as one (left), i.e. the buttons appear swapped. Ignore the
                # post-drop burst up to the next SYN_REPORT, then start clean; the
                # next real touch rebuilds correct state.
                self._resyncing = True
                continue
            if self._resyncing:
                if e.type == E.EV_SYN and e.code == E.SYN_REPORT:
                    self._resyncing = False
                    self._reset_state()
                continue
            if e.type == E.EV_ABS:
                if e.code == E.ABS_MT_SLOT:
                    self.slot = e.value
                elif e.code == E.ABS_MT_TRACKING_ID:
                    if e.value < 0:
                        self.tid.pop(self.slot, None)
                        self.pos.pop(self.slot, None)
                        self.last.pop(self.slot, None)
                    else:
                        self.tid[self.slot] = e.value
                        self.last.pop(self.slot, None)   # first sample is a baseline
                elif e.code == E.ABS_MT_POSITION_X:
                    self.pos.setdefault(self.slot, [0, 0])[0] = e.value
                elif e.code == E.ABS_MT_POSITION_Y:
                    self.pos.setdefault(self.slot, [0, 0])[1] = e.value
            elif e.type == E.EV_KEY and e.code == E.BTN_LEFT:
                self.left = bool(e.value)
            elif e.type == E.EV_SYN and e.code == E.SYN_REPORT:
                ddx, ddy = self._primary_delta()
                dx += ddx
                dy += ddy
        return dx, dy, self.left, len(self.tid) >= 2

    def _primary_delta(self):
        if not self.tid:
            return 0, 0
        slot = min(self.tid)             # primary contact = lowest active slot
        cur = self.pos.get(slot)
        if cur is None:
            return 0, 0
        prev = self.last.get(slot)
        self.last[slot] = list(cur)
        if prev is None:
            return 0, 0                  # baseline sample, no jump on (re)touch
        return cur[0] - prev[0], cur[1] - prev[1]


class ExtMouse:
    """An external USB mouse plugged into the Pi, read as relative motion + buttons.

    Symmetric with Touchpad: read() drains pending evdev events and returns the
    accumulated (dx, dy, buttons) for this batch; held buttons persist across
    reads in self.buttons. The wrapped evdev device is reachable as self.dev for
    the selector (fileno) and cleanup (close)."""

    def __init__(self, dev):
        self.dev = dev
        self.buttons = 0

    def read(self):
        dx = dy = 0
        for e in self.dev.read():
            if e.type == E.EV_REL:
                if e.code == E.REL_X:
                    dx += e.value
                elif e.code == E.REL_Y:
                    dy += e.value
            elif e.type == E.EV_KEY:
                bit = {E.BTN_LEFT: 0x01, E.BTN_RIGHT: 0x02,
                       E.BTN_MIDDLE: 0x04}.get(e.code)
                if bit is not None:
                    if e.value:
                        self.buttons |= bit
                    else:
                        self.buttons &= ~bit
        return dx, dy, self.buttons


class MouseForwarder:
    """The hidg1 1351-mouse gadget endpoint plus the relative-motion accumulator.

    Both the controller touchpad and an external USB mouse feed it through emit();
    it is also driven standalone (no controller) so a USB mouse on the Pi keeps
    working independently of any Bluetooth controller. O_NONBLOCK: a write can
    never freeze the caller if the C64 stops draining the endpoint -- a dropped
    report is simply discarded (mouse motion is fire-and-forget) and the fd is
    reopened at most once a second when the host is absent."""

    def __init__(self):
        self.fd = None
        self.accum_x = 0.0           # fractional carry, so slow drags are not lost
        self.accum_y = 0.0
        self.last_buttons = 0
        self.last_reopen = 0.0
        try:
            self.fd = os.open(HIDG_MOUSE, os.O_WRONLY | os.O_NONBLOCK)
        except OSError as ex:
            print("mouse gadget open failed:", ex, file=sys.stderr)

    def emit(self, dx, dy, buttons, sx, sy):
        self.accum_x += dx * sx
        self.accum_y += dy * sy
        mx = max(-127, min(127, int(self.accum_x)))
        my = max(-127, min(127, int(self.accum_y)))
        # Relative deltas must not be deduped (two identical moves are two moves),
        # but skip empty reports: nothing moved and no button changed.
        if mx == 0 and my == 0 and buttons == self.last_buttons:
            return
        # 3-byte mouse payload: buttons, relative dx, relative dy (no report ID).
        # Commit the accumulator and button state only after the report actually
        # went out, so a dropped (host-not-reading) report retries on the next
        # event instead of losing motion.
        if self._write(bytes([buttons, mx & 0xFF, my & 0xFF])):
            self.accum_x -= mx
            self.accum_y -= my
            self.last_buttons = buttons

    def off(self):
        """Mouse switched off / source handover: drop any carried motion and
        release a held button once, so the C64 never sees a stuck 1351 button."""
        self.accum_x = self.accum_y = 0.0
        if self.last_buttons and self._write(bytes(3)):
            self.last_buttons = 0

    def _write(self, report):
        if self.fd is None:
            self._reopen()          # gadget may have appeared after we started
            if self.fd is None:
                return False
        try:
            os.write(self.fd, report)
            return True
        except BlockingIOError:
            return False
        except OSError as ex:
            if ex.errno not in HID_HOST_ABSENT:
                raise
            self._reopen()
            return False

    def _reopen(self):
        now = time.monotonic()
        if now - self.last_reopen <= 1.0:
            return
        self.last_reopen = now
        self.close()
        try:
            self.fd = os.open(HIDG_MOUSE, os.O_WRONLY | os.O_NONBLOCK)
        except OSError:
            self.fd = None

    def close(self):
        if self.fd is not None:
            try:
                os.close(self.fd)
            except OSError:
                pass
            self.fd = None


def forward_ext_mouse(ext, mouse, cfg):
    """Read a USB mouse (ExtMouse) and drive the 1351 gadget (MouseForwarder),
    gated by the master mouse switch. Returns False if the device errored
    (unplugged) so the caller can drop it, True otherwise."""
    try:
        dx, dy, buttons = ext.read()
    except BlockingIOError:
        return True
    except OSError as ex:
        print("ext mouse lost:", ex, file=sys.stderr)
        return False
    if not cfg.get("touchpad_mouse", True):
        mouse.off()
        return True
    sens = float(cfg.get("ext_mouse_sensitivity", 1.0))
    mouse.emit(dx, dy, buttons, sens, sens)
    return True


class Bridge:
    def __init__(self, dev):
        self.dev = dev
        self.cfg = load_config()
        self.cfg_mtime = self._mtime()
        self.leds = find_leds()
        self.batt_path = find_battery(dev)   # power_supply sysfs node, or None
        self.battery = None                  # last-read capacity (0-100), or None
        self.charging = None                 # last-read charging flag, or None
        self.last_batt_ts = 0.0
        self.ax = {E.ABS_X: 128, E.ABS_Y: 128, E.ABS_RX: 128, E.ABS_RY: 128,
                   E.ABS_HAT0X: 0, E.ABS_HAT0Y: 0, E.ABS_Z: 0, E.ABS_RZ: 0}
        self.fire_down = set()
        self.circle = False
        self.f1_down = False         # options held -> F1 keycode in the keyboard report
        self.ps_held = False         # while the PS button is held, suppress all other input
        # The keyboard HID gadget fd. O_NONBLOCK so a write can never freeze the
        # bridge if the C64 stops draining the endpoint -- a dropped key report is
        # re-sent next tick. The mouse gadget (hidg1) lives in its own
        # MouseForwarder, shared by the touchpad and an external USB mouse.
        self.kbd_fd = os.open(HIDG_KBD, os.O_WRONLY | os.O_NONBLOCK)
        self.last_kbd_report = None
        self.last_kbd_reopen = 0.0
        self.mouse = MouseForwarder()
        self.wasd_on = None          # tri-state until first set_wasd()
        # WASD keystrokes are gated until the swapper-switch REST call lands, so a
        # W/A/S/D can't reach the C64 as a real letter before the U64 is in WASD mode.
        self._wasd_armed = False
        self.last_active = 0.0
        self.status_cache = None
        self.last_status_ts = 0.0
        self.sel = None
        self.tp = None
        self.ext_mouse = None        # external USB mouse on the Pi (ExtMouse), forwarded to hidg1
        self.last_ext_scan = 0.0
        # REST calls to the U64 (swapper + menu) run on a background worker so a
        # slow/timing-out HTTP request can never stall the event loop -- a stalled
        # loop buffers controller input and replays it in a burst on recovery.
        # Bounded queue: under a sustained outage drop new calls rather than pile up.
        self._rest_q = queue.Queue(maxsize=8)
        self._last_menu_ts = 0.0
        threading.Thread(target=self._rest_worker, daemon=True).start()
        # Bind the touchpad if present; whether its motion is sent is gated live by
        # the touchpad_mouse config (see poll_touchpad), so the mouse switches on
        # and off from the web panel without restarting joyd.
        tp_dev = find_touchpad()
        if tp_dev is not None:
            self.tp = Touchpad(tp_dev)
            print("Touchpad:", tp_dev.name, tp_dev.path, file=sys.stderr)
        # An external USB mouse, if already plugged in, is preferred over the
        # touchpad. Hotplug (plug/unplug while running) is handled in tick().
        dev_mouse = find_mouse()
        if dev_mouse is not None:
            self.ext_mouse = ExtMouse(dev_mouse)
            print("External mouse:", dev_mouse.name, dev_mouse.path, file=sys.stderr)

    # --- config ---
    def _mtime(self):
        try:
            return os.path.getmtime(CONFIG)
        except OSError:
            return 0

    def reload_config_if_changed(self):
        m = self._mtime()
        if m != self.cfg_mtime:
            self.cfg_mtime = m
            old_port = self.cfg.get("port")
            old_mouse = (self.cfg.get("touchpad_mouse"),
                         self.cfg.get("ext_mouse_with_touchpad"))
            self.cfg = load_config()
            if self.wasd_on and self.cfg.get("port") != old_port:
                self.rest_put("WASD Port %d" % int(self.cfg["port"]))
            # Disabling the mouse or handing the 1351 between sources: release any
            # held button now so it can't stick, regardless of which source moves next.
            if old_mouse != (self.cfg.get("touchpad_mouse"),
                             self.cfg.get("ext_mouse_with_touchpad")):
                self.mouse.off()
            self.write_status(force=True)

    # --- input state ---
    def reset_inputs(self):
        """Snap every tracked input back to neutral. Used when the PS button goes
        down so a joystick/button held during that hold cannot stay stuck and leak
        into the U64 menu when PS is released (axes only re-engage on a fresh
        evdev event, i.e. when the user actually moves them again)."""
        self.ax.update({E.ABS_X: 128, E.ABS_Y: 128, E.ABS_RX: 128, E.ABS_RY: 128,
                        E.ABS_HAT0X: 0, E.ABS_HAT0Y: 0, E.ABS_Z: 0, E.ABS_RZ: 0})
        self.fire_down.clear()
        self.circle = False
        self.f1_down = False

    def state(self):
        a = self.ax
        up = a[E.ABS_HAT0Y] < 0 or a[E.ABS_Y] < 128 - DEAD or a[E.ABS_RY] < 128 - DEAD
        down = a[E.ABS_HAT0Y] > 0 or a[E.ABS_Y] > 128 + DEAD or a[E.ABS_RY] > 128 + DEAD
        left = (a[E.ABS_HAT0X] < 0 or a[E.ABS_X] < 128 - DEAD or a[E.ABS_RX] < 128 - DEAD
                or (self.circle and self.cfg.get("circle_left", True)))
        right = a[E.ABS_HAT0X] > 0 or a[E.ABS_X] > 128 + DEAD or a[E.ABS_RX] > 128 + DEAD
        fire = bool(self.fire_down) or a[E.ABS_Z] > TRIG or a[E.ABS_RZ] > TRIG
        return up, down, left, right, fire

    def is_active(self):
        return any(self.state())

    # --- outputs ---
    def send_keys(self):
        keys = []
        # WASD only once the swapper switch has landed (_wasd_armed): until then the
        # U64 is still in Normal mode and the letters would type on screen. F1 is a
        # real function key -> it is sent whenever options is held, joystick or not.
        if self.wasd_on and self._wasd_armed:
            up, down, left, right, fire = self.state()
            if up: keys.append(W)
            if down: keys.append(S)
            if left: keys.append(A)
            if right: keys.append(D)
            if fire: keys.append(RET)
        if self.f1_down and self.cfg.get("options_f1", True):
            keys.append(F1)
        keys = keys[:6]
        # Standard 8-byte boot-keyboard payload: modifiers, reserved, 6 keycodes.
        report = bytes([0, 0] + keys + [0] * (6 - len(keys)))
        self._write_kbd(report)

    def send_release(self):
        self._write_kbd(bytes(8))

    def _write_kbd(self, report):
        """Send an 8-byte boot-keyboard report, deduped. O_NONBLOCK: if the C64
        isn't draining the endpoint, drop it -- the next changed report re-sends."""
        if report == self.last_kbd_report:
            return
        if self.kbd_fd is None:
            return
        try:
            os.write(self.kbd_fd, report)
            self.last_kbd_report = report
        except BlockingIOError:
            pass
        except OSError as ex:
            self._on_kbd_error(ex)

    def _on_kbd_error(self, ex):
        # The C64 USB host is absent (off / not enumerated): gadget writes fail
        # with ESHUTDOWN/ENODEV/EIO. Don't propagate -- that would tear the bridge
        # down into a tight reconnect loop and starve the status heartbeat. Force
        # a key re-send once the host returns, and reopen the fd at most once a
        # second so a stale endpoint cannot keep us wedged.
        if ex.errno not in HID_HOST_ABSENT:
            raise
        self.last_kbd_report = None
        now = time.monotonic()
        if now - self.last_kbd_reopen <= 1.0:
            return
        self.last_kbd_reopen = now
        try:
            os.close(self.kbd_fd)
        except OSError:
            pass
        try:
            self.kbd_fd = os.open(HIDG_KBD, os.O_WRONLY | os.O_NONBLOCK)
        except OSError:
            self.kbd_fd = None

    # --- touchpad / external mouse -> 1351 mouse ---
    def mouse_enabled(self):
        """The master mouse switch (web panel `touchpad_mouse`, also driven by the
        share/port logic). Logical only -- hidg1 stays enumerated either way, so the
        1351 keeps port-1 timing; this just gates whether any source emits."""
        return self.cfg.get("touchpad_mouse", True)

    def touch_active(self):
        """The controller touchpad drives the 1351 when the mouse is enabled and
        either no USB mouse is plugged in, or the user asked for both at once."""
        return self.mouse_enabled() and (
            self.ext_mouse is None or self.cfg.get("ext_mouse_with_touchpad", False))

    def poll_touchpad(self, now):
        if self.tp is None:
            return
        try:
            dx, dy, left, right = self.tp.read()
        except BlockingIOError:
            return
        except OSError as ex:
            print("touchpad lost:", ex, file=sys.stderr)
            self.drop_touchpad()
            return
        # Drain the events above even when not the active source (so re-enabling
        # never jumps the cursor); release a held button only when the mouse is
        # fully off -- a USB mouse owning the 1351 manages its own buttons.
        if not self.mouse_enabled():
            self.mouse.off()
            return
        if not self.touch_active():
            return
        buttons = 0x01 if left else 0
        if right and self.cfg.get("touchpad_two_finger_right", True):
            buttons |= 0x02
        # Invert is a touchpad-only convenience; a USB mouse is forwarded as-is.
        if self.cfg.get("mouse_invert_x", False):
            dx = -dx
        if self.cfg.get("mouse_invert_y", False):
            dy = -dy
        legacy = float(self.cfg.get("mouse_sensitivity", 1.0))
        self.mouse.emit(dx, dy, buttons,
                        float(self.cfg.get("mouse_sensitivity_x", legacy)),
                        float(self.cfg.get("mouse_sensitivity_y", legacy)))

    def poll_ext_mouse(self, now):
        """Forward an external USB mouse (relative motion + 3 buttons) to hidg1."""
        if self.ext_mouse is None:
            return
        if not forward_ext_mouse(self.ext_mouse, self.mouse, self.cfg):
            self.drop_ext_mouse()

    def drop_touchpad(self):
        if self.sel is not None and self.tp is not None:
            try:
                self.sel.unregister(self.tp.dev.fileno())
            except (KeyError, ValueError, OSError):
                pass
        self.tp = None

    def drop_ext_mouse(self):
        """External mouse unplugged or errored: unregister, close, and hand the
        1351 back to the touchpad cleanly (release any held button / motion)."""
        if self.sel is not None and self.ext_mouse is not None:
            try:
                self.sel.unregister(self.ext_mouse.dev.fileno())
            except (KeyError, ValueError, OSError):
                pass
        if self.ext_mouse is not None:
            try:
                self.ext_mouse.dev.close()
            except OSError:
                pass
        self.ext_mouse = None
        self.mouse.off()

    def refresh_ext_mouse(self, now):
        """Detect an external USB mouse appearing at runtime (hotplug). Scans at
        most once a second, and only while none is bound -- a connected mouse stops
        the scan, so there is no fd churn. Disappearance is caught in poll_ext_mouse."""
        if self.ext_mouse is not None or self.sel is None:
            return
        if now - self.last_ext_scan < 1.0:
            return
        self.last_ext_scan = now
        dev = find_mouse()
        if dev is None:
            return
        try:
            self.sel.register(dev.fileno(), selectors.EVENT_READ, "extmouse")
        except (KeyError, ValueError, OSError) as ex:
            print("ext mouse register failed:", ex, file=sys.stderr)
            try:
                dev.close()
            except OSError:
                pass
            return
        self.ext_mouse = ExtMouse(dev)
        self.mouse.off()   # clean handover from the touchpad
        print("External mouse:", dev.name, dev.path, file=sys.stderr)

    def set_color(self, rgb):
        if not self.leds:
            return
        try:
            if self.leds["kind"] == "multi":
                with open(self.leds["intensity"], "w") as f:
                    f.write("%d %d %d" % tuple(int(v) for v in rgb))
                with open(self.leds["brightness"], "w") as f:
                    f.write("255")
                return
            g = self.leds.get("global")
            if g and os.path.exists(g):
                with open(g, "w") as f:
                    f.write("1")
            for ch, val in zip(("red", "green", "blue"), rgb):
                with open(self.leds[ch], "w") as f:
                    f.write(str(int(val)))
        except OSError as ex:
            print("lightbar write error:", ex, file=sys.stderr)

    def _rest_call(self, label, url):
        """Blocking PUT -- only call OFF the event loop (worker thread / shutdown)."""
        try:
            req = urllib.request.Request(url, method="PUT")
            urllib.request.urlopen(req, timeout=3).read()
        except Exception as ex:
            print("REST %s failed: %s" % (label, ex), file=sys.stderr)

    def _rest_worker(self):
        while True:
            label, url, done = self._rest_q.get()
            self._rest_call(label, url)
            if done is not None:
                done()
            self._rest_q.task_done()

    def _rest_enqueue(self, label, url, done=None):
        try:
            self._rest_q.put_nowait((label, url, done))
        except queue.Full:
            print("REST queue full, dropping", label, file=sys.stderr)
            # Don't strand the WASD gate if the switch is dropped under an outage --
            # arm anyway so the joystick stays alive rather than dead until a toggle.
            if done is not None:
                done()

    def _swapper_url(self, value):
        return "http://%s%s?value=%s" % (
            self.cfg["u64_host"], SWAPPER_PATH, urllib.parse.quote(value))

    def rest_put(self, value, done=None):
        self._rest_enqueue("put '%s'" % value, self._swapper_url(value), done)

    def _arm_wasd(self):
        """Worker-thread callback: the swapper switch has landed, so WASD keystrokes
        may now flow. Plain bool store is atomic under the GIL; the event loop reads
        it on the next tick (<=TICK later) and sends any held direction as joystick."""
        self._wasd_armed = True

    def set_wasd(self, on):
        if on == self.wasd_on:
            return
        if on:
            # Hold back WASD keystrokes until the switch REST call returns, or a held
            # direction would type a letter before the U64 leaves Normal mode.
            self._wasd_armed = False
            self.rest_put("WASD Port %d" % int(self.cfg["port"]), done=self._arm_wasd)
            self.set_color(self.cfg["active_color"])
            self.wasd_on = True
        else:
            self.send_release()
            self.rest_put("Normal")
            self.set_color(self.cfg["idle_color"])
            self.wasd_on = False
        print("WASD ->", "ON" if on else "OFF", file=sys.stderr)
        self.write_status(force=True)

    def toggle_wasd(self, now):
        # Both modes: triangle toggles WASD. In auto, after a triangle-off any
        # real input turns it straight back on (react() handles that next tick).
        self.set_wasd(not self.wasd_on)

    def swap_port(self):
        """Share button: flip the joystick between control port 1 and 2, exactly
        like clicking PORT1/PORT2 in the web panel. It writes the shared config
        file; reload_config_if_changed then re-applies the swapper and refreshes
        status.json on the next tick, and the web UI follows over its SSE stream.
        Useful in couch mode when a game wants the joystick on Joy1."""
        cfg = load_config()
        new_port = 1 if int(cfg.get("port", 2)) == 2 else 2
        cfg["port"] = new_port
        # The U64 hardwires a USB mouse to control port 1, so a port-1 joystick and
        # the 1351 mouse can't share it: moving the joystick to port 1 frees it by
        # turning the touchpad mouse off (mirrors web/server.py post_config).
        if new_port == 1 and cfg.get("touchpad_mouse"):
            cfg["touchpad_mouse"] = False
        write_config(cfg)
        print("share -> swap to port", new_port, file=sys.stderr)

    def toggle_ext_with_touchpad(self):
        """Square button: flip "use the touchpad alongside a USB mouse", exactly
        like the web panel's simultaneous-touchpad switch. Writes the shared config;
        reload_config_if_changed re-applies it and refreshes the web UI over SSE.
        Matters only while a USB mouse is plugged in (otherwise the pad already
        owns the 1351) -- a couch shortcut to grab the touchpad without unplugging."""
        cfg = load_config()
        cfg["ext_mouse_with_touchpad"] = not cfg.get("ext_mouse_with_touchpad", False)
        write_config(cfg)
        print("square -> touchpad with USB mouse:", cfg["ext_mouse_with_touchpad"],
              file=sys.stderr)

    def menu_tap(self):
        """Toggle the U64 menu via the menu_button API, which simulates the
        physical menu button -- the firmware's real open/close toggle. A USB
        F10 only opens it, and the physical button is a hardware signal no USB
        scancode can reach. See firmware api/route_machine.cc."""
        now = time.monotonic()
        if now - self._last_menu_ts < MENU_DEBOUNCE:
            return
        self._last_menu_ts = now
        url = "http://%s/v1/machine:menu_button" % self.cfg["u64_host"]
        self._rest_enqueue("menu_button", url)
        print("PS -> menu (toggle)", file=sys.stderr)

    # --- status for the web UI ---
    def _read_battery(self):
        """Refresh the cached battery reading from sysfs, throttled. Capacity
        changes slowly, so reading it on every tick would be wasteful."""
        if self.batt_path is None:
            return
        now = time.monotonic()
        if (now - self.last_batt_ts) < BATT_INTERVAL and self.last_batt_ts:
            return
        self.last_batt_ts = now
        try:
            with open(self.batt_path + "/capacity") as f:
                self.battery = int(f.read().strip())
        except (OSError, ValueError):
            self.battery = None
        try:
            with open(self.batt_path + "/status") as f:
                self.charging = f.read().strip() == "Charging"
        except OSError:
            self.charging = None

    def write_status(self, force=False):
        self._read_battery()
        st = {
            "controller": self.dev.name,
            "wasd_on": bool(self.wasd_on),
            "mode": self.cfg["mode"],
            "port": int(self.cfg["port"]),
            "battery": self.battery,
            "charging": self.charging,
            "ext_mouse": self.ext_mouse is not None,
            "ts": time.time(),
        }
        key = (st["controller"], st["wasd_on"], st["mode"], st["port"],
               st["battery"], st["charging"], st["ext_mouse"])
        now = time.monotonic()
        if not force and key == self.status_cache and (now - self.last_status_ts) < HEARTBEAT:
            return
        self.status_cache = key
        self.last_status_ts = now
        try:
            os.makedirs(os.path.dirname(STATUS), exist_ok=True)
            tmp = STATUS + ".tmp"
            with open(tmp, "w") as f:
                json.dump(st, f)
            os.replace(tmp, STATUS)
        except OSError as ex:
            print("status write error:", ex, file=sys.stderr)

    # --- main loop ---
    def run(self):
        sel = selectors.DefaultSelector()
        self.sel = sel
        try:
            self.set_wasd(False)   # clean baseline: Normal + idle color
            sel.register(self.dev.fileno(), selectors.EVENT_READ, "pad")
            if self.tp is not None:
                sel.register(self.tp.dev.fileno(), selectors.EVENT_READ, "touch")
            if self.ext_mouse is not None:
                try:
                    sel.register(self.ext_mouse.dev.fileno(), selectors.EVENT_READ, "extmouse")
                except (KeyError, ValueError, OSError) as ex:
                    print("ext mouse register failed:", ex, file=sys.stderr)
                    self.ext_mouse = None
            while True:
                events = sel.select(timeout=TICK)
                now = time.monotonic()
                for key, _ in events:
                    if key.data == "touch":
                        self.poll_touchpad(now)
                    elif key.data == "extmouse":
                        self.poll_ext_mouse(now)
                    else:
                        try:
                            for e in self.dev.read():
                                self.handle(e, now)
                        except BlockingIOError:
                            pass
                self.tick(now)
        finally:
            # Controller gone or error: leave the C64 in Normal, release keys.
            try:
                self.send_release()
            except OSError:
                pass
            self._rest_call("put 'Normal'", self._swapper_url("Normal"))
            self.wasd_on = False
            if self.ext_mouse is not None:
                try:
                    self.ext_mouse.dev.close()
                except OSError:
                    pass
            self.mouse.close()
            if self.kbd_fd is not None:
                try:
                    os.close(self.kbd_fd)
                except OSError:
                    pass

    def handle(self, e, now):
        if e.type == E.EV_KEY and e.code == E.BTN_MODE:  # PS -> menu toggle on release
            if e.value == 1:
                # Release everything while PS is held: clear any held key/joy so it
                # can't stay stuck and drive the menu (or instantly confirm) once
                # the menu opens on release.
                self.ps_held = True
                self.reset_inputs()
                self.send_release()
            elif e.value == 0:
                self.ps_held = False
                if self.cfg.get("ps_menu", True):
                    self.menu_tap()
            return
        if self.ps_held:
            # PS held -> ignore every other input (see the BTN_MODE press handler).
            return
        if e.type == E.EV_KEY and e.code == E.BTN_NORTH:  # triangle -> WASD toggle
            if e.value == 1:
                self.toggle_wasd(now)
            return
        if e.type == E.EV_ABS and e.code in self.ax:
            self.ax[e.code] = e.value
        elif e.type == E.EV_KEY and e.code in FIRE_BTNS:
            if e.value:
                self.fire_down.add(e.code)
            else:
                self.fire_down.discard(e.code)
        elif e.type == E.EV_KEY and e.code == E.BTN_EAST:  # circle
            self.circle = bool(e.value)
        elif e.type == E.EV_KEY and e.code == E.BTN_START:  # options -> F1
            self.f1_down = bool(e.value)
        elif e.type == E.EV_KEY and e.code == E.BTN_SELECT:  # share -> swap port 1<->2
            if e.value == 1 and self.cfg.get("share_swap", True):
                self.swap_port()
            return
        elif e.type == E.EV_KEY and e.code == E.BTN_WEST:  # square -> touchpad alongside USB mouse
            if e.value == 1:
                self.toggle_ext_with_touchpad()
            return
        else:
            return
        self.react(now)

    def tick(self, now):
        self.reload_config_if_changed()
        self.refresh_ext_mouse(now)
        self.react(now)

    def react(self, now):
        active = self.is_active()
        if active:
            self.last_active = now
        if self.cfg["mode"] == "auto":
            if active and not self.wasd_on:
                self.set_wasd(True)
            elif self.wasd_on and (now - self.last_active) > float(self.cfg["idle_timeout"]):
                self.set_wasd(False)
        self.send_keys()
        self.write_status()


def write_waiting_status(ext_mouse=False):
    """Status while no controller is connected, so the web UI stays informed. A
    USB mouse may still be active on its own, so ext_mouse reflects its presence."""
    cfg = load_config()
    st = {"controller": None, "wasd_on": False,
          "mode": cfg["mode"], "port": int(cfg["port"]),
          "battery": None, "charging": None, "ext_mouse": bool(ext_mouse),
          "ts": time.time()}
    try:
        os.makedirs(os.path.dirname(STATUS), exist_ok=True)
        tmp = STATUS + ".tmp"
        with open(tmp, "w") as f:
            json.dump(st, f)
        os.replace(tmp, STATUS)
    except OSError:
        pass


def wait_for_controller(poll=1.0):
    """Block until a game controller appears, returning its evdev device.

    While waiting, a USB mouse plugged into the Pi is still forwarded to the 1351
    gadget, so the mouse keeps working on its own -- a controller is not required
    for it. The two are independent: either, both, or neither can be present.
    find_controller, the mouse hotplug scan and the heartbeat status are throttled
    to `poll` seconds; mouse motion itself is forwarded as soon as it arrives."""
    mouse = MouseForwarder()
    ext = None
    sel = selectors.DefaultSelector()
    cfg = load_config()
    last_check = 0.0
    try:
        while True:
            now = time.monotonic()
            if (now - last_check) >= poll:
                last_check = now
                cfg = load_config()
                dev = find_controller()
                if dev is not None:
                    return dev
                if ext is None:
                    dev_mouse = find_mouse()
                    if dev_mouse is not None:
                        ext = ExtMouse(dev_mouse)
                        sel.register(dev_mouse.fileno(), selectors.EVENT_READ, "mouse")
                        mouse.off()
                        print("External mouse:", dev_mouse.name, dev_mouse.path, file=sys.stderr)
                write_waiting_status(ext_mouse=ext is not None)
            if ext is None:
                time.sleep(poll)   # nothing to forward; just pace the controller poll
                continue
            if sel.select(timeout=poll) and not forward_ext_mouse(ext, mouse, cfg):
                try:
                    sel.unregister(ext.dev.fileno())
                except (KeyError, ValueError, OSError):
                    pass
                try:
                    ext.dev.close()
                except OSError:
                    pass
                ext = None
                mouse.off()
    finally:
        if ext is not None:
            try:
                ext.dev.close()
            except OSError:
                pass
        mouse.close()
        sel.close()


def main():
    while True:
        dev = wait_for_controller()
        print("Using:", dev.name, dev.path, file=sys.stderr)
        try:
            Bridge(dev).run()
        except OSError as ex:
            print("controller lost:", ex, file=sys.stderr)
        except Exception as ex:
            print("bridge error:", ex, file=sys.stderr)
        time.sleep(1.0)   # guard against a tight respawn if a present controller keeps failing


if __name__ == "__main__":
    sys.exit(main())
