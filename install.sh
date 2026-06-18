#!/bin/bash
# DS64 installer / updater -- run on a fresh Raspberry Pi OS (Bookworm/Trixie).
#
#   curl -fsSL https://raw.githubusercontent.com/wallneradam/DS64/main/install.sh | sudo bash
#
# Idempotent: every step checks the current state and only changes what is wrong,
# so re-running it is safe -- it repairs a broken install and pulls the latest
# software (git). It sets up the FUNCTIONAL product only (controller -> C64
# keyboard bridge + web UI). It does NOT touch the partition table and does NOT
# enable the read-only overlay; power-loss hardening is a separate, reversible
# step you run when ready:  sudo ds64-lock  (undo: sudo ds64-unlock).
set -euo pipefail

REPO_URL="${DS64_REPO:-https://github.com/wallneradam/DS64.git}"
BRANCH="${DS64_BRANCH:-main}"
DEST="${DS64_DEST:-/opt/ds64}"
MODULES_CONF=/etc/modules-load.d/c64u-joy.conf
INPUT_CONF=/etc/bluetooth/input.conf
CONFIG_DIR=/etc/ds64
UNITS=(ds64-gadget ds64-gadget-watch ds64-bt-connectable ds64-joyd ds64-web)

REBOOT_NEEDED=0
BT_RESTART=0

