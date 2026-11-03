"""demo_server.py - demo server RFB/Veyon su :11400.

Trasmette i frame di get_frame() (np RGB, es. ScreenCapture) ai
demo-client connessi: handshake Veyon con auth Token (il token arriva ai
client dentro StartDemoClient), poi per ogni FBUR risponde con un rect
Tight copy+zlib del frame corrente scalato.

Uso:
    srv = DemoServer(11400, cap.get_frame, token=b"\x01\x02...")
    srv.start()
    srv.stop()
"""

import logging
import secrets
import socket
import struct
import threading
import zlib

import numpy as np

import veyon_proto as vp

log = logging.getLogger("demo_server")

OUT_W = 1024                       # larghezza max stream


def new_token():
    return secrets.token_bytes(16)


def fit_frame(fb, w=OUT_W):
    """Scala np (h,w,3) a larghezza max w conservando l'aspect."""
    if fb is None:
        return np.zeros((576, w, 3), np.uint8)
    oh, ow = fb.shape[:2]
    step = max(1, int(np.ceil(ow / w)))
    return fb[::step, ::step]


def _compact_len(n):
    if n < 0x80:
        return bytes([n])
    if n < 0x4000:
        return bytes([0x80 | (n & 0x7F), n >> 7])
    return bytes([0x80 | (n & 0x7F), 0x80 | ((n >> 7) & 0x7F), n >> 14])


def encode_fbu(fb):
    """FBU con un rect Tight copy+zlib dell'intero frame."""
    h, w = fb.shape[:2]
    payload = zlib.compress(fb.tobytes(), 3)
    rect = struct.pack(">HHHHi", 0, 0, w, h, vp.ENC_TIGHT) + \
        b"\x01" + _compact_len(len(payload)) + payload
    return struct.pack(">BxH", vp.MSG_FB_UPDATE, 1) + rect


def _read_vam(sock):
    (n,) = struct.unpack(">I", _recv(sock, 4))
    return _recv(sock, n)


def _recv(sock, n):
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("peer chiuso")
        buf += chunk
    return bytes(buf)


class DemoServer:
    def __init__(self, port, get_frame, token, on_status=None):
        self.port = port
        self.get_frame = get_frame
        self.token = token
        self.on_status = on_status or (lambda t: log.info("%s", t))
        self.clients = 0
        self._lsock = None
        self._stop = threading.Event()
        self._thread = None

    # -- API -------------------------------------------------------------

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._accept_loop,
                                        daemon=True, name="demo-server")
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._lsock:
            try:
                self._lsock.close()
            except OSError:
                pass
            self._lsock = None

    # -- server ------------------------------------------------------------

    def _accept_loop(self):
        try:
            self._lsock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self._lsock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self._lsock.bind(("0.0.0.0", self.port))
            self._lsock.listen(8)
            self.on_status(f"demo server in ascolto su :{self.port}")
            while not self._stop.is_set():
                try:
                    cs, addr = self._lsock.accept()
                except OSError:
                    break
                threading.Thread(target=self._serve, args=(cs, addr[0]),
                                 daemon=True).start()
        except Exception as e:
            self.on_status(f"demo server errore: {e}")
        finally:
            try:
                if self._lsock:
                    self._lsock.close()
            except OSError:
                pass

    def _serve(self, cs, ip):
        try:
            cs.settimeout(30)
            cs.sendall(vp.RFB_VERSION)
            if not _recv(cs, 12).startswith(b"RFB 003."):
                return
            cs.sendall(bytes([1, vp.SEC_TYPE_VEYON]))
            if _recv(cs, 1)[0] != vp.SEC_TYPE_VEYON:
                return
            cs.sendall(vp.pack_vam(1, vp.AUTH_TOKEN))
            vals = vp.decode_all(_read_vam(cs))
            if int(vals[0]) != vp.AUTH_TOKEN:
                return
            cs.sendall(vp.EMPTY_VAM)
            vals = vp.decode_all(_read_vam(cs))
            if bytes(vals[0]) != self.token:
                self.on_status(f"{ip}: token demo errato — rifiutato")
                return
            cs.sendall(struct.pack(">I", vp.AUTH_OK))
            _recv(cs, 1)                                     # ClientInit
            cs.settimeout(None)
            fb = fit_frame(self.get_frame())
            h, w = fb.shape[:2]
            cs.sendall(vp.build_server_init(w, h))
            self.clients += 1
            self.on_status(f"{ip}: demo client connesso "
                           f"({self.clients} totali)")
            while not self._stop.is_set():
                mtype = _recv(cs, 1)[0]
                if mtype == vp.CMSG_SET_PIXEL_FORMAT:
                    _recv(cs, 19)
                elif mtype == vp.CMSG_SET_ENCODINGS:
                    hdr = _recv(cs, 3)
                    (n,) = struct.unpack(">H", hdr[1:3])
                    _recv(cs, 4 * n)
                elif mtype == vp.CMSG_FB_UPDATE_REQ:
                    _recv(cs, 9)
                    cs.sendall(encode_fbu(fit_frame(self.get_frame())))
                elif mtype == vp.CMSG_KEY_EVENT:
                    _recv(cs, 7)
                elif mtype == vp.CMSG_POINTER_EVENT:
                    _recv(cs, 5)
                elif mtype == vp.CMSG_CLIENT_CUT_TEXT:
                    hdr = _recv(cs, 7)
                    (ln,) = struct.unpack(">I", hdr[3:7])
                    _recv(cs, ln)
                else:
                    return
        except (ConnectionError, OSError, struct.error):
            pass
        except Exception:
            log.exception("demo client %s: errore", ip)
        finally:
            self.clients = max(0, self.clients - 1)
            try:
                cs.close()
            except OSError:
                pass
