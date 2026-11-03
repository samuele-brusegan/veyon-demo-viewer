#!/usr/bin/env python3
"""veyon_daemon.py - servizio minimale di cattura token demo.

Gira in background su :11100 senza GUI e senza viewer: prende solo e
solamente le chiavi (token) che il master Veyon invia con
StartDemoClient e le salva in ~/.local/share/veyon-demo-viewer/demos.json.
All'avvio successivo la GUI le trova gia' li'.

Uso: python3 veyon_daemon.py [--port 11100] [-v]
     in background: nohup python3 veyon_daemon.py &
"""

import argparse
import asyncio
import logging
import os

import veyon_proto as vp
from fake_service import ServiceCtx, run_service
from demo_store import DemoStore, STORE_PATH, Settings, SETTINGS_PATH, \
    ip_net24
from screen_capture import ScreenCapture

log = logging.getLogger("veyon_daemon")


async def consume(events, store):
    while True:
        fm, peer = await events.get()
        if fm.uid not in vp.DEMO_UIDS:
            continue
        if fm.command == vp.CMD_START_DEMO_CLIENT:
            host = fm.args.get(vp.ARG_DEMO_SERVER_HOST) or peer
            port = int(fm.args.get(vp.ARG_DEMO_SERVER_PORT) or 11400)
            token = fm.args.get(vp.ARG_DEMO_ACCESS_TOKEN) or b""
            mode = ("fullscreen" if fm.uid == vp.DEMO_UID_FULLSCREEN
                    else "window")
            store.upsert(host, port, token, mode, net=ip_net24(peer))
            log.info("demo %s: %s:%s token %dB salvato (rete %s)",
                     mode, host, port, len(token), ip_net24(peer))
        elif fm.command == vp.CMD_STOP_DEMO_CLIENT:
            store.deactivate()
            log.info("demo terminata")


async def watch_settings(ctx):
    """Osserva settings.json: con 'record_master' attivo avvia la
    cattura schermo e la serve ai FBUR dei master."""
    cap = None
    mtime = None
    while True:
        try:
            mt = os.path.getmtime(SETTINGS_PATH)
        except OSError:
            mt = None
        if mt != mtime:
            mtime = mt
            on = bool(Settings().get("record_master"))
            if on and cap is None:
                cap = ScreenCapture(
                    on_status=lambda t: log.info("schermo: %s", t))
                cap.start()
                ctx.frame_provider = cap.get_frame
                log.info("record_master ON — schermo servito ai master")
            elif not on and cap is not None:
                cap.stop()
                cap = None
                ctx.frame_provider = None
                log.info("record_master OFF — framebuffer nero")
        await asyncio.sleep(3)


async def amain(args):
    store = DemoStore(STORE_PATH)
    # allo start lo stato reale e' ignoto: niente demo marcata attiva;
    # se il master ri-annuncia una demo in corso torna attiva da sola
    store.deactivate()
    events = asyncio.Queue()
    ctx = ServiceCtx(events, verbose=args.verbose)
    await asyncio.gather(run_service(args.port, ctx),
                         consume(events, store),
                         watch_settings(ctx))


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--port", type=int, default=11100)
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(format="%(asctime)s %(name)s %(levelname)s %(message)s",
                        level=logging.DEBUG if args.verbose else logging.INFO)
    try:
        asyncio.run(amain(args))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
