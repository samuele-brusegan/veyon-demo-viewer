"""master_client.py - connessione a un veyon-service (:11100) come master.

Handshake Veyon lato client + auth KeyFile (RSA PKCS1v15/SHA-512, come
QCA EMSA3_SHA512) o Logon (password RSA-OAEP/SHA-1, come QCA
EME_PKCS1_OAEP) o Token (client custom nostro). Poi monitoring: FBUR in
loop e decode dei rect (solo Raw/CopyRect, richiesti via SetEncodings).

Credenziali persistite in ~/.local/share/veyon-demo-viewer/settings.json.
"""

import os
import socket
import struct
import sys
import threading
import time

import numpy as np

import veyon_proto as vp

SETTINGS_PATH = os.path.expanduser(
    "~/.local/share/veyon-demo-viewer/settings.json")


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


# ------------------------------------------------------------------ creds

class Credentials:
    """Credenziali master da settings.json."""

    def __init__(self, path=SETTINGS_PATH):
        import json
        try:
            with open(path) as f:
                d = json.load(f)
        except Exception:
            d = {}
        self.key_file = d.get("key_file", "")
        self.key_name = d.get("key_name", "")
        self.username = d.get("username", "")
        self.password = d.get("password", "")
        self._pkey = None

    def private_key(self):
        """Chiave configurata, altrimenti effimera (basta per i client
        custom che non verificano la firma; su quelli reali l'auth
        fallira' come previsto)."""
        if self._pkey is None:
            from cryptography.hazmat.primitives.asymmetric import rsa
            from cryptography.hazmat.primitives.serialization import \
                load_pem_private_key
            if self.key_file:
                with open(self.key_file, "rb") as f:
                    self._pkey = load_pem_private_key(f.read(),
                                                      password=None)
            else:
                self._pkey = rsa.generate_private_key(65537, 2048)
        return self._pkey


# ------------------------------------------------------------- client conn

