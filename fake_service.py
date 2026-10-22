"""fake_service.py - finto veyon-service minimale su TCP 11100.

Implementa solo cio' che serve al master per vedere l'host come raggiungibile
e per inviare FeatureMessage. Intercetta i comandi demo e li inoltra a un
callback; ignora tutto il resto (lock, reboot, messaggi...).

Uso diretto:  python3 fake_service.py [--port 11100]
Oppure via veyondemo.py che spawna anche il viewer.
"""

import argparse
import asyncio
import getpass
import logging
import secrets
import socket
import struct

import veyon_proto as vp

log = logging.getLogger("fake_service")

AUTH_NAMES = {vp.AUTH_KEYFILE: "KeyFile", vp.AUTH_LOGON: "Logon", vp.AUTH_TOKEN: "Token"}


async def _read_exactly(reader, n):
    return await reader.readexactly(n)


async def read_vam(reader):
    """Legge una VariantArrayMessage, ritorna il payload grezzo."""
    (size,) = struct.unpack(">I", await _read_exactly(reader, 4))
    if size > 64 * 1024 * 1024:
        raise ValueError(f"VAM troppo grande: {size}")
    return await _read_exactly(reader, size)


def decode_vam(payload):
    try:
        return vp.decode_all(payload)
    except vp.VariantError as e:
        log.warning("VariantError: %s -- payload hex: %s", e, payload[:200].hex())
        # best-effort: tipo sconosciuto come primo elemento -> trattalo come int32
        type_id, _is_null = struct.unpack_from(">IB", payload, 0)
        if len(payload) >= 9 and type_id >= 1024:
            v = struct.unpack_from(">i", payload, 5)[0]
            log.info("guess: typeId %d come int32 = %d", type_id, v)
            try:
                rest, off = vp.decode_all(payload[9:])
                return [v] + rest
            except Exception:
                return [v]
        raise


