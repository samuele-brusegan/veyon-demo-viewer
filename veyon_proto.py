"""veyon_proto.py - codec del protocollo Veyon (RFB + VariantArrayMessage + FeatureMessage)

Reimplementazione del wire format di Veyon 4.x derivata dal sorgente GPL.
Vedi docs/protocol.md.
"""

import struct
import uuid as uuidlib

# ---------------------------------------------------------------------------
# Costanti RFB / Veyon
# ---------------------------------------------------------------------------

RFB_VERSION = b"RFB 003.008\n"

SEC_TYPE_VEYON = 40            # rfbSecTypeVeyon

# server -> client RFB message types
MSG_FB_UPDATE = 0
MSG_SET_COLOURMAP = 1
MSG_BELL = 2
MSG_SERVER_CUT_TEXT = 3
MSG_RESIZE_FB = 4
MSG_XVP = 250
MSG_FEATURE = 41             # FeatureMessage::RfbMessageType

# client -> server RFB message types
CMSG_SET_PIXEL_FORMAT = 0
CMSG_FIX_COLOURMAP = 1
CMSG_SET_ENCODINGS = 2
CMSG_FB_UPDATE_REQ = 3
CMSG_KEY_EVENT = 4
CMSG_POINTER_EVENT = 5
CMSG_CLIENT_CUT_TEXT = 6
CMSG_SET_DESKTOP_SIZE = 251
CMSG_XVP = 250

AUTH_OK = 0                  # rfbVncAuthOK

# RfbVeyonAuth::Type
AUTH_KEYFILE = 3
AUTH_LOGON = 4
AUTH_TOKEN = 5

# encodings
ENC_RAW = 0
ENC_COPYRECT = 1
ENC_ZLIB = 7
ENC_LAST_RECT = 0xFFFFFFE0

# feature uids demo (plugins/demo/DemoFeaturePlugin.cpp)
DEMO_UID_FULLSCREEN = uuidlib.UUID("7b6231bd-eb89-45d3-af32-f70663b2f878")
DEMO_UID_WINDOW = uuidlib.UUID("ae45c3db-dc2e-4204-ae8b-374cdab8c62c")
DEMO_UIDS = (DEMO_UID_FULLSCREEN, DEMO_UID_WINDOW)

# feature builtin (core/src/MonitoringMode.cpp)
UID_MONITORING = uuidlib.UUID("edad8259-b4ef-4ca5-90e6-f238d0fda694")   # Ping=0
UID_QUERY_APP_VERSION = uuidlib.UUID("58f5d5d5-9929-48f4-a995-f221c150ae26")
UID_QUERY_ACTIVE_FEATURES = uuidlib.UUID("a0a96fba-425d-414a-aaf4-352b76d7c4f3")
UID_QUERY_USER_INFO = uuidlib.UUID("79a5e74d-50bd-4aab-8012-0e70dc08cc72")
UID_QUERY_SESSION_INFO = uuidlib.UUID("699ed9dd-f58b-477b-a0af-df8105571b3c")
UID_QUERY_SCREENS = uuidlib.UUID("d5bbc486-7bc5-4c36-a9a8-1566c8b0091a")

# MonitoringMode::Argument (chiavi QVariantMap come stringhe)
ARG_ACTIVE_FEATURES = "0"      # ActiveFeaturesList == 0 (alias UserLoginName)
ARG_USER_LOGIN_NAME = "0"
ARG_USER_FULL_NAME = "1"
ARG_SESSION_ID = "2"
ARG_SCREEN_INFO_LIST = "3"
ARG_MIN_FB_UPDATE_INTERVAL = "4"
ARG_APPLICATION_VERSION = "5"
ARG_SESSION_UPTIME = "6"
ARG_SESSION_HOSTNAME = "7"
ARG_SESSION_CLIENT_ADDRESS = "8"
ARG_SESSION_CLIENT_NAME = "9"
ARG_SESSION_METADATA = "10"

# FeatureCommand (demo)
CMD_START_DEMO_SERVER = 0
CMD_STOP_DEMO_SERVER = 1
CMD_START_DEMO_CLIENT = 2
CMD_STOP_DEMO_CLIENT = 3

# Argument enum indices -> chiavi QVariantMap (stringhe numeriche)
ARG_DEMO_ACCESS_TOKEN = "0"
ARG_DEMO_SERVER_HOST = "3"
ARG_DEMO_SERVER_PORT = "4"
ARG_VIEWPORT = "5"

CHALLENGE_SIZE = 128

