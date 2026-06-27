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
NM_PSAVE_CONF=/etc/NetworkManager/conf.d/00-ds64-wifi-powersave-off.conf
CONFIG_DIR=/etc/ds64
ARM_FREQ_CAP="${DS64_ARM_FREQ:-800}"   # MHz cap; tune via DS64_ARM_FREQ (600 = safest)
UNITS=(ds64-gadget ds64-gadget-watch ds64-bt-connectable ds64-bt-watch ds64-joyd ds64-web ds64-ntp)

REBOOT_NEEDED=0
BT_RESTART=0
NM_RELOAD=0

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

# --- refuse to update a hardened (read-only) system ----------------------------
# With the overlay active, writes to the root fs (/opt/ds64, systemd units, ...)
# land on a tmpfs the next reboot discards, so the update would silently vanish.
overlay_active=0
[ "$(findmnt -n -o FSTYPE / 2>/dev/null)" = "overlay" ] && overlay_active=1
if command -v raspi-config >/dev/null 2>&1 \
   && [ "$(raspi-config nonint get_overlay_now 2>/dev/null)" = "0" ]; then
    overlay_active=1
fi
if [ "$overlay_active" -eq 1 ]; then
    die "This Pi is hardened (read-only) -- an update would not stick.
    Unlock first, then update with ds64-update (it runs this installer for you):
        sudo ds64-unlock      # disables the overlay, then reboots
    After it reboots:  sudo ds64-update   (add --dev for the dev branch)"
fi

# --- 1. packages ---------------------------------------------------------------
say "Packages"
miss=()
for p in git python3-evdev python3-aiohttp; do
    dpkg -s "$p" >/dev/null 2>&1 || miss+=("$p")
done
if [ "${#miss[@]}" -gt 0 ]; then
    chg "installing: ${miss[*]}"
    apt-get update -qq
    DEBIAN_FRONTEND=noninteractive apt-get install -y "${miss[@]}"
else
    ok "git, python3-evdev, python3-aiohttp already present"
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
sync   # flush the checkout to the persistent image (the Pi runs off the C64's power;
       # git does not fsync, so an unsynced loop-image write can be lost on a power-cut)
after=$(git -C "$DEST" rev-parse -q --verify HEAD 2>/dev/null || echo none)
if [ "$before" = "$after" ]; then ok "already at ${after:0:7}"; else chg "now at ${after:0:7}"; fi

# --- 3. config.txt: USB gadget + CPU power cap ---------------------------------
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

# Cap the CPU to cut peak current draw. The Pi runs off the C64's USB power
# (non-spec supply); the 1.8 GHz turbo current spike destabilises the shared
# WiFi/BT radio -> controller drops + `hci0: command tx timeout` firmware wedge.
# A controller bridge needs no speed. (600 MHz fully killed the wedge; 900 MHz
# still wedged rarely, so the default cap is a balanced 800 MHz -- set
# DS64_ARM_FREQ to tune, e.g. 600 if a rare wedge persists.)
say "CPU power cap in $CONFIG_TXT (arm_boost=0, arm_freq=$ARM_FREQ_CAP)"
cpu_changed=0
if grep -qE '^[[:space:]]*arm_boost=0[[:space:]]*$' "$CONFIG_TXT"; then :
elif grep -qE '^[[:space:]]*arm_boost=' "$CONFIG_TXT"; then
    sed -i -E 's/^[[:space:]]*arm_boost=.*/arm_boost=0/' "$CONFIG_TXT"; cpu_changed=1
else
    printf '\n[all]\narm_boost=0\n' >> "$CONFIG_TXT"; cpu_changed=1
fi
if grep -qE "^[[:space:]]*arm_freq=${ARM_FREQ_CAP}[[:space:]]*\$" "$CONFIG_TXT"; then :
elif grep -qE '^[[:space:]]*arm_freq=' "$CONFIG_TXT"; then
    sed -i -E "s/^[[:space:]]*arm_freq=.*/arm_freq=${ARM_FREQ_CAP}/" "$CONFIG_TXT"; cpu_changed=1
else
    printf '\n[all]\narm_freq=%s\n' "$ARM_FREQ_CAP" >> "$CONFIG_TXT"; cpu_changed=1