class FakeServiceConnection:
    def __init__(self, reader, writer, ctx):
        self.reader = reader
        self.writer = writer
        self.ctx = ctx                      # shared ctx: queue eventi, cfg
        self.peer = writer.get_extra_info("peername")
        self.pix_bytes = 4                  # aggiornato da SetPixelFormat

    async def send(self, data):
        self.writer.write(data)
        await self.writer.drain()

    def log_rx(self, name, data):
        if self.ctx.verbose:
            log.debug("[%s] rx %s: %s", self.peer[0], name, data[:256].hex())

    # ---- fasi handshake -----------------------------------------------------

    async def handshake(self):
        await self.send(vp.RFB_VERSION)
        ver = await _read_exactly(self.reader, 12)
        if not ver.startswith(b"RFB 003."):
            raise ValueError(f"versione RFB non valida: {ver!r}")
        self.log_rx("version", ver)

        await self.send(bytes([1, vp.SEC_TYPE_VEYON]))
        sec = await _read_exactly(self.reader, 1)
        if sec[0] != vp.SEC_TYPE_VEYON:
            raise ValueError(f"security type rifiutato: {sec!r}")

        # lista auth type offerti: KeyFile + Logon (il master sceglie).
        # Verificato su rete reale: il servizio li manda come plain Int.
        await self.send(vp.pack_vam(2, vp.AUTH_KEYFILE, vp.AUTH_LOGON))

        payload = await read_vam(self.reader)
        vals = decode_vam(payload)
        auth_type = int(vals[0]) if vals else 0
        username = str(vals[1]) if len(vals) > 1 else ""
        log.info("[%s] auth type scelto: %s, user=%r", self.peer[0],
                 AUTH_NAMES.get(auth_type, auth_type), username)

        await self.send(vp.EMPTY_VAM)  # auth ack

        if auth_type == vp.AUTH_KEYFILE:
            await self.send(vp.pack_vam(secrets.token_bytes(vp.CHALLENGE_SIZE)))
            payload = await read_vam(self.reader)   # [keyName, signature]
            self.log_rx("keyfile-resp", payload)
        elif auth_type == vp.AUTH_LOGON:
            await self.send(vp.pack_vam(self.ctx.public_key_pem))
            payload = await read_vam(self.reader)   # [encrypted password]
            self.log_rx("logon-resp", payload)
        elif auth_type == vp.AUTH_TOKEN:
            payload = await read_vam(self.reader)
            self.log_rx("token", payload)
        else:
            log.warning("[%s] auth type %s non gestito, accetto comunque",
                        self.peer[0], auth_type)

        await self.send(struct.pack(">I", vp.AUTH_OK))

        # access control: approviamo subito, niente traffico
        await _read_exactly(self.reader, 1)          # ClientInit
        await self.send(vp.build_server_init(self.ctx.fb_width,
                                             self.ctx.fb_height))
        await self.send_async_feature_messages()

    # ---- loop running -------------------------------------------------------

    async def handle_feature(self):
        (size,) = struct.unpack(">I", await _read_exactly(self.reader, 4))
        payload = await _read_exactly(self.reader, size)
        try:
            fm = vp.FeatureMessage.parse(payload)
        except Exception as e:
            log.warning("[%s] FeatureMessage non parsabile: %s hex=%s",
                        self.peer[0], e, payload[:200].hex())
            return
        log.info("[%s] %s", self.peer[0], fm)

        reply = self.feature_reply(fm)
        if reply is not None:
            await self.send(reply)

        await self.ctx.events.put((fm, self.peer[0]))

    def feature_reply(self, fm):
        """Risposte alle feature builtin, come farebbe il vero servizio."""
        if fm.uid == vp.UID_MONITORING:
            # Ping: il vero servizio risponde con lo stesso messaggio
            return fm.serialize_rfb()
        if fm.uid == vp.UID_QUERY_APP_VERSION:
            return vp.FeatureMessage(fm.uid, 0,
                                     {vp.ARG_APPLICATION_VERSION: 9}
                                     ).serialize_rfb()
        if fm.uid == vp.UID_QUERY_ACTIVE_FEATURES:
            return vp.FeatureMessage(fm.uid, 0,
                                     {vp.ARG_ACTIVE_FEATURES: []}
                                     ).serialize_rfb()
        if fm.uid == vp.UID_QUERY_USER_INFO:
            return self.user_info_msg()
        if fm.uid == vp.UID_QUERY_SESSION_INFO:
            return self.session_info_msg()
        if fm.uid == vp.UID_QUERY_SCREENS:
            return self.screens_msg()
        return None

    def user_info_msg(self):
        user = self.ctx.username
        return vp.FeatureMessage(vp.UID_QUERY_USER_INFO, 0, {
            vp.ARG_USER_LOGIN_NAME: user,
            vp.ARG_USER_FULL_NAME: user,
        }).serialize_rfb()

    def session_info_msg(self):
        return vp.FeatureMessage(vp.UID_QUERY_SESSION_INFO, 0, {
            vp.ARG_SESSION_ID: 0,
            vp.ARG_SESSION_UPTIME: int(self.ctx.uptime()),
            vp.ARG_SESSION_HOSTNAME: self.ctx.hostname,
            vp.ARG_SESSION_CLIENT_ADDRESS: "",
            vp.ARG_SESSION_CLIENT_NAME: "",
            vp.ARG_SESSION_METADATA: "",
        }).serialize_rfb()

    def screens_msg(self):
        return vp.FeatureMessage(vp.UID_QUERY_SCREENS, 0, {
            vp.ARG_SCREEN_INFO_LIST: [{
                "name": "Screen 1",
                "geometry": vp.Rect(0, 0, self.ctx.fb_width, self.ctx.fb_height),
            }],
        }).serialize_rfb()

    async def send_async_feature_messages(self):
        """Come il vero servizio: dopo il sync spinge user/session/screens."""
        await self.send(self.user_info_msg())
        await self.send(self.session_info_msg())
        await self.send(self.screens_msg())

    async def handle_fbur(self, msg):
        incremental, x, y, w, h = struct.unpack(">BHHHH", msg)
        if incremental:
            await self.send(vp.build_empty_fb_update())
        else:
            await self.send(vp.build_fb_update_raw(x, y, w, h, self.pix_bytes))

    async def run(self):
        await self.handshake()
        log.info("[%s] handshake completato, connessione attiva", self.peer[0])
        while True:
            mtype = (await _read_exactly(self.reader, 1))[0]
            if mtype == vp.MSG_FEATURE:
                await self.handle_feature()
            elif mtype == vp.CMSG_SET_PIXEL_FORMAT:
                pf = await _read_exactly(self.reader, 19)
                self.pix_bytes = pf[3] // 8
                self.log_rx("SetPixelFormat", pf)
            elif mtype == vp.CMSG_SET_ENCODINGS:
                hdr = await _read_exactly(self.reader, 3)
                (n,) = struct.unpack(">H", hdr[1:3])
                self.log_rx("SetEncodings", await _read_exactly(self.reader, 4 * n))
            elif mtype == vp.CMSG_FB_UPDATE_REQ:
                await self.handle_fbur(await _read_exactly(self.reader, 9))
            elif mtype == vp.CMSG_KEY_EVENT:
                await _read_exactly(self.reader, 7)
            elif mtype == vp.CMSG_POINTER_EVENT:
                await _read_exactly(self.reader, 5)
            elif mtype == vp.CMSG_CLIENT_CUT_TEXT:
                hdr = await _read_exactly(self.reader, 7)
                (ln,) = struct.unpack(">I", hdr[3:7])
                await _read_exactly(self.reader, ln)
            elif mtype == vp.CMSG_FIX_COLOURMAP:
                hdr = await _read_exactly(self.reader, 5)
                (n,) = struct.unpack(">H", hdr[3:5])
                await _read_exactly(self.reader, 6 * n)
            else:
                log.warning("[%s] msg type sconosciuto %d, chiudo",
                            self.peer[0], mtype)
                return


