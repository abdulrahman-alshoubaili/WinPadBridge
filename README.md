# WinPadBridge

Turn any Windows handheld (ROG Ally, Legion Go, GPD Win, AYANEO, or similar) into a wireless controller for another Windows PC — for example, to play PS2 games in PCSX2 on your desktop when you forgot your DualSense at home, or to use the handheld as a spare pad for any other Windows game.

## Why USB / Bluetooth alone doesn't work

A Windows handheld is a full PC, just like the receiving PC. Both are USB and Bluetooth *hosts*, and a controller is a *peripheral* — a PC cannot present itself as a gamepad over those connections. So WinPadBridge goes over the network instead:

```
Handheld (sender)                       PC (receiver)
built-in gamepad (XInput)               virtual Xbox 360 pad (ViGEmBus)
        |                                       ^
     sender.py    --- UDP over Wi-Fi --->   receiver.py   ---> PCSX2 / any game
```

Two ways to run it:
- **GUI** (`sender_gui.pyw` / `receiver_gui.pyw`) — double-click, no terminal, includes auto-discovery of the receiver PC and a live controller test view.
- **Command line** (`sender.py` / `receiver.py`) — plain scripts, same protocol, useful for scripting or headless setups.

## Requirements

Both devices: Windows 10/11 and Python 3.11 or newer (3.11+ gives accurate high-rate timing). The receiver PC additionally needs the ViGEmBus driver and the `vgamepad` package. The handheld needs nothing beyond Python.

## Setup — Receiver PC

1. Install Python 3.11+ from python.org or the Microsoft Store.
2. Install the ViGEmBus driver: download `ViGEmBus_x64.exe` from https://github.com/nefarius/ViGEmBus/releases and run it.
3. `pip install vgamepad` (if a driver installer window pops up during this, accept it — or use the "Install vgamepad" button in the GUI).
4. On first run, Windows Firewall will ask about Python — allow it on **both private and public** networks. Or run this once in an admin terminal:
   `netsh advfirewall firewall add rule name="WinPadBridge" dir=in action=allow protocol=UDP localport=47845`

## Setup — Handheld (sender)

1. Install Python 3.11+. That's it — the sender uses only the standard library.

## Running it

1. Put both devices on the same network. **Away from home:** on the receiver PC, turn on Settings → Network & internet → Mobile hotspot, then connect the handheld to that hotspot. In hotspot mode the receiver's IP is usually `192.168.137.1` (check with `ipconfig`).
2. On the receiver PC: `python receiver.py` (or double-click `receiver_gui.pyw`)
3. On the handheld (in desktop mode): `python sender.py 192.168.137.1` (use the receiver's IP, or use `sender_gui.pyw`'s "Find receiver PC" button)
4. The receiver PC now has an "Xbox 360 Controller" — any game or emulator that supports Xbox controllers will pick it up. In PCSX2, for example: Settings → Controllers → Controller Port 1 → Automatic Mapping → pick the Xbox 360 pad. Done — play.

## Troubleshooting

- **Receiver gets nothing:** almost always Windows Firewall. Allow Python on private *and* public networks (hotspot networks often count as public), and confirm the IP with `ipconfig`.
- **Sender says "Controller 0 not detected":** set the handheld's controls to Gamepad/XInput mode (Armoury Crate / Command Center or your device's equivalent), and exit any game running on the handheld.
- **Feels laggy:** use the receiver's Mobile hotspot (direct link) instead of a crowded public Wi-Fi. At 250 Hz over a direct link, added delay is only a few milliseconds.
- **Link drops mid-game:** the receiver automatically releases all buttons after 0.5 s so nothing gets stuck.

## Roadmap ideas

DS4 mode (`vg.VDS4Gamepad`) for apps that want a PlayStation pad; rumble backchannel (ViGEmBus forwards vibration → send it back over UDP → `XInputSetState` on the handheld); gyro from the handheld's sensors mapped to DS4 motion; a packaged `.exe` (PyInstaller) so Python isn't required.

## Note on existing tools

Streaming apps (Moonlight/Sunshine, Parsec, Steam Remote Play) can also forward a controller, but they stream the whole screen and need more setup. WinPadBridge only sends input: tiny, offline-friendly, and fully ours.

License: MIT.
