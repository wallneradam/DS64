# DS64 — a modern game controller as a Commodore 64 joystick (software only)

> Plug a Raspberry Pi into your Commodore 64 Ultimate, pair a PlayStation
> controller over Bluetooth, and play. No joystick, no adapter, no soldering,
> no extra parts — just software.

If your joystick hasn't arrived yet (or you only ever had one, like a lot of us
in the 80s), this lets you play right now with a controller you already own —
even co-op with a small kid on the keyboard.

## How it works

Three pieces, no custom firmware:

1. The C64 Ultimate firmware has a built-in **"Joystick Input"** mode that maps
   the **W / A / S / D** keys (and **RETURN** as fire) to a **real** joystick
   the C64 reads at the control port.
2. A Raspberry Pi plugged into the U64's **USB-C** port presents itself as a
   plain **USB keyboard** (Linux USB gadget mode).
3. A small daemon on the Pi reads a **Bluetooth game controller** and "presses"
   W/A/S/D/RETURN. The FPGA turns those keystrokes into joystick movement.

```
controller --BT--> Raspberry Pi --USB-C--> C64 Ultimate --> game
                (USB keyboard gadget)   (WASD -> joystick)
```

Directions come from **either analog stick or the D-pad**; fire from
**X / L1 / R1 / L2 / R2** (any of them).

## What you need

- A **Commodore 64 Ultimate II** (verified on factory firmware `c64u_v1.1.0`).
- A **Raspberry Pi 4 Model B**. Its USB-C port becomes the keyboard; the four
  USB-A ports stay in host mode for Bluetooth. It can be powered straight from
  the U64's USB-C port.
- A **Bluetooth game controller** (verified with a Sony DualShock 4, which
  pairs as "Wireless Controller"). Anything Linux exposes as a gamepad via
  evdev should work.

## Limitations

- **One joystick** (you choose port 1 or 2 in the U64 config). Two independent
  USB joysticks are not possible on the factory firmware.
- While a WASD mode is active, **W/A/S/D and RETURN are consumed** by the
  joystick, so you can't type those keys at the same time. Fine for ~95% of
  games; a planned toggle will switch back to normal typing when the controller
  is idle (see Roadmap).

## Setup

A one-line installer is planned (see Roadmap). For now, the manual steps on a
fresh Raspberry Pi OS:

**1. Enable USB gadget (peripheral) mode.** Append to `/boot/firmware/config.txt`:

```
[all]
dtoverlay=dwc2,dr_mode=peripheral
```

**2. Load the controller kernel modules at boot.** Create
`/etc/modules-load.d/c64u-joy.conf`:

```
uhid
hid_playstation
```

**3. Let the controller bond.** In `/etc/bluetooth/input.conf`, under
`[General]`:

```
ClassicBondedOnly=false
```

**4. Install the Python dependency:**

```
sudo apt install python3-evdev
```

Reboot after steps 1–3.

**5. Create the USB keyboard gadget:**

```
sudo bash setup/gadget-setup.sh
```

`/dev/hidg0` now exists. Test it by typing onto the C64 screen:

```
sudo python3 src/type.py "hello world"
```

**6. Pair the controller** (hold **SHARE + PS** until the lightbar
double-flashes):

```
bash scripts/pair-ds4.sh
```

**7. On the C64 Ultimate**, set **Settings -> Joystick Input -> "WASD Port 2"**.
Leaving it unsaved keeps it RAM-only and it reverts on power-off.

**8. Run the bridge:**

```
sudo python3 src/joyd.py
```

Move the sticks / D-pad and press a fire button — the C64 sees a joystick.

## Files

| Path                     | What it does                                              |
|--------------------------|-----------------------------------------------------------|
| `src/joyd.py`            | The bridge: controller -> WASD/RETURN over `/dev/hidg0`.  |
| `src/type.py`            | Type literal text onto the C64 (handy for testing).       |
| `src/joydemo.py`         | Hold each direction + fire ~1.5s to demo the joystick.    |
| `scripts/pair-ds4.sh`    | Scan for and pair/trust/connect a PlayStation controller. |
| `setup/gadget-setup.sh`  | Build the USB HID keyboard gadget (`/dev/hidg0`).         |

## Roadmap

- **Web UI** for pairing and configuration (in progress).
- **One-line installer** runnable straight from git on a fresh Pi.
- **systemd services** so the gadget, daemon and Bluetooth come up on boot.
- **Keyboard separation:** auto-switch the U64 between WASD and normal typing
  (via its REST config API, RAM-only) when the controller goes idle.

## License

GPLv3 — see [LICENSE](LICENSE).