fi
if [ "$cpu_changed" -eq 1 ]; then
    chg "capped CPU (arm_boost=0, arm_freq=$ARM_FREQ_CAP) -- reboot needed"
    REBOOT_NEEDED=1
else
    ok "CPU cap (arm_boost=0, arm_freq=$ARM_FREQ_CAP) present"
fi

# DS64 is a headless appliance -- no display, no speaker -- so strip the graphics
# stack to trim idle current off the C64's USB rail and free RAM. Disabling the
# vc4 KMS overlay unloads the whole DRM/vc4/v3d module stack and releases its CMA
# reservation (~448 MB on a 2 GB Pi 4); with no KMS and no monitor the firmware
# leaves HDMI powered off on its own (vcgencmd display_power=0), so no extra
# HDMI-off step is needed. audio=off drops the bcm2835 soundcard (the snd_bcm2835
# module stays resident but inert). The ACT LED, WiFi, BT and Ethernet are left ON.
say "Headless: disable KMS/GPU + audio in $CONFIG_TXT"
hp_changed=0
if grep -qE '^[[:space:]]*dtoverlay=vc4-f?kms-v3d' "$CONFIG_TXT"; then
    sed -i -E 's/^([[:space:]]*dtoverlay=vc4-f?kms-v3d.*)/#\1/' "$CONFIG_TXT"
    chg "disabled KMS/GPU overlay (vc4-kms-v3d) -- reboot needed"
    hp_changed=1
else
    ok "KMS/GPU overlay already disabled/absent"
fi
if grep -qE '^[[:space:]]*max_framebuffers=' "$CONFIG_TXT"; then
    sed -i -E 's/^([[:space:]]*max_framebuffers=.*)/#\1/' "$CONFIG_TXT"
    chg "disabled max_framebuffers -- reboot needed"
    hp_changed=1
fi
if grep -qE '^[[:space:]]*dtparam=audio=off[[:space:]]*$' "$CONFIG_TXT"; then
    ok "audio already off"
elif grep -qE '^[[:space:]]*dtparam=audio=' "$CONFIG_TXT"; then
    sed -i -E 's/^([[:space:]]*)dtparam=audio=.*/\1dtparam=audio=off/' "$CONFIG_TXT"
    chg "disabled audio (dtparam=audio=off) -- reboot needed"
    hp_changed=1
else
    printf '\n[all]\ndtparam=audio=off\n' >> "$CONFIG_TXT"
    chg "added dtparam=audio=off -- reboot needed"
    hp_changed=1
fi
[ "$hp_changed" -eq 1 ] && REBOOT_NEEDED=1

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

# --- 6. WiFi power-save off (Bluetooth coexistence) ----------------------------
# WiFi and BT share one radio on the Pi (BCM4345). With WiFi power-save on, the
# radio naps and starves the BT side -> the controller link drops every few
# minutes, and on the resulting disconnect the BT firmware wedges
# (`hci0: command 0x0406 tx timeout`) until a reboot. A global NM default keeps
# the radio awake for every (re)connected WiFi profile, netplan-rendered or not.
say "WiFi power-save off -> $NM_PSAVE_CONF"
want_psave=$'[connection]\nwifi.powersave = 2'
if [ -f "$NM_PSAVE_CONF" ] && [ "$(cat "$NM_PSAVE_CONF")" = "$want_psave" ]; then
    ok "wifi.powersave = 2 (disabled) already set"
else
    install -d -m 755 "$(dirname "$NM_PSAVE_CONF")"
    printf '%s\n' "$want_psave" > "$NM_PSAVE_CONF"
    chg "wrote $NM_PSAVE_CONF (wifi.powersave = 2)"
    NM_RELOAD=1
fi

# --- 7. app config dir ---------------------------------------------------------
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

# --- 8. systemd units ----------------------------------------------------------
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

# --- 9. maintenance tooling ----------------------------------------------------
say "Maintenance tooling -> /usr/local/sbin"
install -m 755 "$DEST/setup/ds64-lock"   /usr/local/sbin/ds64-lock
install -m 755 "$DEST/setup/ds64-unlock" /usr/local/sbin/ds64-unlock
install -m 755 "$DEST/setup/ds64-update" /usr/local/sbin/ds64-update
install -m 755 "$DEST/setup/ds64-bt-reset" /usr/local/sbin/ds64-bt-reset
ok "installed ds64-lock, ds64-unlock, ds64-update, ds64-bt-reset"

