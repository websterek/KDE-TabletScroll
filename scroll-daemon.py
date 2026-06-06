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
                [--config TOML]

Config file (~/.config/tabletscroll/config.toml):
  sensitivity = 0.03
  deadzone = 16
  scroll_events = "both"
  # CLI flags override config values.

Requirements:
  python-evdev (pacman -S python-evdev or pip install evdev)
  User in 'input' group (sudo usermod -a -G input $USER → relogin after)
"""

import argparse
import math
import os
import signal
import sys
import time
from pathlib import Path
from select import select

try:
    import tomllib
except ImportError:
    try:
        import tomli as tomllib  # Python <3.11 fallback
    except ImportError:
        tomllib = None

from evdev import InputDevice, UInput, ecodes, list_devices

# ── CLI ────────────────────────────────────────────────────────────

_verbose = False  # set by main() when --verbose is passed

def _dbg(msg):
    """Print a debug message only when --verbose is set."""
    if _verbose:
        print(f"[INFO] {msg}")

def parse_args():
    """Parse CLI arguments with config file support.

    Loads TOML config first (default path or --config override), then uses
    config values as argparse defaults. CLI flags override config values.
    Hardcoded defaults are the fallback when no config file exists.

    Returns:
        Namespace with fields: sensitivity, deadzone, no_horizontal, invert,
        verbose, device, scroll_events, config.
    """
    p = argparse.ArgumentParser(description='Middle-click scroll daemon')
    p.add_argument('--config', type=str, default=None,
                   help='Path to TOML config file (default: ~/.config/tabletscroll/config.toml)')

    # Parse --config first to know which file to load, then use config
    # values as defaults for the remaining arguments.
    args, _ = p.parse_known_args()
    config_overrides = load_config(args.config)

    p.add_argument('--sensitivity', type=float,
                   default=config_overrides.get('sensitivity', 0.03),
                   help='Scroll speed in notches/s per pixel from anchor (default: 0.03)')
    p.add_argument('--deadzone', type=int,
                   default=config_overrides.get('deadzone', 16),
                   help='Radius in pixels where no scroll occurs (default: 16)')
    p.add_argument('--no-horizontal', '--vertical-only', action='store_true',
                   default=config_overrides.get('no_horizontal', False),
                   help='Disable horizontal scroll')
    p.add_argument('--invert', action='store_true',
                   default=config_overrides.get('invert', False),
                   help='Invert scroll direction')
    p.add_argument('--verbose', action='store_true',
                   default=config_overrides.get('verbose', False),
                   help='Print status messages (device opened, scroll mode, etc.)')
    p.add_argument('--device', type=str,
                   default=config_overrides.get('device', None),
                   help='Match only devices whose name contains this string (case-insensitive)')
    p.add_argument('--scroll-events', choices=['hi-res', 'both'],
                   default=config_overrides.get('scroll_events', 'both'),
                   help='Scroll event type: hi-res only or hi-res + standard (default: both)')

    return p.parse_args()


# ── config file ─────────────────────────────────────────────────────

CONFIG_DIR = Path.home() / '.config' / 'tabletscroll'
DEFAULT_CONFIG = CONFIG_DIR / 'config.toml'

# Map TOML keys to argparse dest names. Only these keys are read;
# unknown keys are silently ignored.
_CONFIG_KEYS = {
    'sensitivity', 'deadzone', 'no_horizontal', 'invert',
    'verbose', 'device', 'scroll_events',
}


def load_config(path=None):
    """Load TOML config file, returning a dict of overrides.

    Args:
        path: Path to config file. Falls back to DEFAULT_CONFIG if None.

    Returns:
        Dict mapping argparse dest names to values. Empty if no config
        found, TOML not available, or file is unreadable.

    Raises:
        SystemExit: If the config file exists but contains invalid TOML.
    """
    if tomllib is None:
        return {}

    filepath = Path(path) if path else DEFAULT_CONFIG
    if not filepath.is_file():
        return {}

    try:
        with open(filepath, 'rb') as f:
            raw = tomllib.load(f)
    except Exception as e:
        print(f"[ERROR] Failed to parse config file '{filepath}': {e}", file=sys.stderr)
        sys.exit(1)

    return {k: v for k, v in raw.items() if k in _CONFIG_KEYS}


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


def compute_scroll_deltas(anchor, cursor, deadzone, sensitivity, invert, dt):
    """Compute raw scroll deltas from cursor offset from anchor.

    Pure function — no state, no side effects. The caller handles
    accumulation and emission.

    Args:
        anchor: (x, y) anchor point.
        cursor: (x, y) current cursor position.
        deadzone: radius in pixels where no scroll is produced.
        sensitivity: scroll speed in notches/s per pixel from anchor.
        invert: if True, reverse scroll direction.
        dt: time delta since last tick in seconds.

    Returns:
        (delta_hi_x, delta_hi_y, delta_std_x, delta_std_y) as floats
        representing the amount to add to respective accumulators.
        Returns None if cursor is within the deadzone.
    """
    dx = cursor[0] - anchor[0]
    dy = cursor[1] - anchor[1]
    dist = math.hypot(dx, dy)
    if dist <= deadzone:
        return None

    effective_dist = dist - deadzone
    rate = effective_dist * sensitivity  # notches/s

    # Direction (normalized) — vertical inverted for Windows-style:
    # moving cursor down → scroll down (positive REL_WHEEL)
    nx = dx / dist
    ny = -dy / dist

    if invert:
        nx = -nx
        ny = -ny

    delta_hi_x = nx * rate * dt * 120
    delta_hi_y = ny * rate * dt * 120
    delta_std_x = nx * rate * dt
    delta_std_y = ny * rate * dt

    return (delta_hi_x, delta_hi_y, delta_std_x, delta_std_y)




# ── main ────────────────────────────────────────────────────────────

def main():
    """Run the middle-click scroll daemon.

    Discovers input devices, creates a uinput scroll device, and enters the
    event loop. Middle-click-and-hold enters scroll mode; release exits.
    Ctrl+C or SIGTERM triggers graceful shutdown.
    """
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
    # monotonic deadline: until this time, the anchor floats with the cursor
    # to absorb click-pressure wobble. No scroll fires during this window.
    anchor_settle_deadline = 0.0
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
        # Block with no timeout when idle (zero CPU), tick at ~60fps when scrolling
        timeout = SCROLL_TICK if mode == SCROLL_MODE else None
        try:
            r, _, _ = select(fds, [], [], timeout)
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
                            mode = SCROLL_MODE
                            trigger_dev = fd
                            anchor = _get_device_position(fd, last_pos, rel_accum)
                            anchor_settle_deadline = time.monotonic() + SETTLE_DURATION
                            anchor_settle_samples = []
                            # Reset dt clock so first frame isn't inflated by idle time
                            last_tick = time.monotonic()
                            _dbg(f"Scroll mode ON ({name})")
                        elif mode == SCROLL_MODE:
                            mode = IDLE
                            trigger_dev = None
                            # anchor intentionally preserved — frozen at last position
                            anchor_settle_deadline = 0.0
                            anchor_settle_samples = []
                            _dbg("Scroll mode OFF")

                    elif evalue == 0:  # RELEASE
                        if mode == SCROLL_MODE:
                            mode = IDLE
                            trigger_dev = None
                            anchor_settle_deadline = 0.0
                            anchor_settle_samples = []
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

            # Offset from fixed anchor → raw deltas (pure physics)
            now = time.monotonic()
            dt = max(now - last_tick, 0.0)
            last_tick = now

            deltas = compute_scroll_deltas(anchor, (cx, cy), args.deadzone,
                                           args.sensitivity, args.invert, dt)
            if deltas is None:
                # Deadzone: no scroll, clear accumulators
                scroll_hi_x = 0.0
                scroll_hi_y = 0.0
                std_notch_x = 0.0
                std_notch_y = 0.0
                continue

            delta_hi_x, delta_hi_y, delta_std_x, delta_std_y = deltas

            # Accumulate hi-res (120 units per notch)
            scroll_hi_x += delta_hi_x
            scroll_hi_y += delta_hi_y

            emit_hi_x = int(scroll_hi_x)
            emit_hi_y = int(scroll_hi_y)
            if emit_hi_x or emit_hi_y:
                scroll_hi_x -= emit_hi_x
                scroll_hi_y -= emit_hi_y
                emit_scroll(ui, emit_hi_x, emit_hi_y, args.no_horizontal)

            # Fire standard (whole-notch) events when enabled
            if args.scroll_events == 'both':
                std_notch_x += delta_std_x
                std_notch_y += delta_std_y
                emit_std_x = int(std_notch_x)
                emit_std_y = int(std_notch_y)
                if emit_std_x or emit_std_y:
                    std_notch_x -= emit_std_x
                    std_notch_y -= emit_std_y
                    if emit_std_y:
                        ui.write(ecodes.EV_REL, ecodes.REL_WHEEL, emit_std_y)
                    if emit_std_x and not args.no_horizontal:
                        ui.write(ecodes.EV_REL, ecodes.REL_HWHEEL, emit_std_x)
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
