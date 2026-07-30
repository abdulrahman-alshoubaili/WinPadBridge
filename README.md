# WinPadBridge

Turn any Windows handheld (ROG Ally, Legion Go, GPD Win, AYANEO, or similar)
into a wireless (or wired) controller for another Windows PC. Built to solve
a real problem: playing PS2 games in **PCSX2** on a PC after forgetting a
real controller at home — using the handheld's own built-in gamepad instead.

No installer, no drivers to hunt down beyond one official one, no command
line required to run it day-to-day — both apps are plain windowed Python
programs.

## How it works

The handheld and the PC are both full Windows PCs — and two PCs can't
plug a controller into each other directly, since neither can "pretend"
to be a USB peripheral. WinPadBridge works around that by streaming the
handheld's gamepad state to the other PC over a link, where it's turned
into a real virtual Xbox 360 controller that any game (PCSX2 included)
can use.

```
Handheld                                      PC
built-in gamepad (XInput)                     virtual Xbox 360 pad (ViGEmBus)
        |                                             ^
sender_gui.pyw  --  Bluetooth or USB cable  -->  receiver_gui.pyw
                                                              |
                                                              v
                                                        PCSX2 / any game
```

Two connection methods are supported, selectable in the sender's UI:

- **Bluetooth** (default) — pair the two devices once in Windows
  Settings, no network required, unaffected by firewalls.
- **USB cable** — needs a USB-C **data-link/bridge cable** (a
  "PC-to-PC transfer cable"), not a plain charging cable. Windows turns
  it into a small virtual network adapter; the app auto-discovers the
  receiver PC over it, so no manual IP entry is needed.

Both use a lightweight custom handshake/streaming protocol (not a
standard HID/gamepad Bluetooth profile), since a Windows PC cannot
natively act as a Bluetooth gamepad peripheral.

## Requirements

**Receiver PC:**
- Windows 10/11, Python 3.11+
- [ViGEmBus driver](https://github.com/nefarius/ViGEmBus/releases) (one-time install — creates the virtual controller)
- `pip install vgamepad` (the receiver can also install this for you from its own UI)

**Handheld (sender):**
- Windows 10/11 (or Windows-based handheld OS), Python 3.11+
- No extra packages — standard library only

## Running it

1. **Receiver PC:** open `receiver_gui.pyw`. It listens on both Bluetooth
   and USB cable at the same time.
2. **Handheld:** make sure the controls are in **Gamepad mode**, not
   Desktop/Mouse mode (Command Center button, Armoury Crate, or your
   device's equivalent → Control Mode → Gamepad). In Desktop mode the
   sticks act as a mouse and won't be seen as gamepad input by any app.
3. Open `sender_gui.pyw`, pick Bluetooth or USB cable, press **Start**.
4. Once connected, open PCSX2 (or any game) →
   Settings → Controllers → Automatic Mapping → pick the Xbox 360
   controller.

Both apps include a live controller-test view and step-by-step help
built into the UI, so most setup issues are diagnosed on-screen.

## Project background

Built iteratively through real hands-on debugging: starting from a
terminal-only prototype, through a GUI rebuild, discovering the
Desktop/Mouse-mode trap on Windows handhelds, adding Bluetooth after
Wi-Fi/firewall issues, fixing a Bluetooth-constant naming bug
(`AF_BTH` vs `AF_BLUETOOTH`), fixing a bufferbloat latency bug from an
over-aggressive send rate, and finally adding the USB cable option as an
alternative to Bluetooth.

## License

MIT — do whatever you like with it.

## Disclaimer

This project only *reads* controller input and streams it as data; it
does not modify system security settings. Use official drivers
(ViGEmBus) from their original source.
