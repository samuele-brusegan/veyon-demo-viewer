"""demo_store.py - store persistente dei token demo catturati.

Condiviso tra veyondemo_gui.py (che lo mostra) e veyon_daemon.py
(che lo popola in background anche a GUI spenta).
"""

import json
import logging
import os
import time

log = logging.getLogger("demo_store")

STORE_PATH = os.path.expanduser(
    "~/.local/share/veyon-demo-viewer/demos.json")


class DemoStore:
    """Token demo catturati, persistenti su disco (chiave host:port)."""

    def __init__(self, path):
        self.path = path
        self.demos = {}
        self.load()

    def load(self):
        try:
            with open(self.path) as f:
                self.demos = json.load(f)
        except Exception:
            self.demos = {}

    def save(self):
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w") as f:
                json.dump(self.demos, f, indent=1)
            os.replace(tmp, self.path)
        except OSError as e:
            log.warning("store: salvataggio fallito: %s", e)

    @staticmethod
    def key(host, port):
        return f"{host}:{port}"

    def upsert(self, host, port, token, mode):
        rec = self.demos.get(self.key(host, port), {})
        rec.update(host=host, port=port, token=token.hex(), mode=mode,
                   first=rec.get("first", time.time()),
                   last=time.time(), active=True)
        self.demos[self.key(host, port)] = rec
        self.save()
        return rec

    def deactivate(self):
        changed = False
        for rec in self.demos.values():
            if rec.get("active"):
                rec["active"] = False
                changed = True
        if changed:
            self.save()

    def sorted(self):
        return sorted(self.demos.values(),
                      key=lambda r: r.get("last", 0), reverse=True)