# ---------------------------------------------------------------------------
# QMetaType ids usati sulla wire (QDataStream Qt_5_5)
# ---------------------------------------------------------------------------

T_BOOL = 1
T_INT = 2
T_UINT = 3
T_LONGLONG = 4
T_ULONGLONG = 5
T_DOUBLE = 6
T_MAP = 8
T_LIST = 9
T_STRING = 10
T_STRINGLIST = 11
T_BYTEARRAY = 12
T_RECT = 19
T_UUID = 30
T_USER = 1024          # Qt5UserType: segue nome tipo + payload

# ---------------------------------------------------------------------------
# marker types per encode() con controllo del tipo
# ---------------------------------------------------------------------------


class Uuid(bytes):
    pass


class Rect(tuple):
    def __new__(cls, x, y, w, h):
        return super().__new__(cls, (x, y, w, h))


class LongLong(int):
    pass


class UserType(int):
    """QVariant di tipo registrato (es. enum Q_ENUM).

    Sul filo: typeId=1024, isNull, nome tipo (u32 len + bytes + NUL), int32.
    __int__ permette di usarlo come int ovunque.
    """

    def __new__(cls, type_name, value):
        obj = super().__new__(cls, value)
        obj.type_name = type_name
        return obj

    def __repr__(self):
        return f"{self.type_name}({int(self)})"


# ---------------------------------------------------------------------------
# encode: python -> QVariant bytes
# ---------------------------------------------------------------------------


def _header(type_id, is_null=0):
    return struct.pack(">IB", type_id, is_null)


def _enc_str(s):
    data = s.encode("utf-16-be")
    return struct.pack(">I", len(data)) + data


def _enc_cstr(s):
    data = s.encode("utf-8") + b"\x00"
    return struct.pack(">I", len(data)) + data


def encode_variant(v):
    """Serializza un QVariant secondo QDataStream(Qt_5_5)."""
    if isinstance(v, UserType):
        return _header(T_USER) + _enc_cstr(v.type_name) + struct.pack(">i", int(v))
    if isinstance(v, bool):
        return _header(T_BOOL) + struct.pack(">b", int(v))
    if isinstance(v, LongLong):
        return _header(T_LONGLONG) + struct.pack(">q", int(v))
    if isinstance(v, int):
        return _header(T_INT) + struct.pack(">i", v)
    if isinstance(v, (Uuid, uuidlib.UUID)):
        b = v.bytes if isinstance(v, uuidlib.UUID) else bytes(v)
        return _header(T_UUID) + b
    if isinstance(v, Rect):
        return _header(T_RECT) + struct.pack(">4i", *v)
    if isinstance(v, str):
        return _header(T_STRING) + _enc_str(v)
    if isinstance(v, bytes):
        return _header(T_BYTEARRAY) + struct.pack(">I", len(v)) + v
    if isinstance(v, dict):
        out = _header(T_MAP) + struct.pack(">I", len(v))
        for k, val in v.items():
            out += _enc_str(str(k))
            out += encode_variant(val)
        return out
    if isinstance(v, (list, tuple)):
        if v and all(isinstance(x, str) for x in v):
            out = _header(T_STRINGLIST) + struct.pack(">I", len(v))
            for x in v:
                out += _enc_str(x)
            return out
        out = _header(T_LIST) + struct.pack(">I", len(v))
        for x in v:
            out += encode_variant(x)
        return out
    raise TypeError(f"tipo non supportato in encode_variant: {type(v)!r}")


# ---------------------------------------------------------------------------
# decode: bytes -> python
# ---------------------------------------------------------------------------


class VariantError(Exception):
    pass


def _dec_str(buf, off):
    (ln,) = struct.unpack_from(">I", buf, off)
    off += 4
    if ln == 0xFFFFFFFF:
        return None, off
    s = buf[off:off + ln].decode("utf-16-be")
    return s, off + ln


