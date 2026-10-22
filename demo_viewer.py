"""demo_viewer.py - viewer per il demo server Veyon.

Si connette a host:porta con auth a token e mostra lo schermo condiviso in
una finestra GTK4 (minimizzabile, mai fullscreen).

Uso: python3 demo_viewer.py <host> <porta> <token_hex>
"""

import socket
import struct
import sys
import threading
import time
import zlib

import numpy as np

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("GdkPixbuf", "2.0")
from gi.repository import Gdk, GdkPixbuf, GLib, Gtk

import veyon_proto as vp


def recv_exactly(sock, n):
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("connessione chiusa dal server")
        buf += chunk
    return bytes(buf)


def read_compact_len(sock):
    """Lunghezza compressa Tight: varint a 7 bit su 1-3 byte."""
    b = recv_exactly(sock, 1)[0]
    ln = b & 0x7F
    if b & 0x80:
        b = recv_exactly(sock, 1)[0]
        ln |= (b & 0x7F) << 7
        if b & 0x80:
            b = recv_exactly(sock, 1)[0]
            ln |= (b & 0xFF) << 14
    return ln


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
        self.tight_bad = [False] * 4
        self.t_conn = 0.0
        self._refresh_pending = False

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

        # pixel format dichiarato (il demo server lo ignora: broadcasta sempre
        # 32bpp depth24 R16/G8/B0 LE, vedi DemoServer::setVncServerPixelFormat)
        pf = struct.pack(">BBBBHHHBBBxxx", 32, 24, 0, 1,
                         255, 255, 255, 16, 8, 0)
        s.sendall(bytes([vp.CMSG_SET_PIXEL_FORMAT]) + b"\x00" * 3 + pf)
        encs = [vp.ENC_TIGHT, vp.ENC_ZYWRLE, vp.ENC_ZRLE, vp.ENC_ULTRA,
                vp.ENC_COPYRECT, vp.ENC_HEXTILE, vp.ENC_CORRE, vp.ENC_RRE,
                vp.ENC_RAW, vp.ENC_ZLIB,
                vp.ENC_NEW_FB_SIZE, vp.ENC_LAST_RECT]
        s.sendall(struct.pack(">BxH", vp.CMSG_SET_ENCODINGS, len(encs)) +
                  struct.pack(f">{len(encs)}I", *encs))
        # stream zlib persistenti del Tight (4 slot, resettabili dal server);
        # tight_bad = stream "avvelenato" (join a meta' stream): si saltano
        # i suoi chunk finche' l'encoder non emette un reset flag
        self.tight_zs = [zlib.decompressobj() for _ in range(4)]
        self.tight_bad = [False] * 4
        self.rects_seen = 0
        self.request_update(False)

    def request_update(self, incremental):
        w, h = self.fb_size
        self.sock.sendall(struct.pack(">BBHHHH", vp.CMSG_FB_UPDATE_REQ,
                                      int(incremental), 0, 0, w, h))

    # -- decode rects --------------------------------------------------------

    def blit(self, x, y, w, h, data):
        n = w * h
        per_px = len(data) // n
        if per_px == 3:          # 24bpp R,G,B come richiesto
            self.fb[y:y + h, x:x + w] = \
                np.frombuffer(data, np.uint8).reshape(h, w, 3)
        elif per_px == 4:        # 32bpp nativo del backend (BGRX little-endian)
            self.fb[y:y + h, x:x + w] = \
                np.frombuffer(data, np.uint8).reshape(h, w, 4)[:, :, [2, 1, 0]]
        else:
            raise RuntimeError(f"pixel data inattesa: {len(data)}B per {w}x{h}")

    def read_rects(self, n):
        s = self.sock
        w, h = self.fb_size
        for _ in range(n):
            x, y, rw, rh, enc = struct.unpack(">HHHHi", recv_exactly(s, 12))
            enc &= 0xFFFFFFFF
            if self.rects_seen < 8:
                print(f"  rect enc={enc:#x} {x},{y} {rw}x{rh}",
                      file=sys.stderr, flush=True)
            self.rects_seen += 1
            if enc == vp.ENC_LAST_RECT:
                return
            if enc == vp.ENC_RAW:
                data = recv_exactly(s, rw * rh * 4)
                self.blit(x, y, rw, rh, data)
            elif enc == vp.ENC_ZLIB:
                (ln,) = struct.unpack(">I", recv_exactly(s, 4))
                self.blit(x, y, rw, rh, zlib.decompress(recv_exactly(s, ln)))
            elif enc == vp.ENC_TIGHT:
                self.read_tight_rect(x, y, rw, rh)
            elif enc == vp.ENC_COPYRECT:
                sx, sy = struct.unpack(">HH", recv_exactly(s, 4))
                self.fb[y:y + rh, x:x + rw] = self.fb[sy:sy + rh, sx:sx + rw]
            elif enc in (vp.ENC_SUPPORTED_MESSAGES, vp.ENC_SUPPORTED_ENCODINGS):
                recv_exactly(s, rw)                    # w = byte count
            elif enc == vp.ENC_NEW_FB_SIZE:
                self.resize_fb(rw, rh)
            elif enc == vp.ENC_EXT_DESKTOP_SIZE:
                (ns,) = struct.unpack("B", recv_exactly(s, 1))
                recv_exactly(s, ns * 16)
                self.resize_fb(rw, rh)
            elif enc == vp.ENC_POINTER_POS:
                pass
            elif enc in (vp.ENC_XCURSOR, vp.ENC_RICH_CURSOR):
                # pixel 4B/px + bitmask 1bpp
                recv_exactly(s, rw * rh * 4 + (rw * rh + 7) // 8)
            elif 0xFFFFFFE0 <= enc <= 0xFFFFFFE9:
                pass                                 # quality level, info only
            else:
                raise RuntimeError(f"encoding non gestito: {enc:#x} "
                                   f"rect {x},{y} {rw}x{rh}")

    # -- Tight (encoding 7) ---------------------------------------------------

    def read_tight_rect(self, x, y, w, h):
        """Decode di un rect Tight (libvncclient tight.c, solo TPIXEL 24bit)."""
        s = self.sock
        ctl = recv_exactly(s, 1)[0]
        for i in range(4):
            if ctl & (1 << i):
                self.tight_zs[i] = zlib.decompressobj()
                self.tight_bad[i] = False
        ctl >>= 4
        uncompressed = False
        if (ctl & 0x0A) == 0x0A:                     # rfbTightNoZlib
            uncompressed = True
            ctl &= ~0x0A
        if ctl == 0x08:                              # rfbTightFill
            r, g, b = recv_exactly(s, 3)
            self.fb[y:y + h, x:x + w] = (r, g, b)
            return
        if ctl == 0x09:                              # rfbTightJpeg
            self.blit_jpeg(x, y, w, h,
                           recv_exactly(s, read_compact_len(s)))
            return
        if ctl > 0x0A:
            raise RuntimeError(f"tight: subencoding {ctl} non valido")

        mode, bits, palette = "copy", 24, None
        if ctl & 0x04:                               # filtro esplicito
            fid = recv_exactly(s, 1)[0]
            if fid == 1:                             # palette
                n = recv_exactly(s, 1)[0] + 1
                palette = np.frombuffer(recv_exactly(s, n * 3),
                                        np.uint8).reshape(n, 3).copy()
                mode, bits = "palette", 1 if n == 2 else 8
            elif fid == 2:
                mode = "gradient"
            elif fid != 0:
                raise RuntimeError(f"tight: filtro {fid} sconosciuto")

        row_size = (w * bits + 7) // 8
        size = row_size * h
        if size < 12:                                # TIGHT_MIN_TO_COMPRESS
            data = recv_exactly(s, size)
        else:
            payload = recv_exactly(s, read_compact_len(s))
            sid = ctl & 0x03
            if uncompressed:
                data = payload
            elif self.tight_bad[sid]:
                return           # chunk di meta' stream: non decodificabile
            else:
                try:
                    data = self.tight_zs[sid].decompress(payload)
                except zlib.error:
                    self.tight_bad[sid] = True
                    print(f"  tight: stream {sid} senza history "
                          f"(join a meta' stream), rect {x},{y} {w}x{h} saltato",
                          file=sys.stderr, flush=True)
                    GLib.idle_add(self.app.set_status,
                                  "connesso (stream parziale, attendo resync)")
                    return
        if len(data) < size:
            raise RuntimeError(f"tight: dati corti {len(data)}/{size}B")
        self.apply_tight_filter(mode, palette, bits, x, y, w, h, data[:size])

    def apply_tight_filter(self, mode, palette, bits, x, y, w, h, data):
        if mode == "copy":
            self.fb[y:y + h, x:x + w] = \
                np.frombuffer(data, np.uint8).reshape(h, w, 3)
        elif mode == "palette":
            if bits == 1:
                idx = np.unpackbits(
                    np.frombuffer(data, np.uint8).reshape(h, -1),
                    axis=1)[:, :w]
            else:
                idx = np.frombuffer(data, np.uint8).reshape(h, w)
            self.fb[y:y + h, x:x + w] = palette[idx]
        else:                                        # gradient
            src = np.frombuffer(data, np.uint8) \
                .reshape(h, w, 3).astype(np.int16)
            out = np.empty((h, w, 3), np.uint8)
            prev = np.zeros((w, 3), np.int16)
            for yy in range(h):
                row = np.empty((w, 3), np.int16)
                pix = (prev[0] + src[yy, 0]) & 0xFF
                row[0] = pix
                for xx in range(1, w):
                    est = np.clip(prev[xx] + pix - prev[xx - 1], 0, 255)
                    pix = (est + src[yy, xx]) & 0xFF
                    row[xx] = pix
                out[yy] = row
                prev = row
            self.fb[y:y + h, x:x + w] = out

    def blit_jpeg(self, x, y, w, h, data):
        loader = GdkPixbuf.PixbufLoader.new_with_type("jpeg")
        loader.write(data)
        loader.close()
        pb = loader.get_pixbuf()
        if pb is None:
            raise RuntimeError("tight: JPEG non decodificabile")
        ch, rs = pb.get_n_channels(), pb.get_rowstride()
        pw, ph = pb.get_width(), pb.get_height()
        raw = np.frombuffer(pb.get_pixels(), np.uint8)
        arr = np.lib.stride_tricks.as_strided(raw, (ph, pw, ch), (rs, ch, 1))
        self.fb[y:y + ph, x:x + pw] = arr[:, :, :3] if ch >= 3 \
            else np.repeat(arr[:, :, :1], 3, axis=2)

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
                self.frames += 1
                self.mark_dirty()
                self.request_update(True)
                # stream zlib avvelenati non guariscono da soli (l'encoder non
                # resetta mai): riconnettersi periodicamente da' la chance di
                # beccare fbuSeq>0 -> resync globale -> epoca con stream puliti
                if any(self.tight_bad) and \
                        time.monotonic() - self.t_conn > 20:
                    raise RuntimeError(
                        "stream zlib senza history: riprovo per resync")
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

    def mark_dirty(self):
        if not self._refresh_pending:
            self._refresh_pending = True
            GLib.idle_add(self._refresh_now)

    def _refresh_now(self):
        self._refresh_pending = False
        self.app.refresh(self.fb)

    def run(self):
        attempt = 0
        while not self.app.closed:
            try:
                attempt += 1
                print(f"[{attempt}] connect {self.host}:{self.port}", file=sys.stderr, flush=True)
                self.sock = socket.create_connection((self.host, self.port), 10)
                self.sock.settimeout(None)
                print("  handshake...", file=sys.stderr, flush=True)
                self.handshake()
                self.frames = 0
                self.t_conn = time.monotonic()
                print("  auth OK, streaming", file=sys.stderr, flush=True)
                GLib.idle_add(self.app.set_status, "connesso")
                self.serve()
            except Exception as e:
                note = " (resync del server: riavvio globale atteso)" \
                    if getattr(self, "frames", 0) == 0 else ""
                print(f"  errore: {e!r}{note}", file=sys.stderr, flush=True)
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
