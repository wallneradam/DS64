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

Directions: either analog stick OR the D-pad. Fire: X / L1 / R1 / L2 / R2.
Optional extra buttons (each toggled in the config):
  PS button -> toggle the U64 menu on release (via the menu_button API)
  circle    -> Left (the "back" direction inside the U64 menu)
  square    -> F1 (a C64 function key, sent over the keyboard gadget)

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
    "square_f1": True,             # square -> F1 (a C64 function key)
    "touchpad_mouse": True,             # touchpad -> Commodore 1351 mouse (port 1)
    "mouse_sensitivity_x": 0.15,        # horizontal scale on the raw touchpad deltas
    "mouse_sensitivity_y": 0.2,         # vertical scale (the pad is short in Y -> a touch faster)
    "touchpad_two_finger_right": True,  # two fingers -> right mouse button
    "mouse_invert_x": False,            # flip horizontal motion if it feels reversed
    "mouse_invert_y": False,            # flip vertical motion if it feels reversed
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
        self.slot = 0
        self.tid = {}     # slot -> tracking id, present only while the finger is down
        self.pos = {}     # slot -> [x, y] latest absolute position
        self.last = {}    # slot -> [x, y] previous position, for the delta
        self.left = False

    def read(self):
        """Drain pending events; return (dx, dy, left, right) for this batch."""
        dx = dy = 0
        for e in self.dev.read():
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


