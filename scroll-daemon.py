#!/usr/bin/env python3
"""
Middle-click scroll daemon for Linux (X11 + Wayland).

  Middle-click-and-hold → Windows-style autoscroll:
  Pen/mouse distance from click point determines scroll speed and direction.
  Farther from anchor = faster. Direction = scroll direction.
  Release middle button → back to normal.

Usage:
  scroll-daemon [--sensitivity FLOAT] [--deadzone PX]
                [--no-horizontal] [--invert] [--device NAME]

Requirements:
  python-evdev (pacman -S python-evdev or pip install evdev)
  User in 'input' group (sudo usermod -a -G input $USER → relogin after)
"""

import argparse
import math
import signal
import sys
import time
from select import select

from evdev import InputDevice, UInput, ecodes, list_devices

# ── CLI ────────────────────────────────────────────────────────────

_verbose = False  # set by main() when --verbose is passed

def _dbg(msg):
    """Print a debug message only when --verbose is set."""
    if _verbose:
        print(f"[INFO] {msg}")

def parse_args():
    p = argparse.ArgumentParser(description='Middle-click scroll daemon')
    p.add_argument('--sensitivity', type=float, default=0.03,
                   help='Scroll speed in notches/s per pixel from anchor (default: 0.03)')
    p.add_argument('--deadzone', type=int, default=16,
                   help='Radius in pixels where no scroll occurs (default: 16)')
    p.add_argument('--no-horizontal', '--vertical-only', action='store_true',
                   help='Disable horizontal scroll')
    p.add_argument('--invert', action='store_true',
                   help='Invert scroll direction')
    p.add_argument('--verbose', action='store_true',
                   help='Print status messages (device opened, scroll mode, etc.)')
    p.add_argument('--device', type=str, default=None,
                   help='Match only devices whose name contains this string (case-insensitive)')
    p.add_argument('--scroll-events', choices=['hi-res', 'both'], default='both',
                   help='Scroll event type: hi-res only or hi-res + standard (default: both)')
    return p.parse_args()


# ── device discovery ───────────────────────────────────────────────

OUR_NAME = 'middle-click-scroll'

def find_middleclick_devices(name_filter=None):
    """Return list of (name, InputDevice) for devices with BTN_MIDDLE,
    excluding our own uinput device.

    Args:
        name_filter: If set, only include devices whose name contains this
                     substring (case-insensitive). Useful for picking a
                     specific device (e.g. 'G305' or 'wacom').
    """
    results = []
    for path in sorted(list_devices()):
        try:
            dev = InputDevice(path)
            caps = dev.capabilities(verbose=False)
            keys = caps.get(ecodes.EV_KEY, [])
            if dev.name == OUR_NAME:
                dev.close()
                continue
            if ecodes.BTN_MIDDLE in keys:
                if name_filter and name_filter.lower() not in dev.name.lower():
                    dev.close()
                    continue
                results.append((dev.name, dev))
            else:
                dev.close()
        except PermissionError:
            pass
        except Exception:
            pass
    return results


# ── uinput ─────────────────────────────────────────────────────────

def create_uinput():
    """Create a virtual uinput device for scroll wheel injection."""
    caps = {
        ecodes.EV_REL: [
            ecodes.REL_WHEEL, ecodes.REL_HWHEEL,
            ecodes.REL_WHEEL_HI_RES, ecodes.REL_HWHEEL_HI_RES,
        ],
        ecodes.EV_KEY: [ecodes.BTN_MIDDLE],
    }
    ui = UInput(caps, name=OUR_NAME)
    _dbg("uinput device created")
    return ui


# ── signal handling ────────────────────────────────────────────────

_shutdown = False

def handle_signal(sig, frame):
    """Signal handler for SIGINT/SIGTERM — sets flag to exit event loop gracefully."""
    global _shutdown
    _shutdown = True


# ── state machine constants ────────────────────────────────────────

IDLE = 0
SCROLL_MODE = 1

# Scroll tick interval: ~60 fps for smooth continuous scrolling
SCROLL_TICK = 0.016

# Duration (seconds) the anchor floats with cursor movement after entering
# scroll mode, absorbing click-pressure wobble before locking.
SETTLE_DURATION = 0.1


def _get_device_position(fd, last_pos, rel_accum):
    """Return (x, y) for a device from absolute tracking or relative accumulation."""
    pos = last_pos.get(fd)
    if pos is not None:
        return pos
    acc = rel_accum.get(fd, [0, 0])
    return (acc[0], acc[1])


