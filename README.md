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
- Optional (Meet tab): `gir1.2-webkit-6.0` — embedded WebKitGTK browser
- Optional (video recording): `ffmpeg` — writes H.264 `.mkv` files

## Installation

```bash
git clone https://github.com/samuele-brusegan/veyon-demo-viewer.git
cd veyon-demo-viewer
python3 install_deps.py        # TUI: seleziona e installa ciò che manca
python3 veyondemo_gui.py
```

`install_deps.py` detects the package manager (apt/dnf/pacman), shows
each dependency with its installed/missing state, pre-selects what's
missing and installs the selection via `sudo`. Use `--list` for a
non-interactive status check.

Manual install, per distro:

```bash
# Debian / Ubuntu / Kali
sudo apt install python3-gi gir1.2-gtk-4.0 gir1.2-adw-1 python3-numpy \
    gir1.2-gst-plugins-base-1.0 gstreamer1.0-pipewire xdg-desktop-portal \
    libx11-6 ffmpeg gir1.2-webkit-6.0

# Fedora (ffmpeg requires RPM Fusion)
sudo dnf install python3-gobject gtk4 libadwaita python3-numpy \
    gstreamer1-plugins-base pipewire-gstreamer xdg-desktop-portal \
    libX11 ffmpeg webkitgtk6.0

# Arch
sudo pacman -S python-gobject gtk4 libadwaita python-numpy \
    gst-plugins-base gst-plugin-pipewire xdg-desktop-portal \
    libx11 ffmpeg webkitgtk-6.0
```

Only GTK4/libadwaita/PyGObject and NumPy are strictly required — the
other packages enable Wayland screen capture (PipeWire + portal), the
Meet tab (WebKitGTK) and video recording (ffmpeg). On X11 sessions
(XFCE, LXDE) screen capture works without PipeWire/portal.

## Meet / Google Classroom

The **Meet** tab embeds a dedicated browser window (WebKitGTK 6.0,
persistent profile in `~/.local/share/veyon-demo-viewer/webkit`):

1. **Apri** opens `classroom.google.com` — log in to Google there
   (the session persists across restarts).
2. **Aggiorna corsi** reads the course list from the open Classroom
   page; double-clicking a course finds its Meet link and joins it.
3. The meeting stays connected until you click **Esci** (or close the
   Meet window). Auto-join clicks "Partecipa"/"Join now" for you.
4. **Link Meet diretto** joins any `meet.google.com/…` URL, including
   as a guest without login.
5. The participants panel lists names scraped from the Meet DOM —
   pinning a specific stream is done in Meet's own UI.
6. **Registra riunione** records only the Meet window to `.mkv`:
   on X11 via `ffmpeg -f x11grab -window_id`, on Wayland via the
   ScreenCast portal (pick the Meet window in the system dialog).

Note: Google sign-in inside embedded browsers can occasionally be
refused depending on the account/2FA policy — if that happens, join
via a direct Meet link as guest, which needs no login.

## Screen recording

Settings → **Salva su file** records the local screen to
`~/Video/veyon-rec-*.mkv` (H.264). On X11 it uses `ffmpeg -f x11grab`;
on Wayland it goes through the ScreenCast portal (consent dialog) and
pipes frames into ffmpeg.

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
- This is an interoperability client: it receives and displays
  presentations. Your local screen is shared only if you enable
  «Inviare al master/docente» or accept a share request (otherwise the
  master sees a black framebuffer).

## License

MIT — see [LICENSE](LICENSE).