class ClientService(threading.Thread):
    """Thread: un veyon-service remoto, handshake + monitoring dei frame.

    on_status(ip, text) e on_frame(ip, fb) chiamati dal thread (usare
    GLib.idle_add lato GUI).
    """

    def __init__(self, ip, creds, on_status, on_frame, on_chat=None,
                 on_ident=None, port=11100):
        super().__init__(daemon=True, name=f"master-{ip}")
        self.ip = ip
        self.port = port
        self.creds = creds
        self.on_status = on_status
        self.on_frame = on_frame
        self.on_chat = on_chat          # (ip, nome, testo) o None
        self.on_ident = on_ident        # (ip, info|None) o None
        self.on_share = None            # (ip, accepted: bool) — risposta share
        self.fb = None
        self.sock = None
        self.stop_flag = False
        self.ident_info = None               # dict {name, ver} se custom
        self._ident_gen = 0             # invalida timer di connessioni vecchie
        self.share_state = lambda: False    # -> bool, per il nostro pong

    # ---- handshake ---------------------------------------------------------

    def handshake(self):
        s = self.sock
        ver = recv_exactly(s, 12)
        if not ver.startswith(b"RFB "):
            raise RuntimeError(f"non e' RFB: {ver!r}")
        s.sendall(vp.RFB_VERSION)
        n = recv_exactly(s, 1)[0]
        types = recv_exactly(s, n)
        if vp.SEC_TYPE_VEYON not in types:
            raise RuntimeError("security type Veyon non offerto")
        s.sendall(bytes([vp.SEC_TYPE_VEYON]))

        vals = vp.decode_all(recv_vam(s))
        offered = [int(v) for v in vals[1:]]
        if self.creds.password and vp.AUTH_LOGON in offered:
            at = vp.AUTH_LOGON
        elif vp.AUTH_KEYFILE in offered:
            at = vp.AUTH_KEYFILE
        elif vp.AUTH_TOKEN in offered:
            at = vp.AUTH_TOKEN
        else:
            raise RuntimeError(
                f"nessun metodo auth usabile (offerti {offered})")
        s.sendall(vp.pack_vam(at, self.creds.username or ""))
        recv_vam(s)                                # auth ack

        if at == vp.AUTH_KEYFILE:
            from cryptography.hazmat.primitives import hashes
            from cryptography.hazmat.primitives.asymmetric import padding
            chal = bytes(vp.decode_all(recv_vam(s))[0])
            sig = self.creds.private_key().sign(
                chal, padding.PKCS1v15(), hashes.SHA512())
            s.sendall(vp.pack_vam(self.creds.key_name or "", sig))
        elif at == vp.AUTH_LOGON:
            from cryptography.hazmat.primitives import hashes
            from cryptography.hazmat.primitives.asymmetric import padding
            from cryptography.hazmat.primitives.serialization import \
                load_pem_public_key
            pem = bytes(vp.decode_all(recv_vam(s))[0])
            pub = load_pem_public_key(pem)
            enc = pub.encrypt(
                self.creds.password.encode(),
                padding.OAEP(mgf=padding.MGF1(hashes.SHA1()),
                             algorithm=hashes.SHA1(), label=None))
            s.sendall(vp.pack_vam(enc))
        else:                                    # Token (nostro client)
            s.sendall(vp.pack_vam(b""))

        (res,) = struct.unpack(">I", recv_exactly(s, 4))
        if res != vp.AUTH_OK:
            raise RuntimeError(f"auth fallita (result={res})")

        # euristica ident: un Veyon reale verificherebbe la firma con
        # una chiave registrata — se ha accettato la nostra chiave
        # effimera e' quasi certamente un nostro client custom
        if at == vp.AUTH_KEYFILE and not self.creds.key_file:
            self.ident_info = {"name": vp.APP_NAME, "ver": "?"}
            if self.on_ident:
                self.on_ident(self.ip, self.ident_info)

        s.sendall(b"\x01")                         # ClientInit
        init = recv_exactly(s, 24)
        w, h = struct.unpack(">HH", init[:4])
        (nl,) = struct.unpack(">I", init[20:24])
        recv_exactly(s, nl)                        # desktop name
        self.fb = np.zeros((h, w, 3), dtype=np.uint8)

        pf = struct.pack(">BBBBHHHBBBxxx", 32, 24, 0, 1,
                         255, 255, 255, 16, 8, 0)
        s.sendall(bytes([vp.CMSG_SET_PIXEL_FORMAT]) + b"\x00" * 3 + pf)
        encs = [vp.ENC_RAW, vp.ENC_COPYRECT, vp.ENC_LAST_RECT]
        s.sendall(struct.pack(">BxH", vp.CMSG_SET_ENCODINGS, len(encs)) +
                  struct.pack(f">{len(encs)}I", *encs))
        self.request_update(False)

    def request_update(self, incremental):
        h, w = self.fb.shape[:2]
        self.sock.sendall(struct.pack(">BBHHHH", vp.CMSG_FB_UPDATE_REQ,
                                      int(incremental), 0, 0, w, h))

    def blit(self, x, y, w, h, data):
        n = w * h
        if len(data) // n == 4:      # 32bpp BGRX little-endian
            self.fb[y:y + h, x:x + w] = \
                np.frombuffer(data, np.uint8).reshape(h, w, 4)[:, :, [2, 1, 0]]
        elif len(data) // n == 3:
            self.fb[y:y + h, x:x + w] = \
                np.frombuffer(data, np.uint8).reshape(h, w, 3)

    def read_rects(self, n):
        s = self.sock
        for _ in range(n):
            x, y, rw, rh, enc = struct.unpack(">HHHHi", recv_exactly(s, 12))
            enc &= 0xFFFFFFFF
            if enc == vp.ENC_LAST_RECT:
                return
            if enc == vp.ENC_RAW:
                self.blit(x, y, rw, rh, recv_exactly(s, rw * rh * 4))
            elif enc == vp.ENC_COPYRECT:
                sx, sy = struct.unpack(">HH", recv_exactly(s, 4))
                self.fb[y:y + rh, x:x + rw] = \
                    self.fb[sy:sy + rh, sx:sx + rw]
            elif enc in (vp.ENC_SUPPORTED_MESSAGES,
                         vp.ENC_SUPPORTED_ENCODINGS):
                recv_exactly(s, rw)
            elif enc == vp.ENC_NEW_FB_SIZE:
                new = np.zeros((rh, rw, 3), np.uint8)
                oh, ow = min(rh, self.fb.shape[0]), min(rw, self.fb.shape[1])
                new[:oh, :ow] = self.fb[:oh, :ow]
                self.fb = new
            else:
                raise RuntimeError(f"monitoring: encoding {enc:#x} non gestito")

    def serve(self):
        s = self.sock
        while not self.stop_flag:
            mtype = recv_exactly(s, 1)[0]
            if mtype == vp.MSG_FB_UPDATE:
                (n,) = struct.unpack(">xH", recv_exactly(s, 3))
                self.read_rects(n)
                self.on_frame(self.ip, self.fb)
                self.request_update(True)
            elif mtype == vp.MSG_SET_COLOURMAP:
                (nc,) = struct.unpack(">xHH", recv_exactly(s, 5))
                recv_exactly(s, nc * 6)
            elif mtype == vp.MSG_BELL:
                pass
            elif mtype == vp.MSG_SERVER_CUT_TEXT:
                hdr = recv_exactly(s, 7)
                (ln,) = struct.unpack(">I", hdr[3:7])
                recv_exactly(s, ln)
            elif mtype == vp.MSG_RESIZE_FB:
                w, h = struct.unpack(">HH", recv_exactly(s, 4))
                new = np.zeros((h, w, 3), np.uint8)
                oh, ow = min(h, self.fb.shape[0]), min(w, self.fb.shape[1])
                new[:oh, :ow] = self.fb[:oh, :ow]
                self.fb = new
            elif mtype == vp.MSG_FEATURE:
                payload = recv_vam(s)
                try:
                    fm = vp.FeatureMessage.parse(payload)
                except Exception:
                    continue
                if fm.uid != vp.UID_CHAT:
                    continue
                if fm.command == vp.CMD_CHAT_MSG and self.on_chat:
                    self.on_chat(
                        self.ip,
                        str(fm.args.get(vp.ARG_CHAT_FROM) or ""),
                        str(fm.args.get(vp.ARG_CHAT_TEXT) or ""))
                elif fm.command == vp.CMD_CHAT_PING:
                    # il service remoto ci chiede l'identita'
                    self.send_feature(vp.FeatureMessage(
                        vp.UID_CHAT, vp.CMD_CHAT_PONG,
                        {vp.ARG_IDENT_NAME: vp.APP_NAME,
                         vp.ARG_IDENT_VER: vp.APP_VERSION,
                         vp.ARG_IDENT_SHARE:
                             1 if self.share_state() else 0}))
                elif fm.command == vp.CMD_CHAT_SHARE_REPLY:
                    if self.on_share:
                        self.on_share(self.ip, bool(int(
                            fm.args.get(vp.ARG_SHARE_ACCEPTED) or 0)))
                elif fm.command == vp.CMD_CHAT_PONG:
                    self.ident_info = {
                        "name": str(fm.args.get(vp.ARG_IDENT_NAME) or "?"),
                        "ver": str(fm.args.get(vp.ARG_IDENT_VER) or "?"),
                        "share": bool(int(
                            fm.args.get(vp.ARG_IDENT_SHARE) or 0))}
                    if self.on_ident:
                        self.on_ident(self.ip, self.ident_info)
            else:
                raise RuntimeError(f"msg type {mtype} sconosciuto")

    # ---- feature messages (NON inviare demo-start: vedi nota in GUI) --------

    def send_feature(self, fm):
        if self.sock:
            self.sock.sendall(fm.serialize_rfb())

    def send_pointer(self, mask, x, y):
        if self.sock:
            self.sock.sendall(struct.pack(">BBHH", 5, mask, x, y))

    def send_key(self, down, keysym):
        if self.sock:
            self.sock.sendall(struct.pack(">BBxxI",
                                          4, int(down), keysym))

    # ---- main loop ----------------------------------------------------------

    def _ident_timeout(self, gen):
        if gen == self._ident_gen and self.ident_info is None and self.on_ident:
            self.on_ident(self.ip, None)         # nessun pong: veyon reale

    def run(self):
        while not self.stop_flag:
            self.ident_info = None
            self._ident_gen += 1
            gen = self._ident_gen
            try:
                self.sock = socket.create_connection((self.ip, self.port), 5)
                self.sock.settimeout(None)
                self.handshake()
                self.on_status(self.ip, "connesso")
                # ident ping: pong atteso entro 3s, altrimenti "reale"
                self.send_feature(vp.FeatureMessage(
                    vp.UID_CHAT, vp.CMD_CHAT_PING, {}))
                threading.Timer(3.0, self._ident_timeout,
                                args=(gen,)).start()
                self.serve()
            except Exception as e:
                self.on_status(self.ip, f"errore: {e}")
            finally:
                if self.sock:
                    try:
                        self.sock.close()
                    except Exception:
                        pass
                    self.sock = None
            for _ in range(50):                    # retry tra 5s, uscita rapida
                if self.stop_flag:
                    return
                time.sleep(0.1)


# ------------------------------------------------------------------- scan

def scan_subnet(port, on_hit, timeout=0.35, workers=64):
    """Scansione /24 della subnet locale su :port; on_hit(ip) per i rispondenti."""
    import concurrent.futures
    net = None
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        parts = ip.split(".")
        net = ".".join(parts[:3])
    except OSError:
        pass
    if not net:
        return

    def probe(i):
        host = f"{net}.{i}"
        try:
            s = socket.create_connection((host, port), timeout)
            s.close()
            on_hit(host)
        except OSError:
            pass

    with concurrent.futures.ThreadPoolExecutor(workers) as ex:
        list(ex.map(probe, range(1, 255)))


def wol(mac):
    """Wake-on-LAN magic packet su broadcast :9."""
    mac = mac.replace(":", "").replace("-", "")
    pkt = b"\xff" * 6 + bytes.fromhex(mac) * 16
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    s.sendto(pkt, ("255.255.255.255", 9))
    s.close()