def emit_scroll(ui, dx_hi_res, dy_hi_res, no_horizontal):
    """Inject hi-res scroll events and a sync packet.

    Args:
        ui: uinput device for injection.
        dx_hi_res: horizontal hi-res delta (signed int).
        dy_hi_res: vertical hi-res delta (signed int).
        no_horizontal: if True, suppress horizontal scroll.
    """
    fired = False
    if dy_hi_res:
        ui.write(ecodes.EV_REL, ecodes.REL_WHEEL_HI_RES, dy_hi_res)
        fired = True
    if dx_hi_res and not no_horizontal:
        ui.write(ecodes.EV_REL, ecodes.REL_HWHEEL_HI_RES, dx_hi_res)
        fired = True
    if fired:
        ui.syn()


def _enter_scroll_mode(fd, last_pos, rel_accum):
    """Set up scroll mode state: track given fd, seed anchor, arm settle window."""
    anchor = _get_device_position(fd, last_pos, rel_accum)
    return (SCROLL_MODE, fd, anchor, time.monotonic() + SETTLE_DURATION, [])

def _exit_scroll_mode():
    """Return idle state for scroll-mode fields (anchor is left unchanged)."""
    return (IDLE, None, 0.0, [])

# ── main ────────────────────────────────────────────────────────────

