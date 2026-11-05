#!/usr/bin/env python3
"""install_deps.py - TUI per selezionare e installare le dipendenze.

Nessuna dipendenza extra: curses e' nella stdlib. Rileva la distro
(apt/dnf/pacman), mostra lo stato installato/mancante di ogni
componente e installa quelli selezionati con il package manager.

Uso: python3 install_deps.py
     python3 install_deps.py --list     # solo stampa lo stato
"""

import curses
import os
import shutil
import subprocess
import sys

# ------------------------------------------------------------- deps --


def _gi_ok(ns, ver):
    try:
        import gi
        gi.require_version(ns, ver)
        __import__("gi.repository." + ns)
        return True
    except (ValueError, ImportError, AttributeError):
        return False


def _which(name):
    return shutil.which(name) is not None


def _lib_ok(soname):
    import ctypes
    import ctypes.util
    try:
        ctypes.CDLL(ctypes.util.find_library(soname) or f"lib{soname}.so")
        return True
    except OSError:
        return False


def _portal_ok():
    for p in ("/usr/share/dbus-1/services/org.freedesktop.portal."
              "Desktop.service",
              "/usr/share/dbus-1/services/org.freedesktop.impl.portal."
              "Permission.service"):
        if os.path.exists(p):
            return True
    return os.path.exists("/usr/libexec/xdg-desktop-portal") or \
        os.path.exists("/usr/lib/xdg-desktop-portal")


DEPS = [
    {
        "id": "gtk",
        "name": "GTK4 + libadwaita + PyGObject",
        "desc": "interfaccia grafica (obbligatorio)",
        "check": lambda: _gi_ok("Gtk", "4.0") and _gi_ok("Adw", "1"),
        "apt": ["python3-gi", "gir1.2-gtk-4.0", "gir1.2-adw-1",
                "gir1.2-gdkpixbuf-2.0"],
        "dnf": ["python3-gobject", "gtk4", "libadwaita"],
        "pacman": ["python-gobject", "gtk4", "libadwaita"],
    },
    {
        "id": "numpy",
        "name": "NumPy",
        "desc": "framebuffer demo e cattura schermo (obbligatorio)",
        "check": lambda: bool(__import__("importlib").util.find_spec("numpy")),
        "apt": ["python3-numpy"],
        "dnf": ["python3-numpy"],
        "pacman": ["python-numpy"],
    },
    {
        "id": "gst",
        "name": "GStreamer + PipeWire",
        "desc": "cattura schermo su Wayland (portale ScreenCast)",
        "check": lambda: (_gi_ok("Gst", "1.0") and _which("pipewire")),
        "apt": ["gir1.2-gst-plugins-base-1.0", "gstreamer1.0-pipewire",
                "pipewire"],
        "dnf": ["gstreamer1-plugins-base", "pipewire-gstreamer",
                "pipewire"],
        "pacman": ["gst-plugins-base", "gst-plugin-pipewire", "pipewire"],
    },
    {
        "id": "portal",
        "name": "XDG Desktop Portal",
        "desc": "consenso cattura schermo su Wayland",
        "check": _portal_ok,
        "apt": ["xdg-desktop-portal", "xdg-desktop-portal-gnome"],
        "dnf": ["xdg-desktop-portal", "xdg-desktop-portal-gnome"],
        "pacman": ["xdg-desktop-portal", "xdg-desktop-portal-gnome"],
    },
    {
        "id": "webkit",
        "name": "WebKitGTK 6.0",
        "desc": "browser integrato del tab Meet / Google Classroom",
        "check": lambda: _gi_ok("WebKit", "6.0"),
        "apt": ["gir1.2-webkit-6.0"],
        "dnf": ["webkitgtk6.0"],
        "pacman": ["webkitgtk-6.0"],
    },
    {
        "id": "ffmpeg",
        "name": "ffmpeg",
        "desc": "registrazione schermo e riunioni Meet in .mkv",
        "check": lambda: _which("ffmpeg"),
        "apt": ["ffmpeg"],
        "dnf": ["ffmpeg"],
        "pacman": ["ffmpeg"],
    },
    {
        "id": "x11",
        "name": "libX11",
        "desc": "cattura schermo e icone su sessioni X11 (XFCE, LXDE)",
        "check": lambda: _lib_ok("X11"),
        "apt": ["libx11-6"],
        "dnf": ["libX11"],
        "pacman": ["libx11"],
    },
    {
        "id": "iconcache",
        "name": "gtk-update-icon-cache",
        "desc": "refresh della cache icone dopo l'installazione",
        "check": lambda: _which("gtk-update-icon-cache")
        or _which("gtk4-update-icon-cache"),
        "apt": ["libgtk-4-bin"],
        "dnf": ["gtk4"],
        "pacman": ["gtk4"],
    },
]


