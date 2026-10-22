"""demo_viewer.py - viewer per il demo server Veyon.

Si connette a host:porta con auth a token e mostra lo schermo condiviso in
una finestra GTK4 (minimizzabile, mai fullscreen).

Uso: python3 demo_viewer.py <host> <porta> <token_hex>
"""

import socket
import struct
import sys
import threading

import numpy as np

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
from gi.repository import Gdk, GLib, Gtk

import veyon_proto as vp


def recv_exactly(sock, n):
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("connessione chiusa dal server")
        buf += chunk
    return bytes(buf)


def recv_vam(sock):
    (size,) = struct.unpack(">I", recv_exactly(sock, 4))
    return recv_exactly(sock, size)


class DemoRfbClient(threading.Thread):
    def __init__(self, host, port, token, app):
        super().__init__(daemon=True)
        self.host = host
        self.port = port
        self.token = token
        self.app = app
        self.sock = None
        self.fb = None              # numpy (h, w, 3) RGB
        self.fb_size = (0, 0)

    # -- handshake -----------------------------------------------------------

    def handshake(self):
        s = self.sock
        recv_exactly(s, 12)                      # server version
        s.sendall(vp.RFB_VERSION)
        n_types = recv_exactly(s, 1)[0]
        types = recv_exactly(s, n_types)
        if vp.SEC_TYPE_VEYON not in types:
            raise RuntimeError(f"security types non supportati: {types!r}")
        s.sendall(bytes([vp.SEC_TYPE_VEYON]))

        payload = recv_vam(s)                    # auth types list
        vals = vp.decode_all(payload)
        if vp.AUTH_TOKEN not in [int(v) for v in vals]:
            raise RuntimeError(f"demo server non offre Token auth: {vals!r}")
        s.sendall(vp.pack_vam(vp.AUTH_TOKEN, ""))
        recv_vam(s)                              # auth ack (vuota)
        s.sendall(vp.pack_vam(self.token))       # il token demo
        (result,) = struct.unpack(">I", recv_exactly(s, 4))
        if result != vp.AUTH_OK:
            raise RuntimeError(f"token rifiutato (result={result})")

        s.sendall(b"\x01")                       # ClientInit (shared)
        init = recv_exactly(s, 24)
        w, h = struct.unpack(">HH", init[:4])
        (name_len,) = struct.unpack(">I", init[20:24])
        recv_exactly(s, name_len)
        self.fb_size = (w, h)
        self.fb = np.zeros((h, w, 3), dtype=np.uint8)
        GLib.idle_add(self.app.set_size, w, h)

        # pixel format: 24bpp, LE, truecolour, r/g/bshift -> bytes R,G,B
        pf = struct.pack(">BBBBHHHBBBxxx", 24, 24, 0, 1,
                         255, 255, 255, 0, 8, 16)
        s.sendall(bytes([vp.CMSG_SET_PIXEL_FORMAT]) + b"\x00" * 3 + pf)
        s.sendall(struct.pack(">BxH", vp.CMSG_SET_ENCODINGS, 2) +
                  struct.pack(">2i", vp.ENC_RAW, vp.ENC_COPYRECT))
        self.request_update(False)

    def request_update(self, incremental):
        w, h = self.fb_size
        self.sock.sendall(struct.pack(">BBHHHH", vp.CMSG_FB_UPDATE_REQ,
                                      int(incremental), 0, 0, w, h))

    # -- decode rects --------------------------------------------------------

    def read_rects(self, n):
        s = self.sock
        w, h = self.fb_size
        for _ in range(n):
            x, y, rw, rh, enc = struct.unpack(">HHHHi", recv_exactly(s, 12))
            enc &= 0xFFFFFFFF
            if enc == vp.ENC_LAST_RECT:
                return
            if enc == vp.ENC_RAW:
                data = recv_exactly(s, rw * rh * 3)
                self.fb[y:y + rh, x:x + rw] = \
                    np.frombuffer(data, np.uint8).reshape(rh, rw, 3)
            elif enc == vp.ENC_COPYRECT:
                sx, sy = struct.unpack(">HH", recv_exactly(s, 4))
                self.fb[y:y + rh, x:x + rw] = self.fb[sy:sy + rh, sx:sx + rw]
            elif enc in (0xFFFFFFB1, 0xFFFFFFB2):      # SupportedMessages/Encodings
                recv_exactly(s, rw)                    # w = byte count
            elif enc in (0xFFFFFF21, 0xFFFFFF15):      # DesktopSize variants
                self.resize_fb(rw, rh)
            else:
                raise RuntimeError(f"encoding non gestito: {enc:#x} "
                                   f"rect {x},{y} {rw}x{rh}")

    def resize_fb(self, w, h):
        new = np.zeros((h, w, 3), dtype=np.uint8)
        oh = min(h, self.fb.shape[0])
        ow = min(w, self.fb.shape[1])
        new[:oh, :ow] = self.fb[:oh, :ow]
        self.fb = new
        self.fb_size = (w, h)
        GLib.idle_add(self.app.set_size, w, h)

    # -- main loop -----------------------------------------------------------

    def serve(self):
        s = self.sock
        while True:
            mtype = recv_exactly(s, 1)[0]
            if mtype == vp.MSG_FB_UPDATE:
                (n,) = struct.unpack(">xH", recv_exactly(s, 3))
                self.read_rects(n)
                GLib.idle_add(self.app.refresh, self.fb)
                self.request_update(True)
            elif mtype == vp.MSG_SET_COLOURMAP:
                hdr = recv_exactly(s, 5)
                (nc,) = struct.unpack(">xHH", hdr)
                recv_exactly(s, nc * 6)
            elif mtype == vp.MSG_BELL:
                pass
            elif mtype == vp.MSG_SERVER_CUT_TEXT:
                hdr = recv_exactly(s, 7)
                (ln,) = struct.unpack(">I", hdr[3:7])
                recv_exactly(s, ln)
            elif mtype == vp.MSG_RESIZE_FB:
                w, h = struct.unpack(">HH", recv_exactly(s, 4))
                self.resize_fb(w, h)
            elif mtype == vp.MSG_FEATURE:
                recv_vam(s)
            else:
                raise RuntimeError(f"msg type sconosciuto {mtype}")

    def run(self):
        import time
        while not self.app.closed:
            try:
                self.sock = socket.create_connection((self.host, self.port), 10)
                self.sock.settimeout(None)
                self.handshake()
                GLib.idle_add(self.app.set_status, "connesso")
                self.serve()
            except Exception as e:
                GLib.idle_add(self.app.set_status, f"disconnesso: {e} — riprovo")
            finally:
                if self.sock:
                    try:
                        self.sock.close()
                    except Exception:
                        pass
                    self.sock = None
            time.sleep(2)


