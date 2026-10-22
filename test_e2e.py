"""test_e2e.py - test end-to-end interno senza vero Veyon.

- mock demo server: RFB server con auth Token che serve frame a pattern
- fake service in-process su porta di test
- mock master: handshake + FBUR + StartDemoClient -> verifica evento catturato
- mock demo client: come demo_viewer ma senza GTK -> verifica decode rect

Uso: python3 test_e2e.py
"""

import asyncio
import socket
import struct
import sys
import uuid
import zlib

import numpy as np

import veyon_proto as vp
import demo_viewer
from fake_service import ServiceCtx, handle_client

SVC_PORT = 21100
DEMO_PORT = 21400
TOKEN = b"test-token-segreto-42"
FB_W, FB_H = 320, 200

passed = []
def check(name, cond):
    passed.append((name, cond))
    print(("PASS" if cond else "FAIL"), name)


# ---------------------------------------------------------------- mock demo server

def pack_compact_len(n):
    if n < 0x80:
        return bytes([n])
    if n < 0x4000:
        return bytes([0x80 | (n & 0x7F), n >> 7])
    return bytes([0x80 | (n & 0x7F), 0x80 | ((n >> 7) & 0x7F), n >> 14])


def tight_rect(x, y, w, h, body):
    return struct.pack(">HHHHi", x, y, w, h, vp.ENC_TIGHT) + body


def build_tight_fbu(w, h):
    """FBU con 4 rect Tight: copy+zlib, fill, palette 2 colori, copy raw."""
    rects = b""

    # 1) copy filter, stream zlib 0 (reset flag): intero framebuffer RGB
    fb = np.zeros((h, w, 3), np.uint8)
    fb[:, :, 0] = np.arange(w, dtype=np.uint8)[None, :]
    payload = zlib.compress(fb.tobytes())
    rects += tight_rect(0, 0, w, h,
                        b"\x01" + pack_compact_len(len(payload)) + payload)

    # 2) fill rect rosso 10x10
    rects += tight_rect(0, 0, 10, 10, b"\x80" + bytes([255, 0, 0]))

    # 3) palette 2 colori 8x8 (righe alternate), filtro esplicito + reset
    pal = bytes([0, 0, 0, 255, 255, 255])
    rows = bytes([0xAA, 0x55] * 4)
    rects += tight_rect(10, 10, 8, 8,
                        b"\x41" + b"\x01" + b"\x01" + pal + rows)

    # 4) copy raw (<12B): 3x1 con pixel 1..9
    rects += tight_rect(30, 30, 3, 1,
                        b"\x01" + bytes([1, 2, 3, 4, 5, 6, 7, 8, 9]))

    return struct.pack(">BxH", vp.MSG_FB_UPDATE, 4) + rects


async def mock_demo_server(reader, writer):
    writer.write(vp.RFB_VERSION)
    await writer.drain()
    await reader.readexactly(12)
    writer.write(bytes([1, vp.SEC_TYPE_VEYON]))
    await writer.drain()
    assert (await reader.readexactly(1))[0] == vp.SEC_TYPE_VEYON
    writer.write(vp.pack_vam(1, vp.AUTH_TOKEN))   # rete reale: plain Int
    await writer.drain()
    payload = await read_vam_r(reader)
    vals = vp.decode_all(payload)
    check("demo: client sceglie Token", int(vals[0]) == vp.AUTH_TOKEN)
    writer.write(vp.EMPTY_VAM)                      # ack
    payload = await read_vam_r(reader)
    vals = vp.decode_all(payload)
    check("demo: token corretto", vals[0] == TOKEN)
    writer.write(struct.pack(">I", vp.AUTH_OK))
    await writer.drain()
    await reader.readexactly(1)                     # ClientInit
    writer.write(vp.build_server_init(FB_W, FB_H))
    await writer.drain()

    frame_no = 0
    try:
        while True:
            mtype = (await reader.readexactly(1))[0]
            if mtype == vp.CMSG_SET_PIXEL_FORMAT:
                await reader.readexactly(19)
            elif mtype == vp.CMSG_SET_ENCODINGS:
                hdr = await reader.readexactly(3)
                (n,) = struct.unpack(">H", hdr[1:3])
                await reader.readexactly(4 * n)
            elif mtype == vp.CMSG_FB_UPDATE_REQ:
                req = await reader.readexactly(9)
                incremental, x, y, w, h = struct.unpack(">BHHHH", req)
                frame_no += 1
                writer.write(build_tight_fbu(w, h))
                await writer.drain()
            else:
                break
    except (asyncio.IncompleteReadError, ConnectionResetError):
        pass


async def read_vam_r(reader):
    (size,) = struct.unpack(">I", await reader.readexactly(4))
    return await reader.readexactly(size)


# ---------------------------------------------------------------- mock master

