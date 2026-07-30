# AllyPad

Turn a ROG (Xbox) Ally X into a wireless controller for another Windows PC — for example, to play PS2 games in PCSX2 on your laptop when you forgot your DualSense at home.

## Why USB / Bluetooth alone doesn't work

The Ally is a full Windows PC, just like the laptop. Both are USB and Bluetooth *hosts*, and a controller is a *peripheral* — a PC cannot present itself as a gamepad over those connections. So AllyPad goes over the network instead:

```
ROG Ally X                              Laptop
built-in gamepad (XInput)               virtual Xbox 360 pad (ViGEmBus)
        |                                       ^
  sender_ally.py  --- UDP over Wi-Fi --->  receiver_laptop.py ---> PCSX2 / any game
```

## Requirements

Both devices: Windows 10/11 and Python 3.11 or newer (3.11+ gives accurate high-rate timing). The laptop additionally needs the ViGEmBus driver and the `vgamepad` package. The Ally needs nothing beyond Python.

## Setup — Laptop (receiver)

1. Install Python 3.11+ from python.org or the Microsoft Store.
2. Install the ViGEmBus driver: download `ViGEmBus_x64.exe` from https://github.com/nefarius/ViGEmBus/releases and run it.
3. `pip install vgamepad` (if a driver installer window pops up during this, accept it).
4. On first run, Windows Firewall will ask about Python — allow it on **both private and public** networks. Or run this once in an admin terminal:
   `netsh advfirewall firewall add rule name="AllyPad" dir=in action=allow protocol=UDP localport=47845`

## Setup — Ally (sender)

1. Install Python 3.11+. That's it — the sender uses only the standard library.

## Running it

1. Put both devices on the same network. **Away from home:** on the laptop, turn on Settings → Network & internet → Mobile hotspot, then connect the Ally to that hotspot. In hotspot mode the laptop's IP is usually `192.168.137.1` (check with `ipconfig`).
2. On the laptop: `python receiver_laptop.py`
3. On the Ally (in desktop mode): `python sender_ally.py 192.168.137.1` (use your laptop's IP)
4. The laptop now has an "Xbox 360 Controller". In PCSX2: Settings → Controllers → Controller Port 1 → Automatic Mapping → pick the Xbox 360 pad. Done — play.

## Troubleshooting

- **Receiver gets nothing:** almost always Windows Firewall. Allow Python on private *and* public networks (hotspot networks often count as public), and confirm the IP with `ipconfig`.
- **Sender says "Controller 0 not detected":** set the Ally's controls to Gamepad mode in Armoury Crate / Command Center, and exit any game running on the Ally.
- **Feels laggy:** use the laptop's Mobile hotspot (direct link) instead of a crowded public Wi-Fi. At 250 Hz over a direct link, added delay is only a few milliseconds.
- **Link drops mid-game:** the receiver automatically releases all buttons after 0.5 s so nothing gets stuck.

## Roadmap ideas

DS4 mode (`vg.VDS4Gamepad`) for apps that want a PlayStation pad; rumble backchannel (ViGEmBus forwards vibration → send it back over UDP → `XInputSetState` on the Ally); gyro from the Ally's sensors mapped to DS4 motion; auto-discovery via UDP broadcast so you never type an IP; a small tray GUI and a packaged `.exe` (PyInstaller) so Python isn't required.

## Note on existing tools

Streaming apps (Moonlight/Sunshine, Parsec, Steam Remote Play) can also forward a controller, but they stream the whole screen and need more setup. AllyPad only sends input: tiny, offline-friendly, and fully ours.

Suggested license when publishing on GitHub: MIT.