say()  { printf '\n==> %s\n' "$*"; }
ok()   { printf '    [ok] %s\n' "$*"; }
chg()  { printf '    ->  %s\n' "$*"; }
warn() { printf '    [!] %s\n' "$*" >&2; }
die()  { printf '\n[!] %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "Run as root:  curl -fsSL <url> | sudo bash"

# --- locate the boot (FAT) partition where config.txt lives --------------------
if   [ -f /boot/firmware/config.txt ]; then BOOT=/boot/firmware
elif [ -f /boot/config.txt ];          then BOOT=/boot
else die "config.txt not found in /boot/firmware or /boot -- is this Raspberry Pi OS?"
fi
CONFIG_TXT="$BOOT/config.txt"

# --- 1. packages ---------------------------------------------------------------
say "Packages"
miss=()
for p in git python3-evdev; do
    dpkg -s "$p" >/dev/null 2>&1 || miss+=("$p")
done
if [ "${#miss[@]}" -gt 0 ]; then
    chg "installing: ${miss[*]}"
    apt-get update -qq
    DEBIAN_FRONTEND=noninteractive apt-get install -y "${miss[@]}"
else
    ok "git, python3-evdev already present"
fi

# --- 2. source: clone or update /opt/ds64 to origin/$BRANCH --------------------
# `git init` is idempotent, so this single path both converts a non-git /opt/ds64
# (e.g. an older rsync deploy) into a tracked clone and updates an existing one.
say "Source -> $DEST ($BRANCH)"
mkdir -p "$DEST"
git -C "$DEST" init -q
if git -C "$DEST" remote get-url origin >/dev/null 2>&1; then
    git -C "$DEST" remote set-url origin "$REPO_URL"
else
    git -C "$DEST" remote add origin "$REPO_URL"
fi
before=$(git -C "$DEST" rev-parse -q --verify HEAD 2>/dev/null || echo none)
git -C "$DEST" fetch -q --depth=1 origin "$BRANCH"
git -C "$DEST" reset -q --hard "origin/$BRANCH"
git -C "$DEST" clean -qfd
after=$(git -C "$DEST" rev-parse -q --verify HEAD 2>/dev/null || echo none)
if [ "$before" = "$after" ]; then ok "already at ${after:0:7}"; else chg "now at ${after:0:7}"; fi

# --- 3. USB gadget (peripheral) mode in config.txt -----------------------------
say "USB gadget (dwc2 peripheral) in $CONFIG_TXT"
if grep -qE '^[[:space:]]*dtoverlay=dwc2,dr_mode=peripheral' "$CONFIG_TXT"; then
    ok "dtoverlay=dwc2,dr_mode=peripheral present"
else
    printf '\n[all]\ndtoverlay=dwc2,dr_mode=peripheral\n' >> "$CONFIG_TXT"
    chg "added dtoverlay=dwc2,dr_mode=peripheral (reboot needed)"
    REBOOT_NEEDED=1
fi
if grep -qE '^[[:space:]]*dtoverlay=dwc2,dr_mode=host' "$CONFIG_TXT"; then
    warn "a conflicting 'dtoverlay=dwc2,dr_mode=host' line exists in $CONFIG_TXT --"
    warn "remove it or the USB-C port may stay in host mode and the gadget won't bind."
fi

# --- 4. controller kernel modules at boot --------------------------------------
say "Controller modules -> $MODULES_CONF"
want_mods=$'uhid\nhid_playstation'
if [ -f "$MODULES_CONF" ] && [ "$(cat "$MODULES_CONF")" = "$want_mods" ]; then
    ok "uhid, hid_playstation already configured"
else
    printf '%s\n' "$want_mods" > "$MODULES_CONF"
    modprobe uhid 2>/dev/null || true
    modprobe hid_playstation 2>/dev/null || true
    chg "wrote $MODULES_CONF"
fi

# --- 5. let the controller bond (BlueZ) ----------------------------------------
say "Bluetooth bonding policy in $INPUT_CONF"
if [ ! -f "$INPUT_CONF" ]; then
    warn "$INPUT_CONF missing -- skipping (is bluez installed?)"
elif grep -qE '^[[:space:]]*ClassicBondedOnly[[:space:]]*=[[:space:]]*false' "$INPUT_CONF"; then
    ok "ClassicBondedOnly=false already set"
else
    if grep -qE '^[[:space:]]*#?[[:space:]]*ClassicBondedOnly[[:space:]]*=' "$INPUT_CONF"; then
        sed -i -E 's/^[[:space:]]*#?[[:space:]]*ClassicBondedOnly[[:space:]]*=.*/ClassicBondedOnly=false/' "$INPUT_CONF"
    elif grep -qE '^\[General\]' "$INPUT_CONF"; then
        sed -i -E '/^\[General\]/a ClassicBondedOnly=false' "$INPUT_CONF"
    else
        printf '\n[General]\nClassicBondedOnly=false\n' >> "$INPUT_CONF"
    fi
    chg "set ClassicBondedOnly=false"
    BT_RESTART=1
fi

# --- 6. app config dir ---------------------------------------------------------
say "App config -> $CONFIG_DIR"
install -d -m 755 "$CONFIG_DIR"
if [ -f "$CONFIG_DIR/config.json" ]; then
    ok "config.json present (kept)"
elif [ -f "$DEST/config.example.json" ]; then
    install -m 644 "$DEST/config.example.json" "$CONFIG_DIR/config.json"
    chg "seeded config.json from config.example.json"
else
    ok "no example config; daemons will create defaults on first run"
fi

# --- 7. systemd units ----------------------------------------------------------
say "systemd units"
for u in "${UNITS[@]}"; do
    install -m 644 "$DEST/setup/systemd/$u.service" "/etc/systemd/system/$u.service"
done
# the persistence unit is installed but left DISABLED -- ds64-lock enables it.
install -m 644 "$DEST/setup/systemd/ds64-persist.service" /etc/systemd/system/ds64-persist.service
# Fence BlueZ + NetworkManager behind ds64-persist so a bus-activated early start
# cannot read the un-bound config dirs. Wants= (not Requires=) keeps them starting
# even if persistence is absent/fails -- non-bricking. Harmless before ds64-lock:
# ds64-persist skips cleanly (ConditionPathExists) until the image exists.
for svc in bluetooth NetworkManager; do
    install -d -m 755 "/etc/systemd/system/$svc.service.d"
    printf '[Unit]\nAfter=ds64-persist.service\nWants=ds64-persist.service\n' \
        > "/etc/systemd/system/$svc.service.d/ds64-persist.conf"
done
systemctl daemon-reload
systemctl enable -q "${UNITS[@]}"
ok "installed + enabled: ${UNITS[*]}"

# --- 8. maintenance tooling ----------------------------------------------------
say "Maintenance tooling -> /usr/local/sbin"
install -m 755 "$DEST/setup/ds64-lock"   /usr/local/sbin/ds64-lock
install -m 755 "$DEST/setup/ds64-unlock" /usr/local/sbin/ds64-unlock
ok "installed ds64-lock, ds64-unlock"

# --- 9. apply now (or defer to the reboot) -------------------------------------
if [ "$REBOOT_NEEDED" -eq 0 ]; then
    if [ "$BT_RESTART" -eq 1 ]; then systemctl restart bluetooth || true; fi
    say "Applying (dwc2 already active) -- (re)starting services"
    systemctl restart ds64-gadget ds64-gadget-watch ds64-bt-connectable ds64-joyd ds64-web || true
    ok "services restarted"
fi

# --- summary -------------------------------------------------------------------
ip=$(hostname -I 2>/dev/null | awk '{print $1}') || ip=
host=$(hostname 2>/dev/null || echo raspberrypi)
say "Done."
if [ "$REBOOT_NEEDED" -eq 1 ]; then
    printf '    USB gadget mode was just enabled -- reboot to activate it:\n'
    printf '        sudo reboot\n'
fi
printf '    Web UI:        http://%s:8080/   (also http://%s.local:8080/)\n' "${ip:-<pi-ip>}" "$host"
printf '    Pair a pad:    open the web UI, or  bash %s/scripts/pair-ds4.sh\n' "$DEST"
printf '    Harden (read-only, power-loss proof) when it all works:\n'
printf '        sudo ds64-lock      # enable overlay + persistent store (one reboot)\n'
printf '        sudo ds64-unlock    # undo, e.g. to update the OS\n'
printf '    Update later:  re-run this installer (it just git-pulls + restarts).\n'