# ---------------------------------------------------------- distro ---


def detect_pm():
    """-> ('apt'|'dnf'|'pacman'|None, comando install con sudo)."""
    for pm, cmd in (("apt", ["apt-get", "install", "-y"]),
                    ("dnf", ["dnf", "install", "-y"]),
                    ("pacman", ["pacman", "-S", "--needed",
                                "--noconfirm"])):
        if _which(pm):
            return pm, cmd
    return None, None


def install(pkgs, pm, cmd):
    """Esegue l'installazione; True se ok. Richiede sudo interattivo."""
    if os.geteuid() == 0:
        sudo = []
    elif _which("sudo"):
        sudo = ["sudo"]
    else:
        print("serve root o sudo per installare pacchetti")
        return False
    full = sudo + cmd + pkgs
    print("\n$ " + " ".join(full) + "\n")
    r = subprocess.run(full)
    return r.returncode == 0


def statuses():
    out = {}
    for d in DEPS:
        try:
            out[d["id"]] = d["check"]()
        except Exception:
            out[d["id"]] = False
    return out


# ------------------------------------------------------------- TUI ---

HELP = "↑↓ muovi · spazio seleziona · a tutte/niente · i installa · q esci"


def tui(stdscr):
    curses.curs_set(0)
    sel = {d["id"]: False for d in DEPS}
    st = statuses()
    for d in DEPS:                      # pre-seleziona i mancanti
        sel[d["id"]] = not st[d["id"]]
    cur = 0
    pm, cmd = detect_pm()

    def draw():
        stdscr.erase()
        h, w = stdscr.getmaxyx()
        stdscr.addnstr(0, 2, "veyon-demo-viewer — dipendenze", w - 4,
                       curses.A_BOLD)
        pmname = pm or "??"
        stdscr.addnstr(1, 2, f"package manager: {pmname}"
                       + ("" if pm else "  (non riconosciuto!)"), w - 4)
        for i, d in enumerate(DEPS):
            y = 3 + i
            if y >= h - 3:
                break
            mark = "[x]" if sel[d["id"]] else "[ ]"
            state = "installato" if st[d["id"]] else "mancante"
            attr = curses.A_REVERSE if i == cur else 0
            line = f"{mark} {d['name']:<34} {state:<11} {d['desc']}"
            stdscr.addnstr(y, 2, line, w - 4, attr)
            if not st[d["id"]]:
                # evidenzia in bold i mancanti
                stdscr.addnstr(y, 6, d["name"], 34,
                               attr | curses.A_BOLD)
        stdscr.addnstr(h - 1, 2, HELP, w - 4)
        stdscr.refresh()

    while True:
        draw()
        k = stdscr.getch()
        if k in (ord("q"), 27):
            return
        elif k == curses.KEY_UP and cur > 0:
            cur -= 1
        elif k == curses.KEY_DOWN and cur < len(DEPS) - 1:
            cur += 1
        elif k == ord(" "):
            sel[DEPS[cur]["id"]] = not sel[DEPS[cur]["id"]]
        elif k == ord("a"):
            v = not all(sel.values())
            for i in sel:
                sel[i] = v
        elif k in (ord("i"), curses.KEY_ENTER, 10, 13):
            chosen = [d for d in DEPS if sel[d["id"]]]
            if not chosen:
                continue
            if pm is None:
                continue
            pkgs = []
            for d in chosen:
                pkgs += d.get(pm) or []
            curses.endwin()
            print(f"installo: {', '.join(pkgs)}")
            ok = install(pkgs, pm, cmd)
            print("installazione",
                  "completata." if ok else "FALLITA (vedi sopra).")
            input("invio per continuare…")
            st = statuses()             # rileva stato post-install
            for d in DEPS:
                sel[d["id"]] = not st[d["id"]]
            stdscr.clear()


def plain_list():
    st = statuses()
    for d in DEPS:
        print(f"{'OK ' if st[d['id']] else '---'} {d['name']:<34} "
              f"{d['desc']}")
    pm, _ = detect_pm()
    print(f"\npackage manager rilevato: {pm or 'nessuno'}")


def main():
    if "--list" in sys.argv or not sys.stdout.isatty():
        plain_list()
        return
    try:
        curses.wrapper(tui)
    except curses.error as e:
        print(f"TUI non disponibile ({e}) — stato attuale:")
        plain_list()


if __name__ == "__main__":
    main()