def main():
    global _verbose
    args = parse_args()
    _verbose = args.verbose

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    ui = create_uinput()

    dev_list = find_middleclick_devices(name_filter=args.device)
    if not dev_list:
        ui.close()
        print("[ERROR] No devices with BTN_MIDDLE found.", file=sys.stderr)
        print("[ERROR] Is the user in the 'input' group? (sudo usermod -a -G input $USER)",
              file=sys.stderr)
        sys.exit(1)

    fd_to_dev = {dev.fd: (name, dev) for name, dev in dev_list}

    # ── state ──
    mode = IDLE                # IDLE or SCROLL_MODE
    trigger_dev = None         # fd of the device that triggered scroll mode
    anchor = (0, 0)           # (x, y) anchor point
    anchor_settle_deadline = 0.0  # monotonic deadline; until this time the anchor
                                  # floats with the cursor to absorb
                                  # click-pressure wobble. No scroll fires.
    anchor_settle_samples = []  # cursor positions collected during settle window
    last_pos = {}              # {fd: (x, y)} last known position (absolute devices)
    rel_accum = {}             # {fd: [x, y]} accumulated relative motion (mice)
    scroll_hi_x = 0.0          # fractional hi-res scroll accumulators (120 → 1 notch)
    scroll_hi_y = 0.0          # finer quantization for smooth low-speed scrolling
    std_notch_x = 0.0          # fractional standard-notch accumulators (1 → 1 notch)
    std_notch_y = 0.0          # cleared together with scroll_hi_x/y on deadzone
    last_tick = time.monotonic()

    _dbg("Listening. Middle-click + hold to scroll. Ctrl+C to stop.")

    while not _shutdown:
        fds = [dev.fd for _, dev in dev_list]
        try:
            r, _, _ = select(fds, [], [], SCROLL_TICK)
        except (OSError, ValueError):
            continue

        # ── process events ──
        for fd in r:
            info = fd_to_dev.get(fd)
            if info is None:
                continue
            name, dev = info

            try:
                events = list(dev.read())
            except Exception:
                continue

            for event in events:
                etype = event.type
                ecode = event.code
                evalue = event.value

                # Track all position changes
                if etype == ecodes.EV_ABS:
                    cur = last_pos.get(fd, (0, 0))
                    if ecode == ecodes.ABS_X:
                        last_pos[fd] = (evalue, cur[1])
                    elif ecode == ecodes.ABS_Y:
                        last_pos[fd] = (cur[0], evalue)
                elif etype == ecodes.EV_REL:
                    acc = rel_accum.setdefault(fd, [0, 0])
                    if ecode == ecodes.REL_X:
                        acc[0] += evalue
                    elif ecode == ecodes.REL_Y:
                        acc[1] += evalue

                # Middle-click handling
                if etype == ecodes.EV_KEY and ecode == ecodes.BTN_MIDDLE:
                    if evalue == 1:  # PRESS
                        if mode == IDLE:
                            mode, trigger_dev, anchor, anchor_settle_deadline, anchor_settle_samples = \
                                _enter_scroll_mode(fd, last_pos, rel_accum)
                            # Reset dt clock so first frame isn't inflated by idle time
                            last_tick = time.monotonic()
                            _dbg(f"Scroll mode ON ({name})")
                        elif mode == SCROLL_MODE:
                            mode, trigger_dev, anchor_settle_deadline, anchor_settle_samples = \
                                _exit_scroll_mode()
                            _dbg("Scroll mode OFF")

                    elif evalue == 0:  # RELEASE
                        if mode == SCROLL_MODE:
                            mode, trigger_dev, anchor_settle_deadline, anchor_settle_samples = \
                                _exit_scroll_mode()
                            _dbg("Scroll mode OFF (released)")

        # ── scroll tick: emit scroll based on offset from anchor ──
        if mode == SCROLL_MODE and not _shutdown:
            fd = trigger_dev

            cx, cy = _get_device_position(fd, last_pos, rel_accum)

            # Settling window: for ~100ms after entering SCROLL_MODE,
            # collect cursor positions to compute the true resting center.
            # No scroll can fire during this window; it's too short
            # to be perceptible.
            if anchor_settle_deadline > 0:
                if time.monotonic() < anchor_settle_deadline:
                    anchor_settle_samples.append((cx, cy))
                    # Safety valve: if sampling runs abnormally long (e.g.
                    # SETTLE_DURATION was raised but the code wasn't updated),
                    # expire the window early so the list never grows without
                    # bound.  Normal operation (~100ms) collects <15 samples.
                    if len(anchor_settle_samples) >= 120:
                        anchor_settle_deadline = 0
                    continue
                # Window expired — lock anchor at average resting position
                if anchor_settle_samples:
                    sx = sum(s[0] for s in anchor_settle_samples) / len(anchor_settle_samples)
                    sy = sum(s[1] for s in anchor_settle_samples) / len(anchor_settle_samples)
                    anchor = (sx, sy)
                anchor_settle_samples.clear()
                anchor_settle_deadline = 0
                last_tick = time.monotonic()
                continue

            # Offset from fixed anchor
            dx = cx - anchor[0]
            dy = cy - anchor[1]

            # Deadzone: no scroll inside, clear all accumulators
            dist = math.hypot(dx, dy)
            if dist <= args.deadzone:
                scroll_hi_x = 0.0
                scroll_hi_y = 0.0
                std_notch_x = 0.0
                std_notch_y = 0.0
                last_tick = time.monotonic()
                continue

            # Frame-rate-independent physics.
            # sensitivity = notches/s per pixel from anchor.
            # Multiply by effective distance and delta-time for true rate.
            now = time.monotonic()
            dt = max(now - last_tick, 0.0)
            last_tick = now

            effective_dist = dist - args.deadzone
            rate = effective_dist * args.sensitivity  # notches/s

            # Direction (normalized) — vertical inverted for Windows-style:
            # moving cursor down → scroll down (positive REL_WHEEL)
            nx = dx / dist
            ny = -dy / dist

            if args.invert:
                nx = -nx
                ny = -ny

            # Accumulate at hi-res resolution (120 units per notch)
            delta_hi_x = nx * rate * dt * 120
            delta_hi_y = ny * rate * dt * 120
            scroll_hi_x += delta_hi_x
            scroll_hi_y += delta_hi_y

            # Fire hi-res events
            scr_x = int(scroll_hi_x)
            scr_y = int(scroll_hi_y)
            if scr_x or scr_y:
                scroll_hi_x -= scr_x
                scroll_hi_y -= scr_y
                emit_scroll(ui, scr_x, scr_y, args.no_horizontal)

            # Fire standard (whole-notch) events when enabled.
            # Derived from the same physical deltas, accumulated in separate
            # floats that are cleared alongside hi-res on deadzone entry.
            if args.scroll_events == 'both':
                delta_std_x = nx * rate * dt  # notches
                delta_std_y = ny * rate * dt
                std_notch_x += delta_std_x
                std_notch_y += delta_std_y
                std_x = int(std_notch_x)
                std_y = int(std_notch_y)
                if std_x or std_y:
                    std_notch_x -= std_x
                    std_notch_y -= std_y
                    if std_y:
                        ui.write(ecodes.EV_REL, ecodes.REL_WHEEL, std_y)
                    if std_x and not args.no_horizontal:
                        ui.write(ecodes.EV_REL, ecodes.REL_HWHEEL, std_x)
                    ui.syn()

    # ── cleanup ──
    _dbg("Shutting down...")
    ui.close()
    for _, dev in dev_list:
        try:
            dev.close()
        except Exception:
            pass
    _dbg("Goodbye.")


if __name__ == '__main__':
    main()
