"""Pubblica l'icona SVG della finestra come _NET_WM_ICON su X11/XFCE."""

import ctypes
import os

import gi
gi.require_version("GdkPixbuf", "2.0")
from gi.repository import GdkPixbuf


def set_x11_window_icon(window, path):
    surface = window.get_surface()
    if surface is None or not hasattr(surface, "get_xid") or not os.path.isfile(path):
        return

    pixbuf = GdkPixbuf.Pixbuf.new_from_file_at_scale(path, 128, 128, True)
    width, height = pixbuf.get_width(), pixbuf.get_height()
    pixels = pixbuf.get_pixels()
    stride = pixbuf.get_rowstride()
    channels = pixbuf.get_n_channels()
    has_alpha = pixbuf.get_has_alpha()
    data_values = [width, height]
    for y in range(height):
        for x in range(width):
            pos = y * stride + x * channels
            red, green, blue = pixels[pos:pos + 3]
            alpha = pixels[pos + 3] if has_alpha else 255
            data_values.append((alpha << 24) | (red << 16) |
                               (green << 8) | blue)

    xlib = ctypes.CDLL("libX11.so.6")
    xlib.XOpenDisplay.argtypes = [ctypes.c_char_p]
    xlib.XOpenDisplay.restype = ctypes.c_void_p
    xlib.XInternAtom.argtypes = [ctypes.c_void_p, ctypes.c_char_p,
                                 ctypes.c_int]
    xlib.XInternAtom.restype = ctypes.c_ulong
    xlib.XChangeProperty.argtypes = [ctypes.c_void_p, ctypes.c_ulong,
                                     ctypes.c_ulong, ctypes.c_ulong,
                                     ctypes.c_int, ctypes.c_int,
                                     ctypes.POINTER(ctypes.c_ulong),
                                     ctypes.c_int]
    xlib.XFlush.argtypes = [ctypes.c_void_p]
    xlib.XCloseDisplay.argtypes = [ctypes.c_void_p]
    display = xlib.XOpenDisplay(None)
    if not display:
        return
    try:
        xid = surface.get_xid()
        icon = xlib.XInternAtom(display, b"_NET_WM_ICON", 0)
        cardinal = xlib.XInternAtom(display, b"CARDINAL", 0)
        data = (ctypes.c_ulong * len(data_values))(*data_values)
        xlib.XChangeProperty(display, xid, icon, cardinal, 32, 0,
                             data, len(data_values))
        xlib.XFlush(display)
    finally:
        xlib.XCloseDisplay(display)
