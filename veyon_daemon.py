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

import veyon_proto as vp
from fake_service import ServiceCtx, run_service
from demo_store import DemoStore, STORE_PATH

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
            store.upsert(host, port, token, mode)
            log.info("demo %s: %s:%s token %dB salvato",
                     mode, host, port, len(token))
        elif fm.command == vp.CMD_STOP_DEMO_CLIENT:
            store.deactivate()
            log.info("demo terminata")


async def amain(args):
    store = DemoStore(STORE_PATH)
    # allo start lo stato reale e' ignoto: niente demo marcata attiva;
    # se il master ri-annuncia una demo in corso torna attiva da sola
    store.deactivate()
    events = asyncio.Queue()
    ctx = ServiceCtx(events, verbose=args.verbose)
    await asyncio.gather(run_service(args.port, ctx),
                         consume(events, store))


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
