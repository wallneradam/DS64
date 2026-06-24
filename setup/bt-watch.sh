#!/bin/bash
# DS64 Bluetooth watchdog: recover the BCM4345C0 UART firmware wedge with no
# reboot -- without ever poking the radio while a controller is connected.
#
# Connection-gated by design. The BCM4345C0 shares one radio between WiFi and
# BT (coex). Injecting an HCI command (e.g. `hciconfig hci0 name`) while a
# controller is actively connected competes with the latency-sensitive HID ACL
# traffic; on this coex-stressed part the injected command can itself time out
# (`hci0: command tx timeout`), and one tx timeout jams the kernel HCI command
# queue so every following probe fails too. A naive "poll the chip every N
# seconds" watchdog therefore reads a false wedge and resets the chip, force-
# disconnecting the controller mid-game -- i.e. it CAUSES the very dropouts it
# is meant to cure. So while a controller is connected we NEVER touch the chip.
#
# The wedge we must auto-recover is the disconnect-time firmware lockup (the
# classic `command 0x0406 tx timeout`): it strikes as the link is torn down,
# leaving the chip dead so the controller can no longer reconnect. By then no
# connection exists, so probing is safe -- there is no active ACL link to
# disturb, and a false-positive reset while idle disconnects nobody.
#
# Connection state comes from sysfs (/sys/class/bluetooth/hci0:<handle>): a pure
# filesystem read, no HCI command, safe to poll mid-game. The liveness probe
# (`hciconfig hci0 name`) runs ONLY when no connection is present, and a reset
# fires only after several consecutive idle failures. A deeper wedge a rebind
# cannot fix backs off rather than tight-looping (only a reboot clears that,
# which this watchdog cannot do).
set -u

HCI=hci0
POLL=10            # seconds between checks
PROBE_TIMEOUT=4    # seconds allowed for one liveness probe
FAILS_TO_ACT=3     # consecutive idle probe failures before recovering
COOLDOWN=20        # seconds to let the chip settle after a recovery
MAX_TRIES=3        # consecutive failed recoveries -> long back-off
BACKOFF=300        # sleep after MAX_TRIES failed recoveries

RESET=/opt/ds64/setup/ds64-bt-reset
[ -f "$RESET" ] || RESET=/usr/local/sbin/ds64-bt-reset

log() { printf 'ds64-bt-watch: %s\n' "$*" >&2; }

# A controller is connected iff the kernel exposes a per-connection node
# /sys/class/bluetooth/hci0:<handle>. Pure sysfs read -- never touches the radio.
conn_present() {
    local c
    for c in /sys/class/bluetooth/"$HCI":*; do
        [ -e "$c" ] && return 0
    done
    return 1
}

# Liveness probe -- ONLY call this when no controller is connected. Read Local
# Name is a local controller command; hciconfig is lenient about exit codes, so
# match its output (a healthy chip prints a "Name:" line).
chip_ok() { timeout "$PROBE_TIMEOUT" hciconfig "$HCI" name 2>&1 | grep -q "Name:"; }

fails=0
fail_resets=0
while true; do
    # Playing: a controller is connected -> hands off entirely, never probe.
    if conn_present; then
        fails=0
        fail_resets=0
        sleep "$POLL"
        continue
    fi

    # Idle: no link to disturb, so the liveness probe is safe.
    if chip_ok; then
        fails=0
        fail_resets=0
        sleep "$POLL"
        continue
    fi

    fails=$((fails + 1))
    if [ "$fails" -lt "$FAILS_TO_ACT" ]; then
        log "$HCI unresponsive while idle ($fails/$FAILS_TO_ACT)"
        sleep "$POLL"
        continue
    fi

    log "$HCI wedged while idle ($fails consecutive failures) -> running ds64-bt-reset"
    bash "$RESET"
    fails=0
    sleep "$COOLDOWN"

    if conn_present || chip_ok; then
        log "recovery confirmed -- $HCI healthy again"
        fail_resets=0
    else
        fail_resets=$((fail_resets + 1))
        log "recovery did NOT restore $HCI (attempt $fail_resets/$MAX_TRIES)"
        if [ "$fail_resets" -ge "$MAX_TRIES" ]; then
            log "giving up for ${BACKOFF}s -- a reboot may be required"
            sleep "$BACKOFF"
            fail_resets=0
        fi
    fi
done
