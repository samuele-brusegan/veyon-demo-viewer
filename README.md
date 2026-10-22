# veyon-demo-viewer

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
  pseudo-encodings) and renders it in a GTK4 window. Resilient to demo-server
  resyncs and mid-stream joins; auto-reconnects.

`veyondemo.py` ties them together on the command line; `veyondemo_gui.py`
provides an Adwaita control window that keeps the service running in the
background, stores every captured token persistently
(`~/.local/share/veyon-demo-viewer/demos.json`), lists intercepted
presentations and auto-opens the viewer on each new demo.

## Requirements

- Python 3.11+ with `gi` (PyGObject), GTK4, libadwaita, NumPy
- TCP connectivity to the Veyon master on port 11100 (your host must be
  registered in the master's computer list)

## Usage

GUI (recommended):

```bash
python3 veyondemo_gui.py
```

Keep it open or minimized — it captures every `StartDemoClient` and opens the
viewer automatically.

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