# --- 10. apply now (or defer to the reboot) ------------------------------------
if [ "$REBOOT_NEEDED" -eq 0 ]; then
    if [ "$BT_RESTART" -eq 1 ]; then systemctl restart bluetooth || true; fi
    if [ "$NM_RELOAD" -eq 1 ]; then
        nmcli general reload 2>/dev/null || systemctl reload NetworkManager 2>/dev/null || true
        wifi_dev=$(nmcli -t -f DEVICE,TYPE d 2>/dev/null | awk -F: '/:wifi$/{print $1; exit}')
        [ -n "$wifi_dev" ] && iw dev "$wifi_dev" set power_save off 2>/dev/null || true
    fi
    say "Applying (dwc2 already active) -- (re)starting services"
    systemctl restart ds64-gadget ds64-gadget-watch ds64-bt-connectable ds64-bt-watch ds64-joyd ds64-web ds64-ntp || true
    ok "services restarted"
fi

# --- 11. harden by default (read-only appliance) -------------------------------
# A C64 has no shutdown -- power is just cut -- so the safe steady state is the
# read-only overlay (runtime writes go to RAM, dropped on reboot) plus a small
# journaled image that persists the bond / WiFi / config. Opt out: DS64_NO_HARDEN=1.
HARDEN=0
if [ -z "${DS64_NO_HARDEN:-}" ]; then
    say "Hardening (read-only appliance) -- set DS64_NO_HARDEN=1 to skip"
    if DS64_IMG="$BOOT/ds64-data.img" DS64_NO_REBOOT=1 /usr/local/sbin/ds64-lock; then
        HARDEN=1
        REBOOT_NEEDED=1
    else
        warn "hardening failed -- system left writable; retry later with: sudo ds64-lock"
    fi
else
    ok "DS64_NO_HARDEN set -- root filesystem left writable (not power-loss proof)"
fi

# --- summary -------------------------------------------------------------------
ip=$(hostname -I 2>/dev/null | awk '{print $1}') || ip=
host=$(hostname 2>/dev/null || echo raspberrypi)
bar=$(printf '─%.0s' {1..58})

printf '\n  ┌%s┐\n'   "$bar"
printf   '  │  %-56s│\n' "DS64 installed"
printf   '  └%s┘\n\n' "$bar"

printf '    Web UI        http://%s/\n'   "${ip:-<pi-ip>}"
printf '                  http://%s.local/\n\n' "$host"
printf '    Pair a pad    open the Web UI, or run:\n'
printf '                  bash %s/scripts/pair-ds4.sh\n\n' "$DEST"
if [ "$HARDEN" -eq 1 ]; then
    printf '    Appliance     read-only mode is ON -- power-loss proof\n\n'
    printf '    Update        sudo ds64-update   # the only command you need\n'
    printf '                  code-only changes apply with no reboot; unit /\n'
    printf '                  installer / config changes run this installer for\n'
    printf '                  you (it asks you to ds64-unlock first if locked)\n'
else
    printf '    Harden        make it power-loss proof (recommended):\n'
    printf '                  sudo ds64-lock     # read-only + persistent store\n'
    printf '                  sudo ds64-unlock   # undo, e.g. to update the OS\n\n'
    printf '    Update        sudo ds64-update   # git-pull + restart; runs this\n'
    printf '                  installer automatically for structural changes\n'
fi

if [ "$REBOOT_NEEDED" -eq 1 ]; then
    printf '\n  %s\n' "$bar"
    if [ "$HARDEN" -eq 1 ]; then
        printf '    ! ONE reboot activates USB gadget + read-only mode.\n'
    else
        printf '    ! ONE reboot is needed to activate USB gadget mode.\n'
    fi
    printf '      The Web UI above will not respond until the Pi reboots.\n'
    printf '  %s\n' "$bar"
    if [ -t 1 ] && [ -e /dev/tty ]; then
        printf '\n    Press ENTER to reboot now  (Ctrl-C to reboot later yourself) '
        read -r _ < /dev/tty || true
        printf '\n    Rebooting...\n'
        reboot
    else
        printf '\n    Reboot when ready:  sudo reboot\n'
    fi
else
    printf '\n    Ready to use now -- open the Web UI above.\n'
fi