def decode_variant(buf, off=0):
    """Legge un QVariant da buf[off:], ritorna (valore, nuovo_off)."""
    type_id, is_null = struct.unpack_from(">IB", buf, off)
    off += 5

    if type_id == T_BOOL:
        return struct.unpack_from(">b", buf, off)[0] != 0, off + 1
    if type_id == T_INT:
        return struct.unpack_from(">i", buf, off)[0], off + 4
    if type_id == T_UINT:
        return struct.unpack_from(">I", buf, off)[0], off + 4
    if type_id == T_LONGLONG:
        return struct.unpack_from(">q", buf, off)[0], off + 8
    if type_id == T_ULONGLONG:
        return struct.unpack_from(">Q", buf, off)[0], off + 8
    if type_id == T_DOUBLE:
        return struct.unpack_from(">d", buf, off)[0], off + 8
    if type_id == T_STRING:
        return _dec_str(buf, off)
    if type_id == T_BYTEARRAY:
        (ln,) = struct.unpack_from(">I", buf, off)
        off += 4
        if ln == 0xFFFFFFFF:
            return None, off
        return bytes(buf[off:off + ln]), off + ln
    if type_id == T_RECT:
        return tuple(struct.unpack_from(">4i", buf, off)), off + 16
    if type_id == T_UUID:
        return bytes(buf[off:off + 16]), off + 16
    if type_id == T_STRINGLIST:
        (n,) = struct.unpack_from(">I", buf, off)
        off += 4
        out = []
        for _ in range(n):
            s, off = _dec_str(buf, off)
            out.append(s)
        return out, off
    if type_id == T_LIST:
        (n,) = struct.unpack_from(">I", buf, off)
        off += 4
        out = []
        for _ in range(n):
            v, off = decode_variant(buf, off)
            out.append(v)
        return out, off
    if type_id == T_MAP:
        (n,) = struct.unpack_from(">I", buf, off)
        off += 4
        out = {}
        for _ in range(n):
            k, off = _dec_str(buf, off)
            v, off = decode_variant(buf, off)
            out[k] = v
        return out, off

    if type_id == T_USER:
        # tipo registrato: nome (u32 len + bytes + NUL) poi payload.
        # Per gli enum QMetaType::save scrive un int32.
        (ln,) = struct.unpack_from(">I", buf, off)
        off += 4
        name = buf[off:off + ln].rstrip(b"\x00").decode("utf-8", "replace")
        off += ln
        value = struct.unpack_from(">i", buf, off)[0]
        return UserType(name, value), off + 4

    raise VariantError(f"typeId sconosciuto {type_id} a offset {off - 5}")


def decode_all(buf):
    """Decodifica tutti i QVariant in un payload VAM."""
    out, off = [], 0
    while off < len(buf):
        v, off = decode_variant(buf, off)
        out.append(v)
    return out


# ---------------------------------------------------------------------------
# VariantArrayMessage (VAM) framing
# ---------------------------------------------------------------------------


def pack_vam(*values):
    payload = b"".join(encode_variant(v) for v in values)
    return struct.pack(">I", len(payload)) + payload


EMPTY_VAM = struct.pack(">I", 0)


# ---------------------------------------------------------------------------
# FeatureMessage
# ---------------------------------------------------------------------------


class FeatureMessage:
    __slots__ = ("uid", "command", "args")

    def __init__(self, uid, command, args):
        self.uid = uid          # uuid.UUID
        self.command = command  # int
        self.args = args        # dict

    @classmethod
    def parse(cls, payload):
        vals = decode_all(payload)
        uid = uuidlib.UUID(bytes=vals[0]) if isinstance(vals[0], bytes) else None
        return cls(uid, int(vals[1]), vals[2] if len(vals) > 2 else {})

    def serialize_rfb(self):
        body = encode_variant(Uuid(self.uid.bytes))
        body += encode_variant(self.command)
        body += encode_variant(self.args)
        return bytes([MSG_FEATURE]) + struct.pack(">I", len(body)) + body

    def __repr__(self):
        return f"FeatureMessage(uid={self.uid}, cmd={self.command}, args={self.args})"


# ---------------------------------------------------------------------------
# RFB helpers
# ---------------------------------------------------------------------------


def build_server_init(width, height, name=b"Veyon"):
    # pixelFormat: 32bpp, depth 24, bigEndian=0, trueColour=1,
    # rmax=gmax=bmax=255, rshift=16 gshift=8 bshift=0
    pf = struct.pack(">BBBBHHHBBBxxx",
                     32, 24, 0, 1, 255, 255, 255, 16, 8, 0)
    return struct.pack(">HH", width, height) + pf + struct.pack(">I", len(name)) + name


def build_fb_update_raw(x, y, w, h, bpp_bytes):
    """FramebufferUpdate con un rect raw nero."""
    rect_hdr = struct.pack(">HHHHi", x, y, w, h, ENC_RAW)
    return struct.pack(">BxH", MSG_FB_UPDATE, 1) + rect_hdr + (b"\x00" * (w * h * bpp_bytes))


def build_empty_fb_update():
    return struct.pack(">BxH", MSG_FB_UPDATE, 0)
