"""
TANK BATTLE for WIZnet W55MH32L-EVB + ST7789 240x320 (MicroPython 1.27)
1 player, or 2-player co-op over Ethernet (one player per board).

Build on a PC (the sprite renderer is viper code, so the arch flag is needed):
    mpy-cross -march=armv7m -O3 tank.py
Copy tank.mpy and main.py to the board.

Controls: arrows move, A fire, B pause (B again while paused = quit),
A+B together = quit to the title.

Co-op: both boards on the same LAN (or one cable between them), both pick
"2P CO-OP". Both boards run the same simulation in lockstep and only exchange
button states (UDP port 5006), so nothing but inputs crosses the network.

SETUP (title menu) works like the BLOCK FALL one: MAC, DHCP/STATIC, IP, MASK, GW,
PEER auto/manual, peer IP, SOUND. SAVE writes tank_cfg.json and resets.
On first run the settings are taken from blockfall_cfg.json if it exists.
In DHCP mode with no DHCP server the board falls back to 192.168.77.x.
Delete tank_cfg.json to restore the defaults.
"""
import gc
import machine
import micropython
import random
from array import array
from machine import Pin, SPI
from time import ticks_ms, ticks_diff, sleep_ms

# ---------------------------------------------------------------- config
SOUND_ON = True
FRAME = 20            # ms per frame (50 fps target)
MX, MY = 4, 44        # map origin on screen
N = 26                # map is 26x26 cells of 8 px = 208x208 px
EMPTY, BRICK, STEEL, WATER, BASE = 0, 1, 2, 3, 4
UP, RIGHT, DOWN, LEFT = 0, 1, 2, 3
DX = (0, 1, 0, -1)
DY = (-1, 0, 1, 0)
MAX_ON_FIELD = 4      # enemies on the map at once
SPAWNS = ((0, 0), (96, 0), (192, 0))
P_SPAWNS = ((64, 192), (128, 192))
BASE_XY = (96, 192)
HIFILE = "tank_hi.txt"
CFGFILE = "tank_cfg.json"
PORT = 5006
NET_D = 2             # input delay in frames (hides network latency)

# input byte: bits 0-2 direction+1 (0 = none), then buttons
IN_FIRE = 8
IN_B = 16
IN_QUIT = 32

# kind: (speed in 1/4 px per frame, hit points, score)
KIND = {"n": (3, 1, 100), "f": (5, 1, 200), "a": (2, 3, 400)}

# 13x13 blocks of 16 px.  . empty  B brick  S steel  W water
# Row 0 cols 0/6/12 are enemy spawns; rows 11-12 cols 4-8 are kept clear
# for the player spawns and the brick fort around the base.
STAGES = (
    ("............."
     "..BB..S..BB.."
     "..BB.....BB.."
     "......B......"
     ".SS..BBB..SS."
     ".....B.B....."
     ".BB.......BB."
     ".BB..WWW..BB."
     ".....B.B....."
     "..B.......B.."
     "..B..B.B..B.."
     ".B.........B."
     "............."),
    ("............."
     ".S..B...B..S."
     ".S..B.W.B..S."
     "....B.W.B...."
     "BB.........BB"
     "...SS...SS..."
     ".B...BBB...B."
     ".B.W.....W.B."
     ".B.W.B.B.W.B."
     ".....B.B....."
     ".BB.......BB."
     "............."
     "............."),
    ("............."
     "BBB.SS.SS.BBB"
     "......W......"
     ".BB.BBWBB.BB."
     ".B...S.S...B."
     ".B.WW...WW.B."
     "...B..S..B..."
     "SS.B.BBB.B.SS"
     "...B.....B..."
     ".WW..B.B..WW."
     "..B..BBB..B.."
     ".S.........S."
     "............."),
)
ROSTERS = ("nnnnnfnnnfnn", "nnfnfnanfnna", "fnafnafnafaa")
CO_EXTRA = "nfa"      # co-op: 3 more enemies per stage


def c565(r, g, b):
    return ((r & 0xF8) << 8) | ((g & 0xFC) << 3) | (b >> 3)


def sw(c):                      # byte-swap for blit_buffer data
    return ((c & 0xFF) << 8) | (c >> 8)


BLACK = 0
WHITE = c565(255, 255, 255)
GRAY = c565(130, 130, 130)
DGRAY = c565(60, 60, 60)
YELLOW = c565(255, 220, 40)
GREEN = c565(80, 210, 80)
ORANGE = c565(255, 120, 0)
RED = c565(230, 30, 30)
BG2 = c565(100, 100, 100)
PCOL = (YELLOW, GREEN)

# ---------------------------------------------------------------- font
# 3x5 font: each glyph is 5 octal digits, one per row (bit 4 = left pixel)
FCH = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789:-!/.>"
FDAT = ("25755" "65656" "34443" "65556" "74647" "74644" "34553" "55755"
        "72227" "11152" "55655" "44447" "57755" "65555" "25552" "65644"
        "25563" "65655" "34216" "72222" "55557" "55552" "55775" "55255"
        "55222" "71247"
        "75557" "26227" "61247" "61216" "55711" "74616" "34757" "71222"
        "75757" "75716"
        "02020" "00700" "22202" "11244" "00002" "42124")

LCD = None


def text(x, y, s, sc, col):
    fr = LCD.fill_rect
    for ch in s:
        i = FCH.find(ch)
        if i >= 0:
            for r in range(5):
                v = ord(FDAT[i * 5 + r]) - 48
                c = 0
                while c < 3:
                    if v & (4 >> c):
                        e = c
                        while e < 2 and v & (4 >> (e + 1)):
                            e += 1
                        fr(x + c * sc, y + r * sc, (e - c + 1) * sc, sc, col)
                        c = e + 1
                    else:
                        c += 1
        x += 4 * sc


def twidth(s, sc):
    return len(s) * 4 * sc - sc


