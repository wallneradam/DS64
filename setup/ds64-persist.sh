#!/bin/bash
# DS64 persistent store -- mount the writable data image and bind the config dirs.
#
# Run at boot by ds64-persist.service, BEFORE bluetooth + NetworkManager start.
# When the read-only overlay is enabled, the root filesystem is RAM-backed and
# discards writes on reboot; this brings back a small journaled ext4 image (a
# plain file on the FAT boot partition) and bind-mounts onto it exactly the dirs
# that must survive a power-off: the controller bond, the WiFi profiles and the
# app config. With the overlay off it is harmless -- it just relocates those dirs
# onto the (more crash-safe) image. The image is created + seeded by ds64-lock;
# if it does not exist yet this script is a no-op.
set -u

IMG="${DS64_IMG:-/boot/firmware/ds64-data.img}"
MNT="${DS64_MNT:-/run/ds64-persist}"

# subdir-in-image  ->  bind target on the live filesystem
BINDS=(
    "bluetooth=/var/lib/bluetooth"
    "nm=/etc/NetworkManager/system-connections"
    "ds64=/etc/ds64"
)

[ -f "$IMG" ] || { echo "ds64-persist: $IMG absent -- nothing to mount"; exit 0; }

mkdir -p "$MNT"
if ! mountpoint -q "$MNT"; then
    mount -o loop,noatime "$IMG" "$MNT" || { echo "ds64-persist: cannot mount $IMG" >&2; exit 1; }
fi

rc=0
for entry in "${BINDS[@]}"; do
    sub="${entry%%=*}"
    target="${entry#*=}"
    src="$MNT/$sub"
    mkdir -p "$src" "$target"
    if mountpoint -q "$target"; then
        continue
    fi
    if ! mount --bind "$src" "$target"; then
        echo "ds64-persist: bind $src -> $target FAILED" >&2
        rc=1
    fi
done
exit "$rc"
