# KDE-TabletScroll

Windows-style middle-click autoscroll for Linux (X11 & Wayland) — designed for tablet input devices via [Open Tablet Drivers (OTD)](https://github.com/OpenTabletDriver/OpenTabletDriver).

## The Problem

KDE Plasma's built-in middle-click scroll emulation **does not work** with tablet input devices (Wacom Intuos, Huion, XP-Pen, etc.) on Wayland. Tablet users are left without any scroll-on-drag capability.

## The Solution

`scroll-daemon` works at the **evdev/uinput** layer, completely independent of your compositor:

- **Hold middle-click** (pen button, mouse wheel click) and move — the page scrolls
- **Farther from anchor = faster scroll**, direction is determined by movement
- Single middle-clicks pass through **unmodified** — no interference with Blender, Krita, etc.

## Supported Devices

| Type | Examples |
|------|----------|
| Tablets | Wacom, Huion, XP-Pen (via Open Tablet Drivers) |
| Mice | Any mouse with a middle button (Logitech G305, MX Master, etc.) |
| Any evdev device | Anything exposing `BTN_MIDDLE` |

## Requirements

- **Python 3.6+**
- **python-evdev**
- User in the `input` group

## Installation

### 1. Install python-evdev

```bash
# Arch Linux
sudo pacman -S python-evdev

# Other distros (pip)
pip install evdev
```

### 2. Add yourself to the `input` group

```bash
sudo usermod -a -G input $USER
```

**Log out and back in** (or reboot) for the group change to take effect.

### 3. Clone and run

```bash
git clone https://github.com/websterek/KDE-TabletScroll.git
cd KDE-TabletScroll
chmod +x scroll-daemon
./scroll-daemon
```

## Usage

```
scroll-daemon [OPTIONS]
```

### Options

| Flag | Type | Default | Description |
|------|------|---------|-------------|
| `--sensitivity` | float | `0.00024` | Scroll speed (higher = faster) |
| `--timeout` | int | `300` | Double‑click timeout in milliseconds |
| `--deadzone` | int | `16` | Dead zone radius in pixels (no scroll within) |
| `--no-horizontal` | flag | — | Disable horizontal scrolling |
| `--invert` | flag | — | Invert scroll direction |
| `--double-click` | flag | — | Require double-click-and-hold to scroll (default: single-click) |

### Examples

```bash
# Default — single middle-click-and-hold scrolls
./scroll-daemon

# Slower scrolling
./scroll-daemon --sensitivity 0.0001

# Double-click to activate (closer to Windows behavior)
./scroll-daemon --double-click

# No horizontal scroll, higher sensitivity
./scroll-daemon --no-horizontal --sensitivity 0.0005
```

## Autostart (KDE Plasma)

1. Open **System Settings → Startup and Shutdown → Autostart**
2. Click **Add → Add Script…**
3. Create a script with:

```bash
#!/bin/bash
/path/to/KDE-TabletScroll/scroll-daemon --sensitivity 0.00024 &
```

## How It Works

```
┌──────────┐    ┌─────────────┐    ┌──────────────┐
│  evdev   │    │ scroll-     │    │   uinput     │
│ devices  │───▶│ daemon      │───▶│ (virtual     │
│ (tablet, │    │ (reads pos, │    │  scroll      │
│  mouse)  │    │  injects    │    │  wheel)      │
│          │    │  scroll)    │    │              │
└──────────┘    └─────────────┘    └──────────────┘
```

The daemon opens evdev devices **read‑only** — it never grabs them. Original input events reach applications normally, while scroll events are injected through a separate virtual uinput device.

## License

MIT
