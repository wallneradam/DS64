#!/bin/bash
# DS64 Bluetooth watchdog: recover the BCM4345C0 UART firmware wedge with no
# reboot -- driven by the kernel's own wedge signal, never by blind polling.
#
# Ground truth of the wedge is the kernel message `hci0: command ... tx timeout`
# (historically `command 0x0406 tx timeout`): the controller stopped answering
# HCI commands. The kernel logs it whenever ANYTHING touches a wedged chip --
# the link-death cleanup, bluetoothd, the connectable service -- so it fires
# whether or not a controller is "connected". We follow the kernel log and react
# to that line.
#
# Why log-driven and not a timer probe: the BCM4345C0 shares one radio between
# WiFi and BT (coex). A watchdog that injects `hciconfig hci0 name` on a timer
# competes with the live HID ACL traffic and can itself wedge the chip or chop a
# healthy link -- and a connection-gated probe misses an in-play wedge entirely
# (it never looks while "connected"). Reacting to the kernel's tx-timeout avoids
# both: we never poke the chip on a timer, and we still catch an in-play wedge
# the instant the kernel does.
#
# A lone tx timeout can be transient, so on the signal we confirm the chip is
# actually dead with ONE liveness probe (only ever after a real warning, never
# periodically) before resetting (unbind/rebind via ds64-bt-reset). A cooldown
# swallows the re-init aftershocks so one wedge causes one reset.
set -u

HCI=hci0
SETTLE=2           # seconds to wait after a timeout before confirming
PROBE_TIMEOUT=6    # seconds for the one confirmation probe
COOLDOWN=30        # seconds to ignore further timeouts right after a reset

RESET=/opt/ds64/setup/ds64-bt-reset
[ -f "$RESET" ] || RESET=/usr/local/sbin/ds64-bt-reset

log() { printf 'ds64-bt-watch: %s\n' "$*" >&2; }

# Dead iff a Read Local Name does NOT come back with a "Name:" line. hciconfig is
# lenient about exit codes, so match its output. Run ONLY to confirm a logged
# wedge -- never on a timer.
chip_dead() { ! timeout "$PROBE_TIMEOUT" hciconfig "$HCI" name 2>&1 | grep -q "Name:"; }

last_reset=-1000   # SECONDS at the last reset; start well outside any cooldown
log "watching the kernel log for '$HCI: command ... tx timeout'"

# Follow only NEW kernel messages from this boot (-n0), plain text (-o cat).
journalctl -kb -n0 -f -o cat 2>/dev/null | while IFS= read -r line; do
    case "$line" in
        *"$HCI: command"*"tx timeout"*) ;;
        *) continue ;;
    esac

    if [ $((SECONDS - last_reset)) -lt "$COOLDOWN" ]; then
        continue                       # re-init aftershock right after a reset
    fi

    log "kernel reports $HCI tx timeout -- confirming"
    sleep "$SETTLE"
    if chip_dead; then
        log "$HCI wedged -> running ds64-bt-reset"
        bash "$RESET"
        last_reset=$SECONDS
    else
        log "$HCI still responds -- transient timeout, no action"
    fi
done