class DemoApp(Gtk.Application):
    def __init__(self):
        super().__init__(application_id="dev.local.veyon-demo-viewer")
        self.picture = None
        self.win = None
        self.closed = False

    def do_activate(self):
        self.win = Gtk.ApplicationWindow(application=self, title="Veyon Demo")
        self.win.set_default_size(960, 600)
        self.win.connect("close-request", self._on_close)
        self.picture = Gtk.Picture()
        self.picture.set_can_shrink(True)
        self.win.set_child(self.picture)
        self.win.present()

    def _on_close(self, *_):
        self.closed = True
        return False

    def set_size(self, w, h):
        self.win.set_default_size(min(w, 1600), min(h, 1000))

    def set_status(self, text):
        self.win.set_title(f"Veyon Demo — {text}")

    def refresh(self, fb):
        h, w, _ = fb.shape
        data = GLib.Bytes.new(fb.tobytes())
        tex = Gdk.MemoryTexture.new(w, h, Gdk.MemoryFormat.R8G8B8,
                                    data, w * 3)
        self.picture.set_paintable(tex)


def main():
    if len(sys.argv) != 4:
        print(__doc__, file=sys.stderr)
        sys.exit(2)
    host, port, token_hex = sys.argv[1], int(sys.argv[2]), sys.argv[3]
    token = bytes.fromhex(token_hex)

    app = DemoApp()
    client = DemoRfbClient(host, port, token, app)
    client.start()
    app.run(sys.argv[:1])


if __name__ == "__main__":
    main()