async def handle_client(reader, writer, ctx):
    conn = FakeServiceConnection(reader, writer, ctx)
    try:
        await conn.run()
    except (asyncio.IncompleteReadError, ConnectionResetError):
        log.info("[%s] disconnesso", conn.peer[0])
    except Exception:
        log.exception("[%s] errore", conn.peer[0])
    finally:
        try:
            writer.close()
        except Exception:
            pass


class ServiceCtx:
    def __init__(self, events, fb_width=1024, fb_height=768, verbose=False):
        self.events = events               # asyncio.Queue
        self.fb_width = fb_width
        self.fb_height = fb_height
        self.verbose = verbose
        self.username = getpass.getuser()
        self.hostname = socket.gethostname()
        import time
        self._t0 = time.monotonic()
        self.public_key_pem = self._make_public_key()

    def uptime(self):
        import time
        return time.monotonic() - self._t0

    @staticmethod
    def _make_public_key():
        try:
            from cryptography.hazmat.primitives import serialization
            from cryptography.hazmat.primitives.asymmetric import rsa
            key = rsa.generate_private_key(65537, 2048)
            return key.public_key().public_bytes(
                serialization.Encoding.PEM,
                serialization.PublicFormat.SubjectPublicKeyInfo)
        except ImportError:
            # non serve davvero: se il master usa logon auth vedremo i bytes
            return b"-----BEGIN PUBLIC KEY-----\n-----END PUBLIC KEY-----\n"


async def run_service(port, ctx):
    server = await asyncio.start_server(
        lambda r, w: handle_client(r, w, ctx), "0.0.0.0", port)
    log.info("fake veyon-service in ascolto su :%d", port)
    async with server:
        await server.serve_forever()


async def _dump_events(queue):
    while True:
        fm, peer = await queue.get()
        log.info("evento da %s: %s", peer, fm)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--port", type=int, default=11100)
    ap.add_argument("--width", type=int, default=1024)
    ap.add_argument("--height", type=int, default=768)
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")

    events = asyncio.Queue()
    ctx = ServiceCtx(events, args.width, args.height, args.verbose)

    async def _main():
        await asyncio.gather(run_service(args.port, ctx), _dump_events(events))

    try:
        asyncio.run(_main())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
