#!/usr/bin/env python3
"""DS64 NTP server -- hands the C64 Ultimate an offset-corrected time.

The Ultimate's clock applies its time zone wrong (a named zone maps to the
neighbouring offset, and its DST is unreliable), so the displayed time is off by
a fixed amount no built-in setting can fix. NTP itself only ever transmits UTC --
the time zone is applied by the *client* -- so we sidestep the broken zone table
by lying on the wire: this server serves `real_UTC + ntp_offset_minutes`, where
the offset is whatever cancels the firmware's error for the zone the user picked
on the C64. Point the Ultimate's NTP server at this Pi, then nudge the offset in
the web UI until the clock reads right (re-tune by ~1h at daylight-saving).

The offset is read from the shared config the web panel writes; `ntp_enabled`
gates whether we bind UDP 123 at all (so the port is free when the feature is
off). Run as root -- 123 is privileged.

  sudo python3 src/ntpd.py
"""
import json
import os
import select
import socket
import struct
import sys
import time

CONFIG = os.environ.get("DS64_CONFIG", "/etc/ds64/config.json")
NTP_PORT = int(os.environ.get("DS64_NTP_PORT", "123"))

# Seconds between the NTP epoch (1900-01-01) and the Unix epoch (1970-01-01).
NTP_EPOCH_OFFSET = 2208988800

# systemd-timesyncd creates this once the system clock is NTP-synchronized.
SYNC_MARKER = "/run/systemd/timesync/synchronized"

DEFAULTS = {
    "ntp_enabled": False,      # bind UDP 123 and answer time queries
    "ntp_offset_minutes": 0,   # added to true UTC before it is served (DST tuning lives here)
}


def load_config():
    cfg = dict(DEFAULTS)
    try:
        with open(CONFIG) as f:
            cfg.update(json.load(f))
    except FileNotFoundError:
        pass
    except Exception as ex:
        print("ntpd: config read error:", ex, file=sys.stderr)
    return cfg


_cache = {"mtime": None, "cfg": dict(DEFAULTS)}


def get_config():
    """Config with an mtime cache, so an idle server does not re-read the file on
    every 1 s poll yet still picks up a web edit within a second."""
    try:
        m = os.path.getmtime(CONFIG)
    except OSError:
        return _cache["cfg"]
    if m != _cache["mtime"]:
        _cache["mtime"] = m
        _cache["cfg"] = load_config()
    return _cache["cfg"]


def clock_synced():
    """True once the Pi's own clock is trustworthy (NTP-synchronized). The Pi has
    no RTC and this read-only appliance can't persist the last-known time (the
    overlay upperdir is tmpfs), so after a cold boot the clock holds a stale baked-in
    value until timesyncd corrects it over the network. Answering during that window
    would hand the C64 a confidently-wrong time it won't re-poll for ~1h, so we stay
    silent until the marker appears -- the client's SNTP retries (forever, <=150 s)
    then pick up the correct time as soon as we are synced."""
    return os.path.exists(SYNC_MARKER)


def _ntp_ts(unix_time):
    """A Unix timestamp -> (seconds, fraction) in NTP 32.32 fixed-point form."""
    t = unix_time + NTP_EPOCH_OFFSET
    secs = int(t)
    frac = int((t - secs) * 4294967296.0)   # * 2**32
    return secs & 0xFFFFFFFF, frac & 0xFFFFFFFF


def build_reply(req, offset_seconds):
    """A mode-4 (server) reply to a client request, carrying `now + offset` as the
    served time. The offset is applied to the receive/transmit/reference stamps --
    the values the client uses to set its clock -- but NOT to the originate stamp,
    which must echo the client's own transmit time verbatim (bytes 40..47) so the
    client accepts the packet."""
    vn = (req[0] >> 3) & 0x7
    if vn == 0:
        vn = 4
    li_vn_mode = (0 << 6) | (vn << 3) | 4    # LI=0 (no warning), version echoed, mode 4 (server)
    now = time.time() + offset_seconds
    ref_s, ref_f = _ntp_ts(now)
    rx_s, rx_f = _ntp_ts(now)
    tx_s, tx_f = _ntp_ts(now)
    # stratum 1 (primary), poll 4, precision -6; zero root delay/dispersion; refid "LOCL".
    header = struct.pack("!BBbbII4s", li_vn_mode, 1, 4, -6, 0, 0, b"LOCL")
    return (header
            + struct.pack("!II", ref_s, ref_f)   # reference timestamp
            + bytes(req[40:48])                   # originate = client's transmit, echoed
            + struct.pack("!II", rx_s, rx_f)      # receive timestamp
            + struct.pack("!II", tx_s, tx_f))     # transmit timestamp


def main():
    print("ntpd: starting (serves UTC + ntp_offset_minutes on UDP %d)" % NTP_PORT,
          file=sys.stderr)
    sock = None
    warned_bind = False
    warned_unsynced = False
    try:
        while True:
            cfg = get_config()
            enabled = bool(cfg.get("ntp_enabled", False))

            if enabled and sock is None:
                try:
                    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                    s.bind(("0.0.0.0", NTP_PORT))
                    sock = s
                    warned_bind = False
                    print("ntpd: listening on 0.0.0.0:%d" % NTP_PORT, file=sys.stderr)
                except OSError as ex:
                    if not warned_bind:
                        print("ntpd: cannot bind UDP %d (%s) -- another NTP server/"
                              "client (chrony?) may hold it" % (NTP_PORT, ex), file=sys.stderr)
                        warned_bind = True
                    time.sleep(3)
                    continue
            elif not enabled and sock is not None:
                sock.close()
                sock = None
                print("ntpd: disabled -- released UDP %d" % NTP_PORT, file=sys.stderr)

            if sock is None:
                time.sleep(1)        # disabled: idle, re-checking the config each second
                continue

            # Wake at least once a second even with no traffic, so an enable/offset
            # change in the config is noticed promptly.
            try:
                ready, _, _ = select.select([sock], [], [], 1.0)
            except OSError:
                sock.close()
                sock = None
                continue
            if not ready:
                continue
            try:
                data, addr = sock.recvfrom(512)
            except OSError:
                continue
            if len(data) < 48:
                continue
            # Don't answer until the Pi's own clock is NTP-synced (see clock_synced):
            # the stale boot-time clock would otherwise be served as gospel and stick
            # on the C64 for ~1h. Dropping makes the client retry until we are synced.
            if not clock_synced():
                if not warned_unsynced:
                    print("ntpd: clock not NTP-synced yet -- dropping requests until it is",
                          file=sys.stderr)
                    warned_unsynced = True
                continue
            if warned_unsynced:
                print("ntpd: clock synced -- serving time", file=sys.stderr)
                warned_unsynced = False
            offset_min = int(cfg.get("ntp_offset_minutes", 0))
            try:
                sock.sendto(build_reply(data, offset_min * 60.0), addr)
                print("ntpd: served %s (UTC %+d min)" % (addr[0], offset_min), file=sys.stderr)
            except OSError:
                pass
    except KeyboardInterrupt:
        pass
    finally:
        if sock is not None:
            sock.close()


if __name__ == "__main__":
    main()