def ctext(y, s, sc, col):              # centred on the screen
    text((240 - twidth(s, sc)) // 2, y, s, sc, col)


def mtext(y, s, sc, col):              # centred on the map
    text(MX + 104 - twidth(s, sc) // 2, y, s, sc, col)


# ---------------------------------------------------------------- graphics
@micropython.viper
def _render(dst: ptr16, spr: ptr8, pal: ptr16, ph: int):
    # 16x16 palette sprite -> RGB565 buffer; ph swaps the two tread shades
    i = 0
    while i < 256:
        c = spr[i]
        if ph:
            if c == 1:
                c = 2
            elif c == 2:
                c = 1
        dst[i] = pal[c]
        i += 1


def mk_tile(kind):
    b = bytearray(128)
    for y in range(8):
        for x in range(8):
            if kind == BRICK:
                if y == 3 or y == 7 or (y < 3 and x == 3) or (3 < y < 7 and x == 7):
                    c = c565(110, 100, 90)
                elif y == 0 or y == 4:
                    c = c565(220, 110, 60)
                else:
                    c = c565(170, 60, 20)
            elif kind == STEEL:
                if x == 7 or y == 7:
                    c = c565(80, 80, 90)
                elif x == 0 or y == 0:
                    c = c565(245, 245, 245)
                elif 2 <= x <= 5 and 2 <= y <= 5:
                    c = c565(215, 215, 225)
                else:
                    c = c565(165, 165, 175)
            else:
                if ((y & 3) == 1 and (x & 3) < 2) or ((y & 3) == 3 and (x & 3) >= 2):
                    c = c565(90, 150, 240)
                else:
                    c = c565(20, 50, 170)
            i = (y * 8 + x) * 2
            b[i] = c >> 8
            b[i + 1] = c & 0xFF
    return b


def mk_sprites():
    # palette indices: 0 bg, 1/2 tread shades, 3 body, 4 turret, 5 barrel
    up = bytearray(256)
    for y in range(16):
        for x in range(16):
            if x < 3 or x > 12:
                c = 1 if (y >> 1) & 1 else 2
            elif 3 <= y <= 14:
                c = 3
            else:
                c = 0
            if 5 <= x <= 10 and 6 <= y <= 11:
                c = 4
            if 7 <= x <= 8 and y <= 8:
                c = 5
            up[y * 16 + x] = c
    sp = [up]
    for d in (RIGHT, DOWN, LEFT):
        b = bytearray(256)
        for y in range(16):
            for x in range(16):
                if d == RIGHT:
                    v = up[(15 - x) * 16 + y]
                elif d == DOWN:
                    v = up[(15 - y) * 16 + (15 - x)]
                else:
                    v = up[x * 16 + (15 - y)]
                b[y * 16 + x] = v
        sp.append(b)
    return sp


def mk_pal(t1, t2, body, tur, bar):
    return array("H", [0, sw(t1), sw(t2), sw(body), sw(tur), sw(bar)])


# ---------------------------------------------------------------- sound
TONES = (196, 262, 330, 392, 523, 659, 784, 1047, 1568)
NZL, NZS = 9, 10        # loud / soft noise buffers
S = -1                  # silence
FX_FIRE = ((8, 1), (6, 1), (4, 1))
FX_STEEL = ((7, 2),)
FX_BRICK = ((NZS, 2),)
FX_HIT = ((5, 1), (S, 1), (5, 1))
FX_BOOM = ((NZL, 5), (NZS, 8))
FX_DIE = ((NZL, 10), (NZS, 14), (0, 12))
FX_START = ((4, 10), (S, 2), (5, 10), (S, 2), (6, 10), (S, 2), (7, 24))
FX_CLEAR = ((6, 8), (S, 2), (7, 8), (S, 2), (8, 20))
FX_OVER = ((3, 20), (S, 3), (2, 20), (S, 3), (1, 40))
FX_LINK = ((5, 6), (S, 2), (7, 10))


class Sound:
    """Non-blocking SFX player: the I2S irq callback feeds one chunk at a time."""

    def __init__(self, on):
        self.i2s = None
        self.kbd = False
        self.seq = None
        self.si = 0
        self.left = 0
        self.pri = 0
        if not on:
            return
        try:
            from machine import I2S
        except ImportError:
            return
        for ib in (2048, 4096):         # small ibuf = low latency for SFX
            try:
                self.i2s = I2S(3, sck=Pin("PB3"), ws=Pin("PA15"), sd=Pin("PB5"),
                               mode=I2S.TX, bits=16, format=I2S.MONO,
                               rate=16000, ibuf=ib)
                break
            except Exception:
                self.i2s = None
                gc.collect()
        if self.i2s is None:
            return
        self.sil = bytearray(256)
        bufs = []
        for f in TONES:                 # 128 samples = 8 ms square wave
            b = array("h", bytes(256))
            for i in range(128):
                b[i] = -1800 if ((i * f * 2) // 16000) & 1 else 1800
            bufs.append(b)
        for amp, hold in ((5000, 2), (2000, 3)):   # 256 samples = 16 ms noise
            b = array("h", bytes(512))
            v = 0
            for i in range(256):
                if i % hold == 0:
                    v = (random.getrandbits(12) - 2048) * amp // 2048
                b[i] = v
            bufs.append(b)
        self.bufs = bufs
        try:
            self.i2s.irq(self._cb)
            self.i2s.write(self.sil)
        except Exception:
            self.deinit()

    def _next(self):
        q = self.seq
        if q is None:
            return self.sil
        if self.left <= 0:
            self.si += 1
            if self.si >= len(q):
                self.seq = None
                self.pri = 0
                return self.sil
            self.left = q[self.si][1]
        self.left -= 1
        i = q[self.si][0]
        return self.sil if i < 0 else self.bufs[i]

    def _cb(self, _):
        try:
            self.i2s.write(self._next())
        except KeyboardInterrupt:
            self.kbd = True             # re-raised in the main loop
        except Exception:
            pass

    def play(self, q, pri=1):
        if self.i2s is None or pri < self.pri:
            return
        self.seq = None
        self.si = -1
        self.left = 0
        self.pri = pri
        self.seq = q

    def deinit(self):
        i = self.i2s
        if i is None:
            return
        self.i2s = None
        try:
            i.irq(None)
            i.write(self.sil)
            sleep_ms(50)
            i.deinit()
            sleep_ms(150)
        except Exception:
            pass


# ---------------------------------------------------------------- input
class Btn:
    def __init__(self, name):
        self.p = Pin(name, Pin.IN, Pin.PULL_UP)
        self.st = False
        self.t = 0
        self.r = 0

    def rep(self, ms):              # press edge, then auto-repeat while held
        now = ticks_ms()
        if self.pressed():
            self.r = now + 350
            return True
        if self.st and self.p.value() == 0 and ticks_diff(now, self.r) >= 0:
            self.r = now + ms
            return True
        return False

    def down(self):
        return self.p.value() == 0

    def pressed(self):              # debounced press edge
        d = self.p.value() == 0
        if d != self.st:
            now = ticks_ms()
            if ticks_diff(now, self.t) > 25:
                self.st = d
                self.t = now
                return d
        return False


# ---------------------------------------------------------------- network
_NIC = None
_PWN = None


def ipstr(a):
    return ".".join([str(x) for x in a])


def board_id():
    """3 bytes that should differ between boards. Returns (bytes, stable?)."""
    try:
        uid = machine.unique_id()
        h = [0, 0, 0]
        for i in range(len(uid)):       # fold the whole chip id into 3 bytes
            h[i % 3] = (h[i % 3] * 31 + uid[i]) & 0xFF
        return h, True
    except Exception:
        pass
    try:                                # hardware RNG, if the port has one
        import os
        return list(os.urandom(3)), False
    except Exception:
        pass
    try:
        from time import ticks_us
        random.seed(ticks_us())
    except Exception:
        pass
    return [random.getrandbits(8) for _ in range(3)], False


def default_cfg():
    h = board_id()[0]
    return {"mac": [0x00, 0x08, 0xDC] + h, "dhcp": True,
            "ip": [192, 168, 1, 20], "mask": [255, 255, 255, 0],
            "gw": [192, 168, 1, 1], "sound": True,
            "peer_auto": True, "peer": [192, 168, 1, 21]}


def load_cfg():
    """Same keys and format as blockfall_cfg.json, so its settings carry over."""
    cfg = default_cfg()
    import json
    found = False
    for fn in (CFGFILE, "blockfall_cfg.json"):
        try:
            with open(fn) as f:
                d = json.load(f)
        except Exception:
            continue
        for k, v in cfg.items():        # keep only well-formed values
            x = d.get(k)
            if isinstance(v, list):
                if isinstance(x, list) and len(x) == len(v):
                    cfg[k] = [int(b) & 0xFF for b in x]
            elif isinstance(x, bool):
                cfg[k] = x
        print("cfg: loaded", fn)
        found = True
        break
    if not found:
        # no settings yet: save them now so a random MAC stays the same
        try:
            save_cfg(cfg)
            print("cfg: created", CFGFILE)
        except Exception:
            pass
    return cfg


def save_cfg(cfg):
    import json
    with open(CFGFILE, "w") as f:
        json.dump(cfg, f)


def net_up(cfg):
    """Bring up the WIZnet NIC once per boot. Returns (ip, mask, board id)."""
    global _NIC, _PWN
    if _NIC is None:
        import network
        spi = SPI(2, baudrate=10_000_000, polarity=0, phase=0)
        _PWN = Pin("PE15", Pin.OUT, value=0)
        nic = network.WIZNET5K(spi, Pin("PB12", Pin.OUT), Pin("PD9", Pin.OUT))
        nic.active(True)
        try:
            nic.config(mac=bytes(cfg["mac"]))
        except Exception as e:
            print("net: mac not set:", e)
        _NIC = nic
        if not cfg["dhcp"]:
            nic.ifconfig((ipstr(cfg["ip"]), ipstr(cfg["mask"]),
                          ipstr(cfg["gw"]), "8.8.8.8"))
        else:
            try:
                nic.ifconfig("dhcp")
            except Exception:
                pass
            t = ticks_ms()
            while ticks_diff(ticks_ms(), t) < 8000:
                try:
                    if nic.isconnected() and nic.ifconfig()[0] != "0.0.0.0":
                        break
                except Exception:
                    pass
                sleep_ms(200)
            if nic.ifconfig()[0] == "0.0.0.0":
                # no DHCP server (e.g. boards cabled together): fixed fallback
                nic.ifconfig(("192.168.77.%d" % (10 + cfg["mac"][5] % 200),
                              "255.255.255.0", "192.168.77.1", "8.8.8.8"))
    c = _NIC.ifconfig()
    return c[0], c[1], bytes(cfg["mac"][3:6])


def bcast_addr(ip, mask):
    a = ip.split(".")
    m = mask.split(".")
    return ".".join([str((int(a[i]) & int(m[i])) | (255 ^ int(m[i]))) for i in range(4)])


class Net:
    """Lockstep link. Packets:
    H id3             hello (broadcast while searching), answered with J
    J id3 seed2       join: seed of the sender
    S sess stage      ready for a stage
    T sess stage f3 in8   my inputs for frames f-7..f (redundant vs. loss)
    """

    def __init__(self, ip, mask, myid, fixed=None, sock=None):
        self.ip = ip
        self.bc = bcast_addr(ip, mask)
        self.myid = myid
        self.fixed = fixed
        if sock is None:
            import socket
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            except Exception:
                pass
            sock.bind(("0.0.0.0", PORT))
            sock.setblocking(False)
        self.s = sock
        self.peer = None
        self.pid = b""
        self.seed = 1
        self.pseed = 1
        self.gseed = 1
        self.jpkt = b"J"
        self.linked = False
        self.hunting = False
        self.active = False
        self.me = 0
        self.sess = 0
        self.stage = -1
        self.seen = -1
        self.top = 0
        self.mine = bytearray(64)
        self.pin = bytearray(64)
        self.pf = array("i", [-1] * 64)
        self.tb = bytearray(14)
        self.tb_ok = False
        self.tr = 0

    def _send(self, pkt, ip=None):
        try:
            self.s.sendto(pkt, (ip or self.peer, PORT))
        except Exception:
            pass

    def _rx(self):
        while True:
            try:
                d, a = self.s.recvfrom(32)
            except Exception:
                return
            ip = a[0]
            if ip == self.ip or len(d) < 2:
                continue
            k = d[0]
            if self.hunting and self.peer is None and (k == 72 or k == 74) and len(d) >= 4:
                self.peer = ip
            if ip != self.peer:
                continue
            if k == 72:                                     # H
                if len(d) >= 4:
                    self.pid = bytes(d[1:4])
                    self._send(self.jpkt)
            elif k == 74:                                   # J
                if len(d) >= 6:
                    self.pid = bytes(d[1:4])
                    self.pseed = d[4] | (d[5] << 8)
                    self.linked = True
            elif not self.active or len(d) < 3 or d[1] != self.sess or d[2] != self.stage:
                continue
            elif k == 83:                                   # S
                self.seen = self.stage
            elif k == 84 and len(d) >= 14:                  # T
                self.seen = self.stage
                f = d[3] | (d[4] << 8) | (d[5] << 16)
                for j in range(8):
                    fr = f - 7 + j
                    if fr >= 0:
                        i = fr & 63
                        self.pin[i] = d[6 + j]
                        self.pf[i] = fr

    def connect(self, g):
        """Find the other board. Returns False if B is pressed."""
        self.peer = self.fixed
        self.pid = b""
        self.linked = False
        self.active = False
        self.tb_ok = False
        self.hunting = True
        self.seed = ((ticks_ms() * 7) ^ (self.myid[0] << 8) ^ self.myid[1]) & 0xFFFF or 1
        self.jpkt = b"J" + self.myid + bytes((self.seed & 255, self.seed >> 8))
        hp = b"H" + self.myid
        t = ticks_ms() - 1000
        while not self.linked:
            g.check_kbd()
            if g.bb.pressed():
                self.hunting = False
                return False
            if ticks_diff(ticks_ms(), t) >= 300:
                t = ticks_ms()
                if self.fixed is None:
                    self._send(hp, self.bc)
                if self.peer:
                    self._send(hp)
            self._rx()
            sleep_ms(10)
        self.hunting = False
        if self.myid != self.pid:
            p1 = self.myid < self.pid
        else:
            p1 = self.ip < self.peer
        self.me = 0 if p1 else 1
        self.gseed = self.seed if p1 else self.pseed
        self.sess = self.gseed & 255
        self.active = True
        return True

    def service(self):
        """Keep the link alive while not exchanging inputs (waits, screens)."""
        if not self.active:
            return
        self._rx()
        now = ticks_ms()
        if self.tb_ok and ticks_diff(now, self.tr) > 50:
            self.tr = now
            self._send(self.tb)     # the peer may still need our last frames

    def begin_stage(self, stage):
        self.stage = stage & 255
        self.seen = -1
        for i in range(64):
            self.pf[i] = -1
            self.mine[i] = 0
        for k in range(NET_D):
            self.pf[k] = k
            self.pin[k] = 0
        self.top = NET_D - 1

    def barrier(self, g):
        pkt = bytes((83, self.sess, self.stage))
        t0 = ticks_ms()
        t = t0 - 1000
        while self.seen != self.stage:
            g.check_kbd()
            now = ticks_ms()
            if ticks_diff(now, t) >= 60:
                t = now
                self._send(pkt)
            self.service()
            if ticks_diff(now, t0) > 15000:
                return False
            sleep_ms(5)
        self._send(pkt)
        return True

    def _build(self):
        b = self.tb
        t = self.top
        b[0] = 84
        b[1] = self.sess
        b[2] = self.stage
        b[3] = t & 255
        b[4] = (t >> 8) & 255
        b[5] = (t >> 16) & 255
        for j in range(8):
            fr = t - 7 + j
            b[6 + j] = self.mine[fr & 63] if fr >= 0 else 0
        self.tb_ok = True

    def exchange(self, f, my, g):
        """Queue my input for frame f+NET_D, return (P1, P2) inputs for frame f."""
        t = f + NET_D
        self.mine[t & 63] = my
        self.top = t
        self._build()
        self._send(self.tb)
        self.tr = t0 = ticks_ms()
        i = f & 63
        while True:
            self._rx()
            if self.pf[i] == f:
                break
            now = ticks_ms()
            if ticks_diff(now, t0) > 3000:
                return None
            if ticks_diff(now, self.tr) > 30:
                self.tr = now
                self._send(self.tb)
            g.check_kbd()
            sleep_ms(1)
        a = self.mine[i]
        b = self.pin[i]
        return (a, b) if self.me == 0 else (b, a)

    def finish(self):
        t = ticks_ms()
        while ticks_diff(ticks_ms(), t) < 800:
            self.service()
            sleep_ms(10)
        self.active = False
        self.tb_ok = False


# ---------------------------------------------------------------- objects
class Tank:
    def __init__(self, kind, x, y, d, spd, hp, pal):
        self.kind = kind
        self.x = x
        self.y = y
        self.d = d
        self.spd = spd
        self.hp = hp
        self.pal = pal
        self.acc = 0        # movement accumulator (1/4 px)
        self.mc = 0         # pixels moved (tread animation)
        self.b = None       # active bullet
        self.sp = 40        # spawn sparkle frames left
        self.inv = 0        # invulnerable frames left
        self.turn = 0       # AI: frames until a new direction
        self.pts = 0
        self.pi = 0         # player index


class Bullet:
    def __init__(self, x, y, d, own, spd):
        self.x = x
        self.y = y
        self.d = d
        self.own = own
        self.spd = spd
        self.on = True


def load_hi():
    try:
        with open(HIFILE) as f:
            return int(f.read())
    except Exception:
        return 0


def save_hi(v):
    try:
        with open(HIFILE, "w") as f:
            f.write(str(v))
    except Exception:
        pass


# ---------------------------------------------------------------- game
MENU = ("1 PLAYER", "2P CO-OP", "SETUP")
ITEMS = ("MAC", "MODE", "IP", "MASK", "GW", "PEER", "P.IP", "SOUND", "SAVE", "BACK")
LISTS = {"MAC": "mac", "IP": "ip", "MASK": "mask", "GW": "gw", "P.IP": "peer"}


def item_fields(cfg, key):
    """-> (list of field strings, separator)"""
    if key == "MAC":
        return ["%02X" % b for b in cfg["mac"]], ":"
    if key == "MODE":
        return ["DHCP" if cfg["dhcp"] else "STATIC"], ""
    if key == "PEER":
        return ["AUTO" if cfg["peer_auto"] else "MANUAL"], ""
    if key == "SOUND":
        return ["ON" if cfg["sound"] else "OFF"], ""
    if key == "SAVE":
        return ["AND RESET"], ""
    if key in LISTS:
        return [str(x) for x in cfg[LISTS[key]]], "."
    return [""], ""


class Game:
    def __init__(self, lcd, snd, cfg):
        self.lcd = lcd
        self.snd = snd
        self.cfg = cfg
        self.M = bytearray(N * N)
        self.tiles = (None, mk_tile(BRICK), mk_tile(STEEL), mk_tile(WATER))
        self.spr = mk_sprites()
        self.rb = bytearray(512)
        self.PPAL = (
            mk_pal(c565(90, 70, 0), c565(200, 160, 20), c565(240, 200, 40),
                   c565(160, 120, 0), c565(255, 240, 150)),
            mk_pal(c565(20, 80, 20), c565(60, 170, 60), c565(80, 210, 80),
                   c565(30, 120, 30), c565(190, 255, 190)),
        )
        self.PINV = mk_pal(c565(150, 150, 150), c565(255, 255, 255), c565(255, 255, 220),
                           c565(200, 200, 200), WHITE)
        self.EPAL = {
            "n": mk_pal(c565(60, 60, 60), c565(140, 140, 140), c565(180, 180, 190),
                        c565(100, 100, 110), c565(230, 230, 235)),
            "f": mk_pal(c565(30, 70, 60), c565(70, 160, 140), c565(90, 200, 170),
                        c565(40, 120, 100), c565(200, 255, 240)),
        }
        self.APAL = (   # armored tank colour by remaining hp 1..3
            mk_pal(c565(80, 80, 40), c565(160, 160, 90), c565(200, 200, 120),
                   c565(120, 120, 60), c565(240, 240, 190)),
            mk_pal(c565(100, 50, 10), c565(200, 110, 40), c565(230, 130, 40),
                   c565(140, 70, 20), c565(255, 200, 150)),
            mk_pal(c565(90, 20, 20), c565(180, 60, 60), c565(220, 60, 50),
                   c565(130, 30, 30), c565(255, 170, 160)),
        )
        self.bl = Btn("PC6")
        self.br = Btn("PG8")
        self.bu = Btn("PA8")
        self.bd = Btn("PC7")
        self.ba = Btn("PF13")
        self.bb = Btn("PF11")
        self.hi = load_hi()
        self.net = None
        self.coop = False
        self.np = 1
        self.sel = 0
        self.rs = 1
        self.score = 0
        self.lives = [2, 2]
        self.out = [False, False]
        self.resp = [-1, -1]
        self.pl = [None, None]
        self.stage = 1
        self.tanks = []
        self.bullets = []
        self.boom = []
        self.roster = ""
        self.ei = 0

    # ------------------------------------------------ helpers
    def rnd(self, n):
        # xorshift16: identical sequence on both boards, no bigints
        x = self.rs
        x ^= (x << 7) & 0xFFFF
        x ^= x >> 9
        x ^= (x << 8) & 0xFFFF
        self.rs = x
        return x % n

    def check_kbd(self):
        if self.snd.kbd:
            self.snd.kbd = False
            raise KeyboardInterrupt

    def wait(self, ms):
        t = ticks_ms()
        while ticks_diff(ticks_ms(), t) < ms:
            self.check_kbd()
            if self.coop:
                self.net.service()
            sleep_ms(10)

    def wait_ab(self):
        while True:
            self.check_kbd()
            if self.ba.pressed():
                return 1
            if self.bb.pressed():
                return 2
            sleep_ms(10)

    def n_enemies(self):
        n = 0
        for t in self.tanks:
            if t.kind != "p":
                n += 1
        return n

    def read_input(self):
        v = 0
        if self.bu.down():
            v = 1 + UP
        elif self.bd.down():
            v = 1 + DOWN
        elif self.bl.down():
            v = 1 + LEFT
        elif self.br.down():
            v = 1 + RIGHT
        if self.ba.pressed():
            v |= IN_FIRE
        if self.bb.pressed():
            v |= IN_B
        if self.ba.down() and self.bb.down():
            v |= IN_QUIT
        return v

    # ------------------------------------------------ screens
    def big_tank(self, x0, y0, s):
        spr = self.spr[UP]
        pal = self.PPAL[0]
        fr = self.lcd.fill_rect
        for y in range(16):
            x = 0
            while x < 16:
                c = spr[y * 16 + x]
                e = x
                while e < 15 and spr[y * 16 + e + 1] == c:
                    e += 1
                if c:
                    fr(x0 + x * s, y0 + y * s, (e - x + 1) * s, s, sw(pal[c]))
                x = e + 1

    def draw_menu(self):
        fr = self.lcd.fill_rect
        fr(40, 210, 16, 58, BLACK)
        text(44, 212 + self.sel * 20, ">", 2, YELLOW)
        for i in range(len(MENU)):
            text(64, 212 + i * 20, MENU[i], 2, WHITE if self.sel == i else GRAY)

    def title(self):
        self.lcd.fill(BLACK)
        ctext(28, "TANK", 8, YELLOW)
        ctext(80, "BATTLE", 5, WHITE)
        self.big_tank(88, 120, 4)
        self.draw_menu()
        ctext(282, "HI %06d" % self.hi, 2, GRAY)
        ctext(302, "B: GAMES MENU", 2, DGRAY)
        n = len(MENU)
        while True:
            self.check_kbd()
            if self.bu.pressed():
                self.sel = (self.sel - 1) % n
                self.draw_menu()
            elif self.bd.pressed():
                self.sel = (self.sel + 1) % n
                self.draw_menu()
            if self.ba.pressed():
                return self.sel
            if self.bb.pressed():
                return -1
            sleep_ms(10)

    def main(self):
        while True:
            s = self.title()
            if s < 0:
                return
            if s == 0:
                self.play_game(False)
            elif s == 1:
                if self.link():
                    self.play_game(True)
            else:
                self.setup()

    # ------------------------------------------------ on-screen setup
    def draw_item(self, cfg, i, sel, field=-1):
        lcd = self.lcd
        key = ITEMS[i]
        y = 44 + i * 23
        lcd.fill_rect(0, y - 2, 240, 14, BLACK)
        static_only = ((key in ("IP", "MASK", "GW") and cfg["dhcp"]) or
                       (key == "P.IP" and cfg["peer_auto"]))
        on = i == sel
        text(4, y, (">" if on else " ") + key, 2, YELLOW if on else GRAY)
        fields, sep = item_fields(cfg, key)
        x = 70
        for j in range(len(fields)):
            f = fields[j]
            if j == field:
                c = YELLOW
            elif static_only:
                c = DGRAY
            else:
                c = WHITE if on else GRAY
            text(x, y, f, 2, c)
            x += len(f) * 8
            if sep and j < len(fields) - 1:
                text(x, y, sep, 2, GRAY)
                x += 8

    def setup_help(self, editing):
        self.lcd.fill_rect(0, 285, 240, 20, BLACK)
        ctext(290, "UD:VALUE LR:MOVE A:OK" if editing else "A:EDIT  B:BACK", 2, GRAY)

    def setup(self):
        lcd = self.lcd
        cfg = {}
        for k, v in self.cfg.items():           # edit a copy; B throws it away
            cfg[k] = list(v) if isinstance(v, list) else v
        lcd.fill(BLACK)
        ctext(12, "SETUP", 3, WHITE)
        sel = 0
        for i in range(len(ITEMS)):
            self.draw_item(cfg, i, sel)
        if _NIC is not None:
            ctext(268, "NOW " + _NIC.ifconfig()[0], 2, DGRAY)
        self.setup_help(False)
        n = len(ITEMS)
        while True:
            self.check_kbd()
            old = sel
            if self.bu.rep(150):
                sel = (sel - 1) % n
            elif self.bd.rep(150):
                sel = (sel + 1) % n
            elif self.bb.pressed():
                return                          # discard changes
            elif self.ba.pressed():
                key = ITEMS[sel]
                if key == "BACK":
                    return
                if key == "MODE":
                    cfg["dhcp"] = not cfg["dhcp"]
                    for i in range(n):
                        self.draw_item(cfg, i, sel)
                elif key == "PEER":
                    cfg["peer_auto"] = not cfg["peer_auto"]
                    for i in range(n):
                        self.draw_item(cfg, i, sel)
                elif key == "SOUND":
                    cfg["sound"] = not cfg["sound"]
                    self.draw_item(cfg, sel, sel)
                elif key == "SAVE":
                    self.save_and_reset(cfg)
                else:
                    self.edit_list(cfg, sel, LISTS[key])
            if sel != old:
                self.draw_item(cfg, old, sel)
                self.draw_item(cfg, sel, sel)
            sleep_ms(10)

    def edit_list(self, cfg, sel, k):
        vals = cfg[k]
        backup = list(vals)
        j = 0
        self.setup_help(True)
        self.draw_item(cfg, sel, sel, j)
        while True:
            self.check_kbd()
            ch = False
            if self.bu.rep(80):
                vals[j] = (vals[j] + 1) & 0xFF
                ch = True
            elif self.bd.rep(80):
                vals[j] = (vals[j] - 1) & 0xFF
                ch = True
            elif self.br.rep(200):
                j = (j + 1) % len(vals)
                ch = True
            elif self.bl.rep(200):
                j = (j - 1) % len(vals)
                ch = True
            elif self.ba.pressed():
                break                           # keep the change
            elif self.bb.pressed():
                vals[:] = backup                # undo
                break
            if ch:
                self.draw_item(cfg, sel, sel, j)
            sleep_ms(10)
        if k == "mac":
            vals[0] &= 0xFE                     # keep the MAC unicast
        self.draw_item(cfg, sel, sel)
        self.setup_help(False)

    def save_and_reset(self, cfg):
        lcd = self.lcd
        lcd.fill(BLACK)
        try:
            save_cfg(cfg)
            ctext(130, "SAVED", 4, GREEN)
            ctext(175, "RESETTING...", 2, GRAY)
        except Exception as e:
            print("save error:", e)
            ctext(130, "SAVE FAILED", 3, RED)
            sleep_ms(1500)
            return
        self.snd.deinit()
        sleep_ms(800)
        machine.reset()

    # ------------------------------------------------ co-op link
    def link(self):
        lcd = self.lcd
        cfg = self.cfg
        lcd.fill(BLACK)
        ctext(30, "CO-OP", 6, GREEN)
        ctext(110, "STARTING NETWORK", 2, WHITE)
        if self.net is None:
            try:
                ip, mask, myid = net_up(cfg)
                peer = None if cfg["peer_auto"] else ipstr(cfg["peer"])
                self.net = Net(ip, mask, myid, peer)
            except Exception as e:
                print("net error:", e)
                lcd.fill_rect(0, 100, 240, 40, BLACK)
                ctext(110, "NETWORK ERROR", 2, RED)
                ctext(132, str(e).upper()[:28], 1, GRAY)
                ctext(240, "CHECK SETUP", 2, WHITE)
                ctext(270, "PRESS A", 2, GRAY)
                self.wait_ab()
                return False
        lcd.fill_rect(0, 100, 240, 40, BLACK)
        ctext(100, "IP " + self.net.ip, 2, GRAY)
        if cfg["dhcp"] and self.net.ip.startswith("192.168.77."):
            ctext(120, "NO DHCP: FALLBACK IP", 1, DGRAY)
        if self.net.fixed:
            ctext(190, "PEER " + self.net.fixed, 2, DGRAY)
        ctext(140, "WAITING FOR", 2, WHITE)
        ctext(160, "THE OTHER BOARD", 2, WHITE)
        ctext(270, "B: CANCEL", 2, GRAY)
        gc.collect()
        if not self.net.connect(self):
            return False
        self.coop = True
        self.snd.play(FX_LINK, 2)
        lcd.fill_rect(0, 136, 240, 40, BLACK)
        lcd.fill_rect(0, 262, 240, 20, BLACK)
        ctext(150, "LINKED! YOU ARE %dP" % (self.net.me + 1), 2, PCOL[self.net.me])
        self.wait(1200)
        return True

    def play_game(self, coop):
        self.coop = coop
        self.np = 2 if coop else 1
        self.score = 0
        self.lives = [2, 2]
        self.out = [False, False]
        self.stage = 1
        while True:
            r = self.play_stage()
            if r != "clear":
                break
            self.stage += 1
        if coop:
            self.net.finish()
            self.coop = False
        new = self.score > self.hi
        if new:
            self.hi = self.score
            save_hi(self.hi)
        if r == "quit":
            return
        self.lcd.fill(BLACK)
        if r == "lost":
            ctext(80, "LINK", 6, RED)
            ctext(122, "LOST", 6, RED)
        else:
            ctext(80, "GAME", 6, RED)
            ctext(122, "OVER", 6, RED)
        ctext(190, "SCORE %06d" % self.score, 2, WHITE)
        if new:
            ctext(212, "NEW HI SCORE!", 2, YELLOW)
        ctext(260, "PRESS A", 2, GRAY)
        self.wait_ab()

    # ------------------------------------------------ stage setup / HUD
    def load_stage(self):
        M = self.M
        for i in range(N * N):
            M[i] = 0
        data = STAGES[(self.stage - 1) % len(STAGES)]
        for br in range(13):
            for bc in range(13):
                t = ".BSW".find(data[br * 13 + bc])
                if t > 0:
                    i = 2 * br * N + 2 * bc
                    M[i] = t
                    M[i + 1] = t
                    M[i + N] = t
                    M[i + N + 1] = t
        for c in range(11, 15):                 # brick fort around the base
            M[23 * N + c] = BRICK
        for r in (24, 25):
            M[r * N + 11] = BRICK
            M[r * N + 14] = BRICK
            M[r * N + 12] = BASE
            M[r * N + 13] = BASE

    def draw_map(self):
        lcd = self.lcd
        fr = lcd.fill_rect
        fr(MX, MY, 208, 208, BLACK)
        fr(MX - 2, MY - 2, 212, 2, DGRAY)
        fr(MX - 2, MY + 208, 212, 2, DGRAY)
        fr(MX - 2, MY, 2, 208, DGRAY)
        fr(MX + 208, MY, 2, 208, DGRAY)
        M = self.M
        for r in range(N):
            for c in range(N):
                t = M[r * N + c]
                if t and t != BASE:
                    lcd.blit_buffer(self.tiles[t], MX + c * 8, MY + r * 8, 8, 8)
        self.draw_base()

    def draw_base(self):
        fr = self.lcd.fill_rect
        x = MX + BASE_XY[0]
        y = MY + BASE_XY[1]
        fr(x, y, 16, 16, BLACK)
        if self.base_ok:
            fr(x + 2, y + 13, 12, 3, GRAY)
            fr(x + 4, y + 11, 8, 2, c565(170, 170, 170))
            fr(x + 5, y + 1, 2, 10, WHITE)
            fr(x + 7, y + 2, 7, 5, YELLOW)
            fr(x + 9, y + 3, 3, 3, RED)
        else:
            fr(x + 1, y + 10, 14, 6, DGRAY)
            fr(x + 3, y + 7, 5, 4, GRAY)
            fr(x + 9, y + 8, 4, 3, DGRAY)

    def draw_top(self):
        self.lcd.fill_rect(0, 0, 240, MY - 2, BLACK)
        text(4, 10, "SC", 3, YELLOW)
        text(124, 10, "HI", 3, GRAY)
        text(152, 10, "%06d" % self.hi, 3, GRAY)
        self.draw_score()

    def draw_score(self):
        self.lcd.fill_rect(32, 10, 72, 15, BLACK)
        text(32, 10, "%06d" % self.score, 3, WHITE)

    def icon(self, i, on):
        fr = self.lcd.fill_rect
        x = 216 + (i & 1) * 10
        y = MY + 4 + (i >> 1) * 10
        fr(x, y, 8, 8, GRAY if on else BLACK)
        if on:
            fr(x + 2, y + 2, 4, 4, DGRAY)

    def draw_panel(self):
        fr = self.lcd.fill_rect
        fr(214, MY, 26, 208, BLACK)
        for i in range(len(self.roster) - self.ei):
            self.icon(i, True)
        for i in range(self.np):
            text(216, MY + 112 + i * 30, "%dP" % (i + 1), 2, PCOL[i])
            self.draw_lives(i)
        fr(217, MY + 172, 2, 14, WHITE)
        fr(219, MY + 172, 9, 6, ORANGE)
        text(218, MY + 190, str(self.stage), 2, WHITE)

    def draw_lives(self, i):
        y = MY + 126 + i * 30
        self.lcd.fill_rect(216, y, 24, 10, BLACK)
        text(218, y, "-" if self.out[i] else str(self.lives[i]), 2, WHITE)

    def draw_hint(self):
        self.lcd.fill_rect(0, 262, 240, 58, BLACK)
        ctext(270, "A:FIRE  B:PAUSE", 2, GRAY)
        if self.coop:
            ctext(290, "YOU ARE %dP" % (self.net.me + 1), 2, PCOL[self.net.me])
        else:
            ctext(290, "ARROWS:MOVE", 2, DGRAY)

    def draw_pause(self):
        self.lcd.fill_rect(0, 262, 240, 58, BLACK)
        ctext(266, "PAUSED", 3, YELLOW)
        ctext(292, "A:RESUME B:QUIT", 2, WHITE)

    def pace(self, t0):
        dt = ticks_diff(ticks_ms(), t0)
        if dt < FRAME:
            sleep_ms(FRAME - dt)
        return ticks_ms()

    # ------------------------------------------------ stage loop
    def play_stage(self):
        net = self.net if self.coop else None
        if net:
            net.begin_stage(self.stage)
        self.lcd.fill(BG2)
        ctext(140, "STAGE %d" % self.stage, 4, BLACK)
        self.wait(1500)
        if net:
            if not net.barrier(self):
                return "lost"
            self.rs = ((net.gseed ^ (self.stage * 4099)) & 0xFFFF) or 1
        else:
            self.rs = (ticks_ms() & 0xFFFF) or 1
        self.load_stage()
        r = ROSTERS[(self.stage - 1) % len(ROSTERS)]
        self.roster = r + CO_EXTRA if net else r
        self.ei = 0
        self.tanks = []
        self.bullets = []
        self.boom = []
        self.base_ok = True
        self.lost = False
        self.sp_i = 0
        self.spawn_t = 40
        self.fire_rate = max(20, 70 - 8 * (self.stage - 1))
        self.spawn_gap = max(60, 160 - 20 * (self.stage - 1)) - (20 if net else 0)
        self.pl = [None, None]
        gc.collect()
        self.lcd.fill(BLACK)
        self.draw_top()
        self.draw_map()
        self.draw_panel()
        self.draw_hint()
        for i in range(self.np):
            if self.out[i]:
                self.resp[i] = -1
            else:
                self.new_player(i)
        self.snd.play(FX_START, 3)
        paused = False
        end_t = 0
        res = None
        f = 0
        t0 = ticks_ms()
        while True:
            self.check_kbd()
            my = self.read_input()
            if net:
                ins = net.exchange(f, my, self)
                if ins is None:
                    return "lost"
            else:
                ins = (my,)
            f += 1
            ctl = 0
            for v in ins:
                ctl |= v
            if ctl & IN_QUIT:
                return "quit"
            if paused:
                if ctl & IN_FIRE:
                    paused = False
                    self.draw_hint()
                elif ctl & IN_B:
                    return "quit"
                t0 = self.pace(t0)
                continue
            if (ctl & IN_B) and not end_t:
                paused = True
                self.draw_pause()
                t0 = self.pace(t0)
                continue
            if res == "over":
                self.step_booms()
            else:
                self.update(ins)
            if end_t:
                end_t -= 1
                if end_t == 0:
                    return res
            elif self.lost:
                res = "over"
                end_t = 150
                self.snd.play(FX_OVER, 5)
                mtext(MY + 78, "GAME", 4, RED)
                mtext(MY + 106, "OVER", 4, RED)
            elif self.ei >= len(self.roster) and self.n_enemies() == 0:
                res = "clear"
                end_t = 120
                self.snd.play(FX_CLEAR, 4)
                mtext(MY + 92, "CLEAR!", 4, YELLOW)
            t0 = self.pace(t0)

    def update(self, ins):
        for i in range(self.np):
            self.update_player(i, ins[i])

        for e in self.tanks[:]:
            if e.kind != "p" and e in self.tanks:
                if e.sp:
                    self.spark_step(e)
                else:
                    self.ai(e)

        if self.ei < len(self.roster):
            self.spawn_t -= 1
            if self.spawn_t <= 0 and self.try_spawn():
                self.spawn_t = self.spawn_gap

        dirty = False
        for b in self.bullets:
            if b.on:
                self.step_bullet(b)
            if not b.on:
                dirty = True
        if dirty:
            self.bullets = [b for b in self.bullets if b.on]
        self.step_booms()

    def update_player(self, i, v):
        t = self.pl[i]
        if t is not None:
            if t.sp:
                self.spark_step(t)
                return
            d = (v & 7) - 1
            if d >= 0:
                self.move(t, d)
            if v & IN_FIRE:
                self.fire(t)
            if t.inv:
                t.inv -= 1
                if (t.inv & 7) == 0:
                    t.pal = self.PINV if (t.inv & 8) else self.PPAL[i]
                    self.draw_tank(t)
        elif self.resp[i] > 0:
            self.resp[i] -= 1
        elif self.resp[i] == 0 and self.free(P_SPAWNS[i][0], P_SPAWNS[i][1], None):
            self.new_player(i)

    # ------------------------------------------------ tanks
    def new_player(self, i):
        x, y = P_SPAWNS[i]
        t = Tank("p", x, y, UP, 4, 1, self.PPAL[i])
        t.pi = i
        t.inv = 150
        self.pl[i] = t
        self.resp[i] = -1
        self.tanks.append(t)
        self.draw_tank(t)

    def try_spawn(self):
        if self.n_enemies() >= MAX_ON_FIELD:
            return False
        for k in range(3):
            i = (self.sp_i + k) % 3
            x, y = SPAWNS[i]
            if self.free(x, y, None):
                kind = self.roster[self.ei]
                self.icon(len(self.roster) - self.ei - 1, False)
                self.ei += 1
                spd, hp, pts = KIND[kind]
                pal = self.APAL[hp - 1] if kind == "a" else self.EPAL[kind]
                t = Tank(kind, x, y, DOWN, spd, hp, pal)
                t.pts = pts
                self.tanks.append(t)
                self.sp_i = (i + 1) % 3
                self.draw_tank(t)
                return True
        return False

    def draw_spark(self, t):
        fr = self.lcd.fill_rect
        x = MX + t.x
        y = MY + t.y
        fr(x, y, 16, 16, BLACK)
        if (t.sp >> 2) & 1:
            fr(x + 7, y + 1, 2, 14, WHITE)
            fr(x + 1, y + 7, 14, 2, WHITE)
            fr(x + 5, y + 5, 6, 6, YELLOW)
        else:
            fr(x + 7, y + 4, 2, 8, WHITE)
            fr(x + 4, y + 7, 8, 2, WHITE)

    def spark_step(self, t):
        t.sp -= 1
        if t.sp == 0 or (t.sp & 3) == 0:
            self.draw_tank(t)

    def draw_tank(self, t):
        if t.sp:
            self.draw_spark(t)
            return
        _render(self.rb, self.spr[t.d], t.pal, (t.mc >> 2) & 1)
        self.lcd.blit_buffer(self.rb, MX + t.x, MY + t.y, 16, 16)

    def erase_diff(self, ox, oy, nx, ny):
        # clear the part of the old 16x16 box that the new box does not cover
        fr = self.lcd.fill_rect
        dx = nx - ox
        dy = ny - oy
        if abs(dx) >= 16 or abs(dy) >= 16:
            fr(MX + ox, MY + oy, 16, 16, BLACK)
            return
        if dx > 0:
            fr(MX + ox, MY + oy, dx, 16, BLACK)
        elif dx < 0:
            fr(MX + nx + 16, MY + oy, -dx, 16, BLACK)
        if dy > 0:
            fr(MX + ox, MY + oy, 16, dy, BLACK)
        elif dy < 0:
            fr(MX + ox, MY + ny + 16, 16, -dy, BLACK)

    def free(self, x, y, me):
        if x < 0 or y < 0 or x > 192 or y > 192:
            return False
        M = self.M
        r = y >> 3
        r1 = (y + 15) >> 3
        c0 = x >> 3
        c1 = (x + 15) >> 3
        while r <= r1:
            i = r * N
            c = c0
            while c <= c1:
                if M[i + c]:
                    return False
                c += 1
            r += 1
        for o in self.tanks:
            if o is not me and abs(o.x - x) < 16 and abs(o.y - y) < 16:
                # tanks that already overlap may separate
                if me is None or not (abs(o.x - me.x) < 16 and abs(o.y - me.y) < 16):
                    return False
        return True

    def move(self, t, d):
        if d != t.d:
            if (d & 1) != (t.d & 1):        # 90 degree turn: snap to the 8 px grid
                if d & 1:
                    nx, ny = t.x, (t.y + 4) & ~7
                else:
                    nx, ny = (t.x + 4) & ~7, t.y
                if (nx != t.x or ny != t.y) and self.free(nx, ny, t):
                    self.erase_diff(t.x, t.y, nx, ny)
                    t.x = nx
                    t.y = ny
            t.d = d
            self.draw_tank(t)
        t.acc += t.spd
        ox = t.x
        oy = t.y
        ok = True
        while t.acc >= 4:
            t.acc -= 4
            nx = t.x + DX[d]
            ny = t.y + DY[d]
            if self.free(nx, ny, t):
                t.x = nx
                t.y = ny
                t.mc += 1
            else:
                t.acc = 0
                ok = False
                break
        if t.x != ox or t.y != oy:
            self.erase_diff(ox, oy, t.x, t.y)
            self.draw_tank(t)
        return ok

    def ai(self, e):
        e.turn -= 1
        d = e.d
        if e.turn <= 0:
            e.turn = 40 + self.rnd(100)
            d = self.pick_dir(e)
        if not self.move(e, d):
            if e.b is None and self.rnd(3) == 0:
                self.fire(e)            # shoot the wall in the way
            if e.turn > 12:
                e.turn = 4 + self.rnd(12)
        if e.b is None:
            aim = False
            for p in self.pl:
                if p is not None and not p.sp and (
                        (abs(e.x - p.x) < 6 and ((e.d == DOWN and p.y > e.y) or (e.d == UP and p.y < e.y))) or
                        (abs(e.y - p.y) < 6 and ((e.d == RIGHT and p.x > e.x) or (e.d == LEFT and p.x < e.x)))):
                    aim = True
                    break
            if aim:
                if self.rnd(8) == 0:
                    self.fire(e)        # a player is in the line of fire
            elif self.rnd(self.fire_rate) == 0:
                self.fire(e)

    def pick_dir(self, e):
        r = self.rnd(8)
        if r < 3:
            tx, ty = BASE_XY
        elif r < 5:
            p = self.pl[self.rnd(self.np)]
            if p is None:
                return self.rnd(4)
            tx, ty = p.x, p.y
        else:
            return self.rnd(4)
        dx = tx - e.x
        dy = ty - e.y
        if abs(dx) >= 8 and (abs(dy) < 8 or self.rnd(2)):
            return RIGHT if dx > 0 else LEFT
        return DOWN if dy > 0 else UP

    def hit_tank(self, t):
        if t.kind == "p":
            if t.inv:
                return
            i = t.pi
            self.tanks.remove(t)
            self.pl[i] = None
            self.add_boom(t.x, t.y, 1)
            self.snd.play(FX_DIE, 4)
            if self.lives[i] > 0:
                self.lives[i] -= 1
                self.resp[i] = 60
            else:
                self.out[i] = True
                self.resp[i] = -1
                done = True
                for k in range(self.np):
                    if not self.out[k]:
                        done = False
                if done:
                    self.lost = True
            self.draw_lives(i)
            return
        t.hp -= 1
        if t.hp > 0:
            t.pal = self.APAL[t.hp - 1]
            self.draw_tank(t)
            self.snd.play(FX_HIT, 2)
            return
        self.tanks.remove(t)
        self.add_boom(t.x, t.y, 1)
        self.snd.play(FX_BOOM, 2)
        self.score += t.pts
        self.draw_score()

    # ------------------------------------------------ bullets
    def fire(self, t):
        if t.b is not None:
            return
        d = t.d
        if d == UP:
            x, y = t.x + 6, t.y - 4
        elif d == DOWN:
            x, y = t.x + 6, t.y + 16
        elif d == LEFT:
            x, y = t.x - 4, t.y + 6
        else:
            x, y = t.x + 16, t.y + 6
        b = Bullet(x, y, d, t, 4 if t.kind == "p" else 3)
        t.b = b
        self.bullets.append(b)
        if t.kind == "p":
            self.snd.play(FX_FIRE, 1)
        if not self.hit_check(b):
            self.lcd.fill_rect(MX + x, MY + y, 4, 4, WHITE)

    def step_bullet(self, b):
        self.erase_bullet(b)
        b.x += DX[b.d] * b.spd
        b.y += DY[b.d] * b.spd
        if not self.hit_check(b):
            self.lcd.fill_rect(MX + b.x, MY + b.y, 4, 4, WHITE)

    def erase_bullet(self, b):
        x = b.x
        y = b.y
        M = self.M
        c0 = x >> 3
        c1 = (x + 3) >> 3
        r0 = y >> 3
        r1 = (y + 3) >> 3
        if M[r0 * N + c0] or M[r0 * N + c1] or M[r1 * N + c0] or M[r1 * N + c1]:
            self.redraw_area(x, y, 4, 4)          # bullet was over water
            return
        self.lcd.fill_rect(MX + x, MY + y, 4, 4, BLACK)
        for t in self.tanks:
            if x + 4 > t.x and x < t.x + 16 and y + 4 > t.y and y < t.y + 16:
                self.draw_tank(t)

    def end_bullet(self, b, sx, sy):
        b.on = False
        if b.own.b is b:
            b.own.b = None
        if sx >= 0:
            self.add_boom(sx - 2, sy - 2, 0)

    def hit_check(self, b):
        """Returns True if the bullet was used up (and must not be drawn)."""
        x = b.x
        y = b.y
        if x < 0 or y < 0 or x > 204 or y > 204:
            self.end_bullet(b, max(0, min(x, 204)), max(0, min(y, 204)))
            return True
        if self.bullet_map(b):
            self.end_bullet(b, x, y)
            return True
        mine = b.own.kind == "p"
        for t in self.tanks:
            if t is not b.own and not t.sp and x + 4 > t.x and x < t.x + 16 \
                    and y + 4 > t.y and y < t.y + 16:
                self.end_bullet(b, -1, 0)
                if mine != (t.kind == "p"):     # no friendly fire either way
                    self.hit_tank(t)
                return True
        for o in self.bullets:
            if o is not b and o.on and abs(o.x - x) < 6 and abs(o.y - y) < 6 \
                    and (o.own.kind == "p") != mine:
                self.erase_bullet(o)
                self.end_bullet(o, -1, 0)
                self.end_bullet(b, -1, 0)
                return True
        return False

    def bullet_map(self, b):
        # the bullet covers the two 8 px cells either side of its centre line
        M = self.M
        x = b.x
        y = b.y
        d = b.d
        if d == UP or d == DOWN:
            r = y >> 3 if d == UP else (y + 3) >> 3
            ra, ca, rb, cb = r, (x - 2) >> 3, r, (x + 5) >> 3
        else:
            c = x >> 3 if d == LEFT else (x + 3) >> 3
            ra, ca, rb, cb = (y - 2) >> 3, c, (y + 5) >> 3, c
        hit = 0
        for r, c in ((ra, ca), (rb, cb)):
            if 0 <= r < N and 0 <= c < N:
                i = r * N + c
                t = M[i]
                if t == BRICK:
                    M[i] = EMPTY
                    self.lcd.fill_rect(MX + c * 8, MY + r * 8, 8, 8, BLACK)
                    hit |= 1
                elif t == STEEL:
                    hit |= 2
                elif t == BASE:
                    hit |= 4
        if hit & 4:
            self.base_hit()
        elif hit and b.own.kind == "p":
            self.snd.play(FX_STEEL if hit & 2 else FX_BRICK, 1)
        return hit != 0

    def base_hit(self):
        if not self.base_ok:
            return
        self.base_ok = False
        self.draw_base()
        self.add_boom(BASE_XY[0], BASE_XY[1], 1)
        self.snd.play(FX_DIE, 5)
        self.lost = True

    # ------------------------------------------------ explosions
    def add_boom(self, x, y, big):
        if not big:
            x = max(0, min(x, 200))
            y = max(0, min(y, 200))
        self.boom.append([x, y, big, 0])
        self.draw_boom(x, y, big, 0)

    def draw_boom(self, x, y, big, ph):
        fr = self.lcd.fill_rect
        X = MX + x
        Y = MY + y
        if big:
            if ph == 0:
                fr(X + 2, Y + 2, 12, 12, ORANGE)
                fr(X + 5, Y + 5, 6, 6, YELLOW)
            else:
                fr(X, Y, 16, 16, RED)
                fr(X + 3, Y + 3, 10, 10, ORANGE)
                fr(X + 6, Y + 6, 4, 4, WHITE)
        else:
            fr(X, Y, 8, 8, ORANGE)
            fr(X + 2, Y + 2, 4, 4, YELLOW)

    def step_booms(self):
        if not self.boom:
            return
        for e in self.boom:
            e[3] += 1
            if e[2]:
                if e[3] == 6:
                    self.draw_boom(e[0], e[1], 1, 1)
                elif e[3] == 14:
                    self.redraw_area(e[0], e[1], 16, 16)
            elif e[3] == 4:
                self.redraw_area(e[0], e[1], 8, 8)
        self.boom = [e for e in self.boom if e[3] < (14 if e[2] else 4)]

    def redraw_area(self, x, y, w, h):
        # repaint map cells under an area, then any tank touching them
        M = self.M
        lcd = self.lcd
        c0 = max(0, x >> 3)
        c1 = min(N - 1, (x + w - 1) >> 3)
        r0 = max(0, y >> 3)
        r1 = min(N - 1, (y + h - 1) >> 3)
        base = False
        for r in range(r0, r1 + 1):
            for c in range(c0, c1 + 1):
                t = M[r * N + c]
                if t == BASE:
                    base = True
                elif t:
                    lcd.blit_buffer(self.tiles[t], MX + c * 8, MY + r * 8, 8, 8)
                else:
                    lcd.fill_rect(MX + c * 8, MY + r * 8, 8, 8, BLACK)
        if base:
            self.draw_base()
        X0 = c0 * 8
        X1 = (c1 + 1) * 8
        Y0 = r0 * 8
        Y1 = (r1 + 1) * 8
        for t in self.tanks:
            if t.x < X1 and t.x + 16 > X0 and t.y < Y1 and t.y + 16 > Y0:
                self.draw_tank(t)


# ---------------------------------------------------------------- entry
def run():
    global LCD
    gc.collect()
    cfg = load_cfg()
    gc.collect()
    snd = Sound(SOUND_ON and cfg["sound"])  # I2S early: its buffer needs a contiguous block
    gc.collect()
    import st7789py as st7789
    spi = SPI(1, baudrate=36_000_000, polarity=0, phase=0)
    lcd = st7789.ST7789(spi, 240, 320,
                        reset=Pin("PC5", Pin.OUT), dc=Pin("PB0", Pin.OUT),
                        cs=Pin("PC4", Pin.OUT), backlight=Pin("PB1", Pin.OUT),
                        rotation=0)
    lcd.inversion_mode(False)
    LCD = lcd
    try:
        g = Game(lcd, snd, cfg)
        gc.collect()
        print("tank: free RAM", gc.mem_free())
        g.main()
    finally:
        snd.deinit()
        lcd.fill(BLACK)


if __name__ == "__main__":
    run()
