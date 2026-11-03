# veyon-demo-viewer <img src="assets/icon.svg" width="48" align="right">

Watch [Veyon](https://veyon.io) classroom demos (screen presentations) on a
Linux machine that **is not** an official Veyon client — without exposing your
own screen, and in a normal, resizable, minimizable window even when the
teacher presents fullscreen.

## How it works

Veyon demos are push-based: when a teacher starts a presentation, the Veyon
master sends a `StartDemoClient` feature command to every registered client,
containing the demo server address and a one-time access token. No client, no
command, no stream.

This project implements two parts:

- **fake-service** (`fake_service.py`) — a minimal Veyon-compatible endpoint
  listening on TCP 11100. It completes the RFB/Veyon handshake and
  authentication (KeyFile/Logon), answers the master's monitoring pings and
  builtin queries (user info, session info, screens, app version) so the
  machine appears as a normal online client with a black framebuffer, and
  intercepts `StartDemoClient`/`StopDemoClient` messages. It deliberately
  does *not* implement remote control, input locking, screen capture or any
  other client feature.
- **demo-viewer** (`demo_viewer.py`) — connects to the captured demo server
  (typically `host:11400`), authenticates with the intercepted token, decodes
  the broadcast RFB stream (Tight encoding: fill, JPEG, copy/palette/gradient
  filters, persistent zlib streams; plus raw/copyrect and UltraVNC
  pseudo-encodings) and renders it in a minimal, borderless, PiP-style GTK4
  window — always on top and on every workspace (via XWayland on Wayland
  sessions): drag it anywhere, `Esc`/`q` closes it. Resilient to demo-server
  resyncs and mid-stream joins; auto-reconnects.
- **daemon** (`veyon_daemon.py`) — a minimal background service: the fake
  service without GUI and without viewer. It captures `StartDemoClient`
  tokens and persists them to
  `~/.local/share/veyon-demo-viewer/demos.json`, so presentations caught
  while the GUI was closed are already there on first launch.
- **screen recording** (`screen_capture.py` + `demo_server.py`,
  two independent switches in *Impostazioni*, both **off by default**) —
  captures your screen via the XDG ScreenCast portal + PipeWire
  (GNOME asks consent once; the restore token is persisted).
  *Produzione demo* runs a RFB/Veyon demo server on `:11400` broadcasting
  your screen (token auth) so you can multicast it to clients;
  *Inviare al master* makes the fake service answer FBURs with your real
  screen instead of the black framebuffer — the master actually sees you.
- **master mode** (`master_client.py` + the *Master* page of the GUI,
  off by default — enable it from the switch in *Impostazioni*) —
  connects to other `veyon-service` instances on `:11100` like a real
  Veyon master (KeyFile RSA-PKCS1v15/SHA-512 or Logon RSA-OAEP/SHA-1 auth,
  configurable; Token fallback for custom clients), monitors their screens
  with live thumbnails (manual IP list or `/24` subnet scan) and offers
  per-client actions: screenshot, text message, screen lock/unlock,
  reboot, power down, Wake-on-LAN, remote control and `StartDemoClient`
  multicast to the selected clients. Invisible to official masters:
  connections go straight to each client, no registration in any
  computer list.

`veyondemo.py` ties them together on the command line; `veyondemo_gui.py`
provides an Adwaita control window that keeps the service running in the
background, shares the same token store, lists intercepted
presentations and auto-opens the viewer on each new demo. The demo
history shows capture date/time and is filtered by the current `/24`
network: demos captured on other LANs stay hidden (but not deleted)
until you are back on that network.

## Requirements

- Python 3.11+ with `gi` (PyGObject), GTK4, libadwaita, NumPy
- TCP connectivity to the Veyon master on port 11100 (your host must be
  registered in the master's computer list)
- GNOME, XFCE, or another GTK desktop; XFCE needs GTK4 and libadwaita runtime

The GUI keeps its Adwaita layout on XFCE. It uses XFCE's window controls,
registers desktop entries against each GTK application ID, and publishes the
matching `assets` SVG as the X11 window icon.

## Usage

GUI (recommended):

```bash
python3 veyondemo_gui.py
```

Keep it open or minimized — it captures every `StartDemoClient` and opens the
viewer automatically.

Minimal background service (captures tokens even while the GUI is closed):

```bash
python3 veyon_daemon.py         # add & or run under your autostart/systemd
```

Command line:

```bash
python3 veyondemo.py            # or -v for protocol logs, -h for narration
```

Manual viewer launch (if you already have a token):

```bash
python3 demo_viewer.py <host> <port> <token_hex>
```

## Tests

```bash
python3 test_e2e.py             # mock master + mock demo server, 16 checks
```

## Notes

- See `docs/protocol.md` for the reverse-engineered wire format (RFB, Veyon
  security type 40, VariantArrayMessage, FeatureMessage, Tight details and
  the demo server's synchronization behavior).
- The viewer never forces fullscreen: the demo's fullscreen/window flag is
  read for display purposes only.
- This is an interoperability client: it only receives and displays
  presentations. Your local screen is never shared (the master sees a black
  framebuffer).

## License

MIT — see [LICENSE](LICENSE).
