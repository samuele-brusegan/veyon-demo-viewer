#!/usr/bin/env python3
"""veyondemo.py - entrypoint: fake service + spawn del viewer alle demo.

Quando il master invia StartDemoClient viene lanciato demo_viewer.py in una
finestra normale. StopDemoClient lo chiude. Il flag fullscreen viene ignorato
sempre.

Uso: python3 veyondemo.py [--port 11100] [-v]
"""

import argparse
import asyncio
import logging
import os
import sys

import veyon_proto as vp
from fake_service import ServiceCtx, run_service

log = logging.getLogger("veyondemo")

HERE = os.path.dirname(os.path.abspath(__file__))


class DemoLauncher:
    """Consuma gli eventi FeatureMessage e gestisce il processo viewer."""

    def __init__(self, events, spawn=True, human=False):
        self.events = events
        self.spawn = spawn
        self.human = human
        self.proc = None

    def say(self, msg):
        if self.human:
            print(f"  {msg}", flush=True)

    async def run(self):
        while True:
            fm, peer = await self.events.get()
            try:
                await self.handle(fm, peer)
            except Exception:
                log.exception("errore gestione evento")

    async def handle(self, fm, peer):
        if fm.uid not in vp.DEMO_UIDS:
            log.debug("feature ignorata: %s", fm)
            return

        if fm.command == vp.CMD_START_DEMO_CLIENT:
            host = fm.args.get(vp.ARG_DEMO_SERVER_HOST) or peer
            port = int(fm.args.get(vp.ARG_DEMO_SERVER_PORT) or 11400)
            token = fm.args.get(vp.ARG_DEMO_ACCESS_TOKEN) or b""
            mode = "fullscreen" if fm.uid == vp.DEMO_UID_FULLSCREEN else "window"
            log.info("demo start (%s -> window): %s:%d token=%dB",
                     mode, host, port, len(token))
            self.say(f"il docente ha avviato una presentazione da "
                     f"{host}:{port} — apro il viewer in finestra "
                     f"(la modalità {mode} viene ignorata)")
            await self.start_viewer(host, port, token)
        elif fm.command == vp.CMD_STOP_DEMO_CLIENT:
            log.info("demo stop")
            if self.proc is not None:
                self.say("presentazione terminata — chiudo il viewer")
            await self.stop_viewer()

    async def start_viewer(self, host, port, token):
        await self.stop_viewer()
        if not self.spawn:
            log.info("viewer disabilitato (--no-viewer)")
            return
        self.proc = await asyncio.create_subprocess_exec(
            sys.executable, os.path.join(HERE, "demo_viewer.py"),
            host, str(port), token.hex(),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL)

    async def stop_viewer(self):
        if self.proc is None:
            return
        if self.proc.returncode is None:
            self.proc.terminate()
            try:
                await asyncio.wait_for(self.proc.wait(), 3)
            except asyncio.TimeoutError:
                self.proc.kill()
        self.proc = None


async def amain(args):
    logging.getLogger().setLevel(logging.DEBUG if args.verbose else logging.INFO)
    events = asyncio.Queue()
    ctx = ServiceCtx(events, args.width, args.height, args.verbose, args.human)
    launcher = DemoLauncher(events, spawn=not args.no_viewer, human=args.human)
    if args.human:
        print(f"In ascolto sulla porta {args.port}: aspetto che il docente "
              "avvii una presentazione.\n  (quando parte, apro una finestra; "
              "chiudo con Ctrl-C)", flush=True)
    await asyncio.gather(run_service(args.port, ctx), launcher.run())


def main():
    ap = argparse.ArgumentParser(description=__doc__, add_help=False)
    ap.add_argument("--help", action="help",
                    help="mostra questo messaggio ed esce")
    ap.add_argument("--port", type=int, default=11100)
    ap.add_argument("--width", type=int, default=1024,
                    help="larghezza finto framebuffer (thumbnail nera)")
    ap.add_argument("--height", type=int, default=768)
    ap.add_argument("--no-viewer", action="store_true",
                    help="logga i comandi demo senza aprire il viewer")
    ap.add_argument("-v", "--verbose", action="store_true")
    ap.add_argument("-h", "--human", action="store_true",
                    help="output in linguaggio naturale")
    args = ap.parse_args()

    logging.basicConfig(format="%(asctime)s %(name)s %(levelname)s %(message)s",
                        level=logging.INFO)
    try:
        asyncio.run(amain(args))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
