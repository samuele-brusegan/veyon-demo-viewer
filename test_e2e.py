"""test_e2e.py - test end-to-end interno senza vero Veyon.

- mock demo server: RFB server con auth Token che serve frame a pattern
- fake service in-process su porta di test
- mock master: handshake + FBUR + StartDemoClient -> verifica evento catturato
- mock demo client: come demo_viewer ma senza GTK -> verifica decode rect

Uso: python3 test_e2e.py
"""

import asyncio
import struct
import sys
import uuid

import numpy as np

import veyon_proto as vp
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

async def mock_demo_server(reader, writer):
    writer.write(vp.RFB_VERSION)
    await writer.drain()
    await reader.readexactly(12)
    writer.write(bytes([1, vp.SEC_TYPE_VEYON]))
    await writer.drain()
    assert (await reader.readexactly(1))[0] == vp.SEC_TYPE_VEYON
    writer.write(vp.pack_vam(1, vp.UserType("RfbVeyonAuth::Type", vp.AUTH_TOKEN)))
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
                # pattern: sfondo a gradiente + barra che si sposta
                fb = np.zeros((h, w, 3), np.uint8)
                fb[:, :, 0] = np.arange(w, dtype=np.uint8)[None, :]
                pos = frame_no % max(1, w - 40)
                fb[:, pos:pos + 40] = (255, 0, 0)
                rect = struct.pack(">BxH", vp.MSG_FB_UPDATE, 1) + \
                    struct.pack(">HHHHi", x, y, w, h, vp.ENC_RAW) + fb.tobytes()
                writer.write(rect)
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

    writer.write(vp.pack_vam(vp.UserType("RfbVeyonAuth::Type", vp.AUTH_KEYFILE),
                             "teacher"))
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


# ---------------------------------------------------------------- mock demo client (come demo_viewer, no GUI)

async def mock_demo_client():
    reader, writer = await asyncio.open_connection("127.0.0.1", DEMO_PORT)
    assert await reader.readexactly(12) == vp.RFB_VERSION
    writer.write(vp.RFB_VERSION)
    await writer.drain()
    n = (await reader.readexactly(1))[0]
    await reader.readexactly(n)
    writer.write(bytes([vp.SEC_TYPE_VEYON]))
    await writer.drain()
    payload = await read_vam_r(reader)                        # [Token]
    writer.write(vp.pack_vam(vp.AUTH_TOKEN, ""))
    await writer.drain()
    await read_vam_r(reader)                                  # ack
    writer.write(vp.pack_vam(TOKEN))
    await writer.drain()
    (res,) = struct.unpack(">I", await reader.readexactly(4))
    check("demo client: auth OK", res == vp.AUTH_OK)

    writer.write(b"\x01")
    init = await reader.readexactly(24)
    w, h = struct.unpack(">HH", init[:4])
    (nl,) = struct.unpack(">I", init[20:24])
    await reader.readexactly(nl)
    check("demo client: ServerInit", (w, h) == (FB_W, FB_H))

    writer.write(struct.pack(">BBHHHH", vp.CMSG_FB_UPDATE_REQ, 0, 0, 0, w, h))
    await writer.drain()
    mtype = (await reader.readexactly(1))[0]
    (nrects,) = struct.unpack(">xH", await reader.readexactly(3))
    x, y, rw, rh, enc = struct.unpack(">HHHHi", await reader.readexactly(12))
    data = await reader.readexactly(rw * rh * 3)
    fb = np.frombuffer(data, np.uint8).reshape(rh, rw, 3)
    check("demo client: frame raw decodificato",
          mtype == 0 and nrects == 1 and rw == w and rh == h
          and int(fb[50, 100, 0]) == 100)
    writer.close()


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
        await mock_demo_client()

    svc.close(); demo.close()
    failed = [n for n, c in passed if not c]
    print(f"\n{len(passed) - len(failed)}/{len(passed)} test passati")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    asyncio.run(main())