class Bridge:
    def __init__(self, dev):
        self.dev = dev
        self.cfg = load_config()
        self.cfg_mtime = self._mtime()
        self.leds = find_leds()
        self.ax = {E.ABS_X: 128, E.ABS_Y: 128, E.ABS_RX: 128, E.ABS_RY: 128,
                   E.ABS_HAT0X: 0, E.ABS_HAT0Y: 0, E.ABS_Z: 0, E.ABS_RZ: 0}
        self.fire_down = set()
        self.circle = False
        self.f1_down = False         # square held -> F1 keycode in the keyboard report
        self.ps_held = False         # while the PS button is held, suppress all other input
        # Two independent HID gadget fds, one per interface. O_NONBLOCK so a write
        # can never freeze the bridge if the C64 stops draining an endpoint -- a
        # dropped report is re-sent (keys) or discarded (mouse motion is
        # fire-and-forget). The mouse fd opens only when a touchpad is present.
        self.kbd_fd = os.open(HIDG_KBD, os.O_WRONLY | os.O_NONBLOCK)
        self.mouse_fd = None
        self.last_kbd_report = None
        self.last_kbd_reopen = 0.0
        self.last_mouse_reopen = 0.0
        self.wasd_on = None          # tri-state until first set_wasd()
        self.last_active = 0.0
        self.status_cache = None
        self.last_status_ts = 0.0
        self.sel = None
        self.tp = None
        self.last_buttons = 0
        self.accum_x = 0.0           # fractional carry, so slow drags are not lost
        self.accum_y = 0.0
        # REST calls to the U64 (swapper + menu) run on a background worker so a
        # slow/timing-out HTTP request can never stall the event loop -- a stalled
        # loop buffers controller input and replays it in a burst on recovery.
        # Bounded queue: under a sustained outage drop new calls rather than pile up.
        self._rest_q = queue.Queue(maxsize=8)
        self._last_menu_ts = 0.0
        threading.Thread(target=self._rest_worker, daemon=True).start()
        # Always bind the touchpad if one is present; whether its motion is sent is
        # gated live by the touchpad_mouse config (see poll_touchpad), so the mouse
        # switches on and off from the web panel without restarting joyd.
        tp_dev = find_touchpad()
        if tp_dev is not None:
            self.tp = Touchpad(tp_dev)
            try:
                self.mouse_fd = os.open(HIDG_MOUSE, os.O_WRONLY | os.O_NONBLOCK)
            except OSError as ex:
                print("mouse gadget open failed:", ex, file=sys.stderr)
            print("Touchpad:", tp_dev.name, tp_dev.path, file=sys.stderr)

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
            self.cfg = load_config()
            if self.wasd_on and self.cfg.get("port") != old_port:
                self.rest_put("WASD Port %d" % int(self.cfg["port"]))
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
        # WASD only in joystick mode (otherwise the letters would type), but F1 is a
        # function key -> it is sent whenever square is held, joystick on or off.
        if self.wasd_on:
            up, down, left, right, fire = self.state()
            if up: keys.append(W)
            if down: keys.append(S)
            if left: keys.append(A)
            if right: keys.append(D)
            if fire: keys.append(RET)
        if self.f1_down and self.cfg.get("square_f1", True):
            keys.append(F1)
        keys = keys[:6]
        # Standard 8-byte boot-keyboard payload: modifiers, reserved, 6 keycodes.
        report = bytes([0, 0] + keys + [0] * (6 - len(keys)))
        self._write_kbd(report)

    def send_release(self):
        self._write_kbd(bytes(8))

    def _write_kbd(self, report):
        if report == self.last_kbd_report:
            return
        if self._emit("kbd", report):
            self.last_kbd_report = report

    def _emit(self, which, report):
        """Write one report to the keyboard ('kbd') or mouse fd. Returns True if
        it went out. The fds are O_NONBLOCK: if the C64 isn't draining that
        endpoint, drop the report (return False) instead of freezing the bridge.
        Callers decide whether to retry (keyboard re-sends next tick; mouse
        motion is dropped)."""
        fd = self.kbd_fd if which == "kbd" else self.mouse_fd
        if fd is None:
            return False
        try:
            os.write(fd, report)
            return True
        except BlockingIOError:
            return False
        except OSError as ex:
            self._on_hid_error(which, ex)
            return False

    def _on_hid_error(self, which, ex):
        # The C64 USB host is absent (off / not enumerated): gadget writes fail
        # with ESHUTDOWN/ENODEV. Don't propagate -- that would tear the bridge
        # down into a tight reconnect loop and starve the status heartbeat.
        # Force a key re-send once the host returns, and reopen the affected fd
        # at most once a second so a stale endpoint cannot keep us wedged.
        if ex.errno not in HID_HOST_ABSENT:
            raise
        now = time.monotonic()
        if which == "kbd":
            self.last_kbd_report = None
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
        else:
            if now - self.last_mouse_reopen <= 1.0:
                return
            self.last_mouse_reopen = now
            try:
                os.close(self.mouse_fd)
            except OSError:
                pass
            try:
                self.mouse_fd = os.open(HIDG_MOUSE, os.O_WRONLY | os.O_NONBLOCK)
            except OSError:
                self.mouse_fd = None

    # --- touchpad -> 1351 mouse ---
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
        # Drain the events above even when disabled (so re-enabling never jumps the
        # cursor), but only turn them into mouse reports while the mouse is on.
        if not self.cfg.get("touchpad_mouse", True):
            self.mouse_off()
            return
        self.emit_mouse(dx, dy, left, right)

    def mouse_off(self):
        """Mouse switched off at runtime: drop any carried motion and release a
        held button once, so the C64 never sees a stuck 1351 button."""
        self.accum_x = self.accum_y = 0.0
        if self.last_buttons and self._emit("mouse", bytes(3)):
            self.last_buttons = 0

    def emit_mouse(self, dx, dy, left, right):
        if self.cfg.get("mouse_invert_x", False):
            dx = -dx
        if self.cfg.get("mouse_invert_y", False):
            dy = -dy
        legacy = float(self.cfg.get("mouse_sensitivity", 1.0))
        self.accum_x += dx * float(self.cfg.get("mouse_sensitivity_x", legacy))
        self.accum_y += dy * float(self.cfg.get("mouse_sensitivity_y", legacy))
        mx = max(-127, min(127, int(self.accum_x)))
        my = max(-127, min(127, int(self.accum_y)))
        buttons = 0x01 if left else 0
        if right and self.cfg.get("touchpad_two_finger_right", True):
            buttons |= 0x02
        # Relative deltas must not be deduped (two identical moves are two moves),
        # but skip empty reports: nothing moved and no button changed.
        if mx == 0 and my == 0 and buttons == self.last_buttons:
            return
        # 3-byte mouse payload: buttons, relative dx, relative dy (no report ID).
        # Commit the accumulator and button state only after the report actually
        # went out, so a dropped (host-not-reading) report retries on the next
        # touchpad event instead of losing motion.
        report = bytes([buttons, mx & 0xFF, my & 0xFF])
        if self._emit("mouse", report):
            self.accum_x -= mx
            self.accum_y -= my
            self.last_buttons = buttons

    def drop_touchpad(self):
        if self.sel is not None and self.tp is not None:
            try:
                self.sel.unregister(self.tp.dev.fileno())
            except (KeyError, ValueError, OSError):
                pass
        self.tp = None

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
            label, url = self._rest_q.get()
            self._rest_call(label, url)
            self._rest_q.task_done()

    def _rest_enqueue(self, label, url):
        try:
            self._rest_q.put_nowait((label, url))
        except queue.Full:
            print("REST queue full, dropping", label, file=sys.stderr)

    def _swapper_url(self, value):
        return "http://%s%s?value=%s" % (
            self.cfg["u64_host"], SWAPPER_PATH, urllib.parse.quote(value))

    def rest_put(self, value):
        self._rest_enqueue("put '%s'" % value, self._swapper_url(value))

    def set_wasd(self, on):
        if on == self.wasd_on:
            return
        if on:
            self.rest_put("WASD Port %d" % int(self.cfg["port"]))
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
    def write_status(self, force=False):
        st = {
            "controller": self.dev.name,
            "wasd_on": bool(self.wasd_on),
            "mode": self.cfg["mode"],
            "port": int(self.cfg["port"]),
            "ts": time.time(),
        }
        key = (st["controller"], st["wasd_on"], st["mode"], st["port"])
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
            while True:
                events = sel.select(timeout=TICK)
                now = time.monotonic()
                for key, _ in events:
                    if key.data == "touch":
                        self.poll_touchpad(now)
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
            for fd in (self.kbd_fd, self.mouse_fd):
                if fd is not None:
                    try:
                        os.close(fd)
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
        elif e.type == E.EV_KEY and e.code == E.BTN_WEST:  # square -> F1
            self.f1_down = bool(e.value)
        else:
            return
        self.react(now)

    def tick(self, now):
        self.reload_config_if_changed()
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


def write_waiting_status():
    """Status while no controller is connected, so the web UI stays informed."""
    cfg = load_config()
    st = {"controller": None, "wasd_on": False,
          "mode": cfg["mode"], "port": int(cfg["port"]), "ts": time.time()}
    try:
        os.makedirs(os.path.dirname(STATUS), exist_ok=True)
        tmp = STATUS + ".tmp"
        with open(tmp, "w") as f:
            json.dump(st, f)
        os.replace(tmp, STATUS)
    except OSError:
        pass


def main():
    while True:
        dev = find_controller()
        if dev is None:
            write_waiting_status()
            time.sleep(1.0)
            continue
        print("Using:", dev.name, dev.path, file=sys.stderr)
        try:
            Bridge(dev).run()
        except OSError as ex:
            print("controller lost:", ex, file=sys.stderr)
            write_waiting_status()
        except Exception as ex:
            print("bridge error:", ex, file=sys.stderr)
            write_waiting_status()
        time.sleep(1.0)


if __name__ == "__main__":
    sys.exit(main())
