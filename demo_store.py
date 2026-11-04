"""demo_store.py - store persistente dei token demo catturati.

Condiviso tra veyondemo_gui.py (che lo mostra) e veyon_daemon.py
(che lo popola in background anche a GUI spenta).
"""

import ipaddress
import json
import logging
import os
import socket
import time

log = logging.getLogger("demo_store")

STORE_PATH = os.path.expanduser(
    "~/.local/share/veyon-demo-viewer/demos.json")


def ip_net24(ip):
    """Rete /24 di un IPv4 (es. '192.168.4.17' -> '192.168.4.0/24').
    None se non e' un IPv4."""
    try:
        addr = ipaddress.ip_address(str(ip))
    except ValueError:
        return None
    if addr.version != 4:
        return None
    return str(ipaddress.ip_network(f"{addr}/24", strict=False))


def local_ipv4():
    """IPv4 locale corrente (interfaccia della rotta di default)."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.0.2.1", 80))
            return s.getsockname()[0]
    except OSError:
        pass
    try:
        return socket.gethostbyname(socket.gethostname())
    except OSError:
        return None


def local_net24():
    """Rete /24 locale corrente. None se non determinabile."""
    return ip_net24(local_ipv4())


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

    def upsert(self, host, port, token, mode, net=None):
        rec = self.demos.get(self.key(host, port), {})
        rec.update(host=host, port=port, token=token.hex(), mode=mode,
                   net=net or rec.get("net") or ip_net24(host),
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


SETTINGS_PATH = os.path.expanduser(
    "~/.local/share/veyon-demo-viewer/settings.json")


class Settings:
    """Impostazioni persistenti (credenziali master, lista client)."""

    def __init__(self, path=SETTINGS_PATH):
        self.path = path
        self.data = {}
        try:
            with open(path) as f:
                self.data = json.load(f)
        except Exception:
            self.data = {}

    def save(self):
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w") as f:
                json.dump(self.data, f, indent=1)
            os.replace(tmp, self.path)
        except OSError as e:
            log.warning("settings: salvataggio fallito: %s", e)

    def get(self, key, default=""):
        return self.data.get(key, default)

    def set(self, key, value):
        self.data[key] = value
        self.save()

    def clients(self):
        return self.data.get("clients", [])

    def set_clients(self, ips):
        self.data["clients"] = ips
        self.save()


CHAT_PATH = os.path.expanduser(
    "~/.local/share/veyon-demo-viewer/chat.json")
CHAT_MAX_PER_PEER = 200


class ChatStore:
    """Storico chat persistente: {ip: [{ts, who, text}]}.

    'who' e' "io" per i messaggi inviati, altrimenti il nome del peer.
    Condiviso tra GUI e daemon (il daemon salva i messaggi ricevuti
    mentre la GUI e' chiusa)."""

    def __init__(self, path=CHAT_PATH):
        self.path = path
        self.msgs = {}
        try:
            with open(path) as f:
                self.msgs = json.load(f)
        except Exception:
            self.msgs = {}

    def save(self):
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w") as f:
                json.dump(self.msgs, f, indent=0)
            os.replace(tmp, self.path)
        except OSError as e:
            log.warning("chat store: salvataggio fallito: %s", e)

    def append(self, ip, who, text):
        lst = self.msgs.setdefault(ip, [])
        lst.append({"ts": time.time(), "who": who, "text": text})
        del lst[:-CHAT_MAX_PER_PEER]
        self.save()

    def history(self, ip):
        return list(self.msgs.get(ip, []))