async def mock_master(events):
    reader, writer = await asyncio.open_connection("127.0.0.1", SVC_PORT)
    assert await reader.readexactly(12) == vp.RFB_VERSION
    writer.write(vp.RFB_VERSION)
    await writer.drain()
    assert await reader.readexactly(2) == bytes([1, vp.SEC_TYPE_VEYON])
    writer.write(bytes([vp.SEC_TYPE_VEYON]))
    await writer.drain()

    payload = await read_vam_r(reader)
    vals = vp.decode_all(payload)
    auth_types = [int(v) for v in vals[1:]]
    check("master: auth types offerti KeyFile+Logon",
          auth_types == [vp.AUTH_KEYFILE, vp.AUTH_LOGON])

    writer.write(vp.pack_vam(vp.AUTH_KEYFILE, "teacher"))   # plain Int
    await writer.drain()
    assert await reader.readexactly(4) == vp.EMPTY_VAM          # ack
    payload = await read_vam_r(reader)                        # challenge
    check("master: challenge 128B", len(payload) == vp.CHALLENGE_SIZE + 5 + 4)
    writer.write(vp.pack_vam("superior", b"\xaa" * 512))      # [keyName, sig]
    await writer.drain()
    (res,) = struct.unpack(">I", await reader.readexactly(4))
    check("master: auth OK", res == vp.AUTH_OK)

    writer.write(b"\x01")                                     # ClientInit
    init = await reader.readexactly(24)
    (name_len,) = struct.unpack(">I", init[20:24])
    await reader.readexactly(name_len)
    check("master: ServerInit", len(init) == 24)

    # async feature messages post-connect (user/session/screens)
    for _ in range(3):
        assert (await reader.readexactly(1))[0] == vp.MSG_FEATURE
        await read_vam_r(reader)

    # monitoring: FBUR non incrementale -> rect nero
    writer.write(struct.pack(">BBHHHH", vp.CMSG_FB_UPDATE_REQ, 0,
                             0, 0, 16, 16))
    await writer.drain()
    mtype = (await reader.readexactly(1))[0]
    hdr = await reader.readexactly(3)
    (n,) = struct.unpack(">xH", hdr)
    check("master: FB update", mtype == vp.MSG_FB_UPDATE and n == 1)
    await reader.readexactly(12 + 16 * 16 * 4)

    # ping -> echo
    ping = vp.FeatureMessage(vp.UID_MONITORING, 0, {})
    writer.write(ping.serialize_rfb())
    await writer.drain()
    assert (await reader.readexactly(1))[0] == vp.MSG_FEATURE
    payload = await read_vam_r(reader)
    echo = vp.FeatureMessage.parse(payload)
    check("master: ping echo", echo.uid == vp.UID_MONITORING)

    # StartDemoClient (window) con host vuoto -> il servizio usa il peer
    fm = vp.FeatureMessage(vp.DEMO_UID_WINDOW, vp.CMD_START_DEMO_CLIENT, {
        vp.ARG_DEMO_ACCESS_TOKEN: TOKEN,
        vp.ARG_DEMO_SERVER_HOST: "",
        vp.ARG_DEMO_SERVER_PORT: DEMO_PORT,
        vp.ARG_VIEWPORT: vp.Rect(0, 0, FB_W, FB_H),
    })
    writer.write(fm.serialize_rfb())
    await writer.drain()

    got = None
    try:
        while True:
            msg, peer = await asyncio.wait_for(events.get(), 5)
            if msg.uid in vp.DEMO_UIDS:
                got = msg
                break
    except asyncio.TimeoutError:
        pass
    check("master: StartDemoClient catturato",
          got is not None and got.command == vp.CMD_START_DEMO_CLIENT
          and got.args.get(vp.ARG_DEMO_ACCESS_TOKEN) == TOKEN)
    if got:
        check("master: viewport rect", got.args.get(vp.ARG_VIEWPORT) == (0, 0, FB_W, FB_H))

    writer.close()
    return got


# ------------------------------------------------- mock demo client (vero decoder)

class DummyApp:
    closed = False
    def set_size(self, w, h): pass
    def set_status(self, t): pass
    def refresh(self, fb): pass


def mock_demo_client():
    c = demo_viewer.DemoRfbClient("127.0.0.1", DEMO_PORT, TOKEN, DummyApp())
    c.sock = socket.create_connection(("127.0.0.1", DEMO_PORT), 5)
    c.handshake()
    check("demo client: auth+ServerInit", c.fb_size == (FB_W, FB_H))

    # handshake ha gia' inviato il FBUR: leggiamo un FBU col decoder vero
    mtype = demo_viewer.recv_exactly(c.sock, 1)[0]
    (nrects,) = struct.unpack(">xH", demo_viewer.recv_exactly(c.sock, 3))
    c.read_rects(nrects)
    fb = c.fb
    check("tight copy+zlib", int(fb[50, 100, 0]) == 100)
    check("tight fill", tuple(fb[5, 5]) == (255, 0, 0))
    check("tight palette",
          tuple(fb[10, 10]) == (255, 255, 255)
          and tuple(fb[10, 11]) == (0, 0, 0)
          and tuple(fb[11, 10]) == (0, 0, 0)
          and tuple(fb[11, 11]) == (255, 255, 255))
    check("tight copy raw",
          tuple(fb[30, 30]) == (1, 2, 3) and tuple(fb[30, 32]) == (7, 8, 9))
    c.sock.close()


# ---------------------------------------------------------------- main

async def main():
    events = asyncio.Queue()
    ctx = ServiceCtx(events, verbose=True)
    svc = await asyncio.start_server(lambda r, w: handle_client(r, w, ctx),
                                     "127.0.0.1", SVC_PORT)
    demo = await asyncio.start_server(mock_demo_server, "127.0.0.1", DEMO_PORT)

    fm = await mock_master(events)
    if fm:
        host = fm.args.get(vp.ARG_DEMO_SERVER_HOST) or "127.0.0.1"
        port = fm.args.get(vp.ARG_DEMO_SERVER_PORT)
        token = fm.args.get(vp.ARG_DEMO_ACCESS_TOKEN)
        check("demo args", port == DEMO_PORT and token == TOKEN)
        await asyncio.to_thread(mock_demo_client)

    svc.close(); demo.close()
    failed = [n for n, c in passed if not c]
    print(f"\n{len(passed) - len(failed)}/{len(passed)} test passati")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    asyncio.run(main())
