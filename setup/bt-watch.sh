#!/bin/bash
# DS64 Bluetooth watchdog: detect the BCM4345C0 UART firmware wedge and recover
# it with no reboot by calling ds64-bt-reset.
#
# Detection: `hciconfig hci0 name` (HCI Read Local Name) is a pure controller
# command -- it never touches an air link, so it is safe to poll even while a
# controller is connected and in use. A healthy chip answers in <100 ms with a
# "Name:" line; a wedged firmware makes the kernel return ETIMEDOUT and the
# output carries no "Name:". hciconfig is lenient about exit codes, so we match
# its output, not its status.
#
# A real wedge is permanent, so we act only after several CONSECUTIVE failed
# probes -- one slow reply (adapter momentarily busy during a connection setup)
# never triggers a reset. If a recovery does not restore the chip after a few
# tries we back off for a while rather than tight-loop (a deeper wedge needs a
# reboot, which this watchdog cannot do).
set -u

HCI=hci0
POLL=10            # seconds between probes while healthy
PROBE_TIMEOUT=4    # seconds allowed for one probe
FAILS_TO_ACT=3     # consecutive failed probes before recovering (~POLL*N s)
COOLDOWN=20        # seconds to let the chip settle after a recovery
MAX_TRIES=3        # consecutive failed recoveries -> long back-off
BACKOFF=300        # sleep after MAX_TRIES failed recoveries

RESET=/opt/ds64/setup/ds64-bt-reset
[ -f "$RESET" ] || RESET=/usr/local/sbin/ds64-bt-reset

log() { printf 'ds64-bt-watch: %s\n' "$*" >&2; }

chip_ok() { timeout "$PROBE_TIMEOUT" hciconfig "$HCI" name 2>&1 | grep -q "Name:"; }

fails=0
fail_resets=0
while true; do
    if chip_ok; then
        fails=0
        fail_resets=0
        sleep "$POLL"
        continue
    fi

    fails=$((fails + 1))
    if [ "$fails" -lt "$FAILS_TO_ACT" ]; then
        log "$HCI unresponsive ($fails/$FAILS_TO_ACT)"
        sleep "$POLL"
        continue
    fi

    log "$HCI wedged ($fails consecutive failures) -> running ds64-bt-reset"
    bash "$RESET"
    fails=0
    sleep "$COOLDOWN"

    if chip_ok; then
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
