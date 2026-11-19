"""
BUBBLE BLAST - bubble shooter for WIZnet W55MH32L-EVB + ST7789 240x320
(MicroPython 1.27).  1 PLAYER with levels, or 2P VERSUS over Ethernet:
popping 4+ or dropping bubbles sends garbage bubbles to the rival's field.

Build on a PC (viper code needs the arch flag):
    mpy-cross -march=armv7m -O3 bubble.py

Controls: LEFT/RIGHT aim, A shoot, UP swap current/next bubble,
B pause (1P) / B twice quit (VS), A+B quit to the title.
SETUP works like the Tank/Block Fall one and saves bubble_cfg.json (first run
copies tank_cfg.json or blockfall_cfg.json).
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
FRAME = 20                # ms per frame
D, R = 20, 10             # bubble box / radius
ROWH = 17                 # row pitch (hex packing)
COLS, ROWS = 8, 13        # odd rows hold 7
DEAD = 12                 # a bubble in this row = lose
FX, FY = 6, 28            # field origin
FW = COLS * D             # 160
LX, LY = FX + FW // 2, 300   # launcher
DLY = FY + DEAD * ROWH    # death line y
PX = 174                  # side panel x
BURST, FALL = 7, 8        # animation states in the grid
HIFILE = "bubble_hi.txt"
CFGFILE = "bubble_cfg.json"
PORT = 5007
NB_EVEN = ((0, -1), (0, 1), (-1, -1), (-1, 0), (1, -1), (1, 0))
NB_ODD = ((0, -1), (0, 1), (-1, 0), (-1, 1), (1, 0), (1, 1))
RGB = ((230, 40, 40), (240, 200, 30), (40, 190, 60),
       (50, 110, 240), (170, 60, 220), (40, 200, 210))


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
RED = c565(230, 30, 30)
CYAN = c565(60, 200, 230)
PCOL = (YELLOW, GREEN)
BODY = [BLACK] + [c565(r, g, b) for r, g, b in RGB] + [WHITE, GRAY]

# ---------------------------------------------------------------- font
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


def ftext(y, s, sc, col):              # centred on the field
    text(FX + FW // 2 - twidth(s, sc) // 2, y, s, sc, col)


# ---------------------------------------------------------------- graphics
@micropython.viper
def _fill(dst: ptr16, n: int, v: int):
    i = 0
    while i < n:
        dst[i] = v
        i += 1


@micropython.viper
def _blend(dst: ptr16, dw: int, dh: int, spr: ptr8, pal: ptr16, ox: int, oy: int):
    # draw a 20x20 palette sprite (index 0 = transparent) into a dw x dh buffer
    y0 = 0
    if oy < 0:
        y0 = 0 - oy
    y1 = 20
    if oy + 20 > dh:
        y1 = dh - oy
    x0 = 0
    if ox < 0:
        x0 = 0 - ox
    x1 = 20
    if ox + 20 > dw:
        x1 = dw - ox
    y = y0
    while y < y1:
        si = y * 20
        di = (oy + y) * dw + ox
        x = x0
        while x < x1:
            c = spr[si + x]
            if c:
                dst[di + x] = pal[c]
            x += 1
        y += 1


def mk_bubble():
    # 0 transparent, 1 rim, 2 body, 3 light side, 4 highlight
    s = bytearray(400)
    for y in range(20):
        for x in range(20):
            dx = 2 * x - 19
            dy = 2 * y - 19
            d2 = dx * dx + dy * dy
            if d2 >= 400:
                v = 0
            elif d2 >= 300:
                v = 1
            else:
                hx = dx + 8
                hy = dy + 8
                if hx * hx + hy * hy < 40:
                    v = 4
                elif dx + dy < -10:
                    v = 3
                else:
                    v = 2
            s[y * 20 + x] = v
    return s


def mk_pals():
    pals = [None]
    for r, g, b in RGB:
        pals.append(array("H", [0, sw(c565(r * 2 // 5, g * 2 // 5, b * 2 // 5)),
                                sw(c565(r, g, b)),
                                sw(c565((r + 255) // 2, (g + 255) // 2, (b + 255) // 2)),
                                sw(WHITE)]))
    pals.append(array("H", [0, sw(WHITE), sw(c565(255, 240, 150)), sw(WHITE), sw(WHITE)]))
    pals.append(array("H", [0, sw(DGRAY), sw(GRAY), sw(c565(180, 180, 180)), sw(WHITE)]))
    return pals


# ---------------------------------------------------------------- sound
TONES = (196, 262, 330, 392, 523, 659, 784, 1047, 1568)
NZL, NZS = 9, 10
S = -1
FX_SHOOT = ((5, 1), (6, 1))
FX_STICK = ((2, 2),)
FX_POP = ((5, 2), (6, 2), (7, 3))
FX_DROP = ((6, 2), (7, 2), (8, 2), (7, 2), (8, 4))
FX_PUSH = ((NZL, 4), (NZS, 6))
FX_GARB = ((NZS, 3), (1, 3), (NZS, 3))
FX_SWAP = ((4, 1),)
FX_CLEAR = ((4, 8), (S, 2), (5, 8), (S, 2), (6, 8), (S, 2), (8, 24))
FX_WIN = ((6, 8), (S, 2), (7, 8), (S, 2), (8, 20))
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
        for ib in (2048, 4096):
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
        for f in TONES:
            b = array("h", bytes(256))
            for i in range(128):
                b[i] = -1800 if ((i * f * 2) // 16000) & 1 else 1800
            bufs.append(b)
        for amp, hold in ((5000, 2), (2000, 3)):
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
            self.kbd = True
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

    def down(self):
        return self.p.value() == 0

    def pressed(self):
        d = self.p.value() == 0
        if d != self.st:
            now = ticks_ms()
            if ticks_diff(now, self.t) > 25:
                self.st = d
                self.t = now
                return d
        return False

    def rep(self, ms):
        now = ticks_ms()
        if self.pressed():
            self.r = now + 350
            return True
        if self.st and self.p.value() == 0 and ticks_diff(now, self.r) >= 0:
            self.r = now + ms
            return True
        return False


# ---------------------------------------------------------------- settings
def ipstr(a):
    return ".".join([str(x) for x in a])


def board_id():
    try:
        uid = machine.unique_id()
        h = [0, 0, 0]
        for i in range(len(uid)):
            h[i % 3] = (h[i % 3] * 31 + uid[i]) & 0xFF
        return h
    except Exception:
        pass
    try:
        import os
        return list(os.urandom(3))
    except Exception:
        pass
    try:
        from time import ticks_us
        random.seed(ticks_us())
    except Exception:
        pass
    return [random.getrandbits(8) for _ in range(3)]


def default_cfg():
    return {"mac": [0x00, 0x08, 0xDC] + board_id(), "dhcp": True,
            "ip": [192, 168, 1, 20], "mask": [255, 255, 255, 0],
            "gw": [192, 168, 1, 1], "sound": True,
            "peer_auto": True, "peer": [192, 168, 1, 21]}


def save_cfg(cfg):
    import json
    with open(CFGFILE, "w") as f:
        json.dump(cfg, f)


def load_cfg():
    cfg = default_cfg()
    import json
    found = False
    for fn in (CFGFILE, "tank_cfg.json", "blockfall_cfg.json"):
        try:
            with open(fn) as f:
                d = json.load(f)
        except Exception:
            continue
        for k, v in cfg.items():
            x = d.get(k)
            if isinstance(v, list):
                if isinstance(x, list) and len(x) == len(v):
                    cfg[k] = [int(b) & 0xFF for b in x]
            elif isinstance(x, bool):
                cfg[k] = x
        print("cfg: loaded", fn)
        found = fn == CFGFILE
        break
    if not found:
        try:
            save_cfg(cfg)
        except Exception:
            pass
    return cfg


# ---------------------------------------------------------------- network
_NIC = None
_PWN = None


def net_up(cfg):
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
                nic.ifconfig(("192.168.77.%d" % (10 + cfg["mac"][5] % 200),
                              "255.255.255.0", "192.168.77.1", "8.8.8.8"))
    c = _NIC.ifconfig()
    return c[0], c[1], bytes(cfg["mac"][3:6])


def bcast_addr(ip, mask):
    a = ip.split(".")
    m = mask.split(".")
    return ".".join([str((int(a[i]) & int(m[i])) | (255 ^ int(m[i]))) for i in range(4)])


class Net:
    """Versus link. Each board plays its own field; packets carry
    [kind, session, round, ...]:
      H id3 / J id3 seed2   find the other board (like the tank game)
      S          ready for this round
      F par pend grid52     my field (latest wins, also the heartbeat)
      A id n     n garbage bubbles, numbered; K next  = cumulative ack
      L          I lost (repeated until Y);  Q  I quit
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
        self.seed = self.pseed = self.gseed = 1
        self.jpkt = b"J"
        self.linked = self.hunting = self.active = False
        self.me = 0
        self.sess = 0
        self.round = -1
        self.fb = bytearray(57)
        self.og = bytearray(52)
        self.begin_round(0)
        self.round = -1

    def _send(self, pkt, ip=None):
        try:
            self.s.sendto(pkt, (ip or self.peer, PORT))
        except Exception:
            pass

    def _hdr(self, k):
        return bytes((k, self.sess, self.round))

    def _rx(self):
        while True:
            try:
                d, a = self.s.recvfrom(64)
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
                continue
            if k == 74:                                     # J
                if len(d) >= 6:
                    self.pid = bytes(d[1:4])
                    self.pseed = d[4] | (d[5] << 8)
                    self.linked = True
                continue
            if not self.active or len(d) < 3 or d[1] != self.sess:
                continue
            if k == 81:                                     # Q (any round)
                self.opp_quit = True
                continue
            if d[2] != self.round:
                continue
            self.last_rx = ticks_ms()
            if k == 83:                                     # S
                self.seen = True
            elif k == 70 and len(d) >= 57:                  # F
                self.seen = True
                self.opar = d[3]
                self.opend = d[4]
                self.og[:] = d[5:57]
                self.odirty = True
            elif k == 65 and len(d) >= 5:                   # A
                if d[3] == (self.rx_next & 255):
                    self.incoming += d[4]
                    self.rx_next += 1
                self._send(self._hdr(75) + bytes((self.rx_next & 255,)))
            elif k == 75 and len(d) >= 4:                   # K
                nxt = d[3]
                while self.tx and 0 < ((nxt - self.tx[0][0]) & 255) <= 128:
                    self.tx.pop(0)
            elif k == 76:                                   # L
                self.opp_lost = True
                self._send(self._hdr(89))
            elif k == 89:                                   # Y
                self.got_y = True

    def connect(self, g):
        self.peer = self.fixed
        self.pid = b""
        self.linked = False
        self.active = False
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

    def begin_round(self, rnd):
        self.round = rnd & 255
        self.seen = False
        self.tx = []            # unacked attacks: [id, n]
        self.tx_id = 0
        self.rx_next = 0
        self.incoming = 0
        self.opp_lost = False
        self.opp_quit = False
        self.lost_tx = False
        self.got_y = False
        self.opar = 0
        self.opend = 0
        self.odirty = False
        for i in range(52):
            self.og[i] = 0
        self.last_rx = ticks_ms()
        self.tr = 0
        self.tl = 0

    def barrier(self, g):
        """Wait until the rival is in the same round. False = B / timeout / quit."""
        t0 = ticks_ms()
        t = t0 - 1000
        while not self.seen:
            g.check_kbd()
            if g.bb.pressed() or self.opp_quit:
                return False
            now = ticks_ms()
            if ticks_diff(now, t) >= 100:
                t = now
                self._send(self._hdr(83))
            self._rx()
            if ticks_diff(now, t0) > 60000:
                return False
            sleep_ms(10)
        for _ in range(3):
            self._send(self._hdr(83))
        self.last_rx = ticks_ms()
        return True

    def service(self):
        if not self.active:
            return
        self._rx()
        now = ticks_ms()
        if self.tx and ticks_diff(now, self.tr) > 150:
            self.tr = now
            for e in self.tx[:4]:
                self._send(self._hdr(65) + bytes((e[0] & 255, e[1])))
        if self.lost_tx and not self.got_y and ticks_diff(now, self.tl) > 100:
            self.tl = now
            self._send(self._hdr(76))

    def attack(self, n):
        while n > 0:
            k = min(n, 30)
            self.tx.append([self.tx_id, k])
            self._send(self._hdr(65) + bytes((self.tx_id & 255, k)))
            self.tx_id += 1
            n -= k
        self.tr = ticks_ms()

    def send_field(self, f, pend):
        b = self.fb
        b[0] = 70
        b[1] = self.sess
        b[2] = self.round
        b[3] = f.par
        b[4] = min(pend, 255)
        g = f.g
        for k in range(52):
            lo = g[2 * k]
            hi = g[2 * k + 1]
            b[5 + k] = (lo if lo < BURST else 0) | ((hi if hi < BURST else 0) << 4)
        self._send(b)

    def lose(self):
        self.lost_tx = True
        self.tl = ticks_ms()
        self._send(self._hdr(76))

    def quit(self):
        if self.active:
            for _ in range(3):
                self._send(self._hdr(81))
                sleep_ms(20)
        self.active = False


# ---------------------------------------------------------------- field
class Field:
    def __init__(self):
        self.g = bytearray(ROWS * COLS)
        self.mark = bytearray(ROWS * COLS)
        self.par = 0

    def ncols(self, r):
        return 7 if (r + self.par) & 1 else 8

    def cx(self, r, c):
        return FX + R + c * D + (R if (r + self.par) & 1 else 0)

    def cy(self, r):
        return FY + R + r * ROWH

    def clear(self):
        for i in range(ROWS * COLS):
            self.g[i] = 0
        self.par = 0

    def fill_rows(self, n, ncol, rnd):
        g = self.g
        for r in range(n):
            for c in range(self.ncols(r)):
                v = 1 + rnd(ncol)
                if c > 0 and rnd(3) == 0:
                    v = g[r * COLS + c - 1]         # small same-colour clumps
                g[r * COLS + c] = v

    def push(self, pick):
        g = self.g
        for i in range(ROWS * COLS - 1, COLS - 1, -1):
            g[i] = g[i - COLS]
        self.par ^= 1
        n = self.ncols(0)
        for c in range(COLS):
            g[c] = pick() if c < n else 0

    def nbs(self, i):
        r = i // COLS
        c = i % COLS
        out = []
        for dr, dc in (NB_ODD if (r + self.par) & 1 else NB_EVEN):
            rr = r + dr
            cc = c + dc
            if 0 <= rr < ROWS and 0 <= cc < self.ncols(rr):
                out.append(rr * COLS + cc)
        return out

    def group(self, i):
        g = self.g
        m = self.mark
        for k in range(ROWS * COLS):
            m[k] = 0
        col = g[i]
        st = [i]
        m[i] = 1
        out = []
        while st:
            j = st.pop()
            out.append(j)
            for k in self.nbs(j):
                if not m[k] and g[k] == col:
                    m[k] = 1
                    st.append(k)
        return out

    def floating(self):
        g = self.g
        m = self.mark
        for k in range(ROWS * COLS):
            m[k] = 0
        st = []
        for c in range(COLS):
            if 1 <= g[c] <= 6:
                m[c] = 1
                st.append(c)
        while st:
            j = st.pop()
            for k in self.nbs(j):
                if not m[k] and 1 <= g[k] <= 6:
                    m[k] = 1
                    st.append(k)
        return [k for k in range(ROWS * COLS) if 1 <= g[k] <= 6 and not m[k]]

    def present(self):
        s = 0
        for v in self.g:
            if 1 <= v <= 6:
                s |= 1 << v
        return [k for k in range(1, 7) if s & (1 << k)]

    def dead(self):
        for c in range(COLS):
            if self.g[DEAD * COLS + c]:
                return True
        return False

    def empty(self):
        for v in self.g:
            if v:
                return False
        return True

    def hits(self, x, y):
        if y - R <= FY:
            return True
        g = self.g
        r0 = (y - FY - R) // ROWH
        for r in (r0 - 1, r0, r0 + 1):
            if 0 <= r < ROWS:
                dy = FY + R + r * ROWH - y
                off = R if (r + self.par) & 1 else 0
                c0 = (x - FX - off) // D
                for c in (c0 - 1, c0, c0 + 1):
                    if 0 <= c < COLS:
                        v = g[r * COLS + c]
                        if v and v < BURST:
                            dx = FX + R + off + c * D - x
                            if dx * dx + dy * dy < 256:
                                return True
        return False

    def snap(self, x, y):
        """Nearest free cell that touches the ceiling or another bubble."""
        g = self.g
        rr = (y - FY - R + ROWH // 2) // ROWH
        best = -1
        bd = 1 << 20
        loose = -1
        ld = 1 << 20
        for r in (rr - 1, rr, rr + 1):
            if 0 <= r < ROWS:
                off = R if (r + self.par) & 1 else 0
                c0 = (x - FX - off) // D
                for c in (c0 - 1, c0, c0 + 1):
                    if 0 <= c < self.ncols(r) and g[r * COLS + c] == 0:
                        dx = FX + R + off + c * D - x
                        dy = FY + R + r * ROWH - y
                        d = dx * dx + dy * dy
                        i = r * COLS + c
                        ok = r == 0
                        if not ok:
                            for k in self.nbs(i):
                                if 1 <= g[k] <= 6:
                                    ok = True
                                    break
                        if ok and d < bd:
                            bd = d
                            best = i
                        if d < ld:
                            ld = d
                            loose = i
        return best if best >= 0 else loose


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


# ---------------------------------------------------------------- setup UI
MENU = ("1 PLAYER", "2P VERSUS", "SETUP")
ITEMS = ("MAC", "MODE", "IP", "MASK", "GW", "PEER", "P.IP", "SOUND", "SAVE", "BACK")
LISTS = {"MAC": "mac", "IP": "ip", "MASK": "mask", "GW": "gw", "P.IP": "peer"}


def item_fields(cfg, key):
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


# ---------------------------------------------------------------- game
class Game:
    def __init__(self, lcd, snd, cfg):
        self.lcd = lcd
        self.snd = snd
        self.cfg = cfg
        self.f = Field()
        self.spr = mk_bubble()
        self.pals = mk_pals()
        self.rb = bytearray(32 * 32 * 2)
        self.cos = array("h", [0] * 181)
        self.sin = array("h", [0] * 181)
        import math
        for a in range(181):
            t = math.radians(a)
            self.cos[a] = int(math.cos(t) * 256)
            self.sin[a] = int(math.sin(t) * 256)
        self.bl = Btn("PC6")
        self.br = Btn("PG8")
        self.bu = Btn("PA8")
        self.bd = Btn("PC7")
        self.ba = Btn("PF13")
        self.bb = Btn("PF11")
        self.hi = load_hi()
        self.net = None
        self.vs = False
        self.sel = 0
        self.rs = 1
        self.score = 0
        self.level = 1
        self.ncol = 4
        self.pushn = 8
        self.shots = 0
        self.cur = 1
        self.nxt = 1
        self.ang = 90
        self.adots = []
        self.ball = None
        self.loaded = False
        self.anim = None
        self.atk = 0
        self.pending = 0
        self.res = None
        self.fdirty = False
        self.wins = [0, 0]
        self.rv = bytearray(ROWS * COLS)
        self.rpar = -1

    # ------------------------------------------------ helpers
    def rnd(self, n):
        x = self.rs
        x ^= (x << 7) & 0xFFFF
        x ^= x >> 9
        x ^= (x << 8) & 0xFFFF
        self.rs = x
        return x % n

    def pick(self):
        p = self.f.present()
        if p:
            return p[self.rnd(len(p))]
        return 1 + self.rnd(self.ncol)

    def check_kbd(self):
        if self.snd.kbd:
            self.snd.kbd = False
            raise KeyboardInterrupt

    def wait(self, ms):
        t = ticks_ms()
        while ticks_diff(ticks_ms(), t) < ms:
            self.check_kbd()
            if self.vs and self.net:
                self.net.service()
            sleep_ms(10)

    def wait_ab(self):
        while True:
            self.check_kbd()
            if self.vs and self.net:
                self.net.service()
            if self.ba.pressed():
                return 1
            if self.bb.pressed():
                return 2
            sleep_ms(10)

    def pace(self, t0):
        dt = ticks_diff(ticks_ms(), t0)
        if dt < FRAME:
            sleep_ms(FRAME - dt)
        return ticks_ms()

    # ------------------------------------------------ drawing
    def bubble_at(self, x, y, v):          # lone bubble on black (panel, title)
        b = self.rb
        _fill(b, 400, 0)
        if v:
            _blend(b, 20, 20, self.spr, self.pals[v], 0, 0)
        self.lcd.blit_buffer(memoryview(b)[:800], x, y, 20, 20)

    def region(self, x, y, w, h):
        """Recompose a field rectangle (<= 32x32) from the grid and blit it."""
        if x < FX:
            w -= FX - x
            x = FX
        if x + w > FX + FW:
            w = FX + FW - x
        if y < FY:
            h -= FY - y
            y = FY
        if y + h > 320:
            h = 320 - y
        if w <= 0 or h <= 0:
            return
        b = self.rb
        _fill(b, w * h, 0)
        f = self.f
        g = f.g
        spr = self.spr
        pals = self.pals
        r = (y - FY - D) // ROWH + 1
        if r < 0:
            r = 0
        r1 = (y + h - 1 - FY) // ROWH
        if r1 > ROWS - 1:
            r1 = ROWS - 1
        while r <= r1:
            off = R if (r + f.par) & 1 else 0
            c = (x - FX - off - D) // D + 1
            if c < 0:
                c = 0
            c1 = (x + w - 1 - FX - off) // D
            if c1 > COLS - 1:
                c1 = COLS - 1
            base = r * COLS
            by = FY + r * ROWH - y
            while c <= c1:
                v = g[base + c]
                if v:
                    _blend(b, w, h, spr, pals[v], FX + off + c * D - x, by)
                c += 1
            r += 1
        if self.loaded:
            ox = LX - R - x
            oy = LY - R - y
            if -D < ox < w and -D < oy < h:
                _blend(b, w, h, spr, pals[self.cur], ox, oy)
        bl = self.ball
        if bl:
            ox = (bl[0] >> 8) - R - x
            oy = (bl[1] >> 8) - R - y
            if -D < ox < w and -D < oy < h:
                _blend(b, w, h, spr, pals[bl[4]], ox, oy)
        if y <= DLY < y + h:                    # dashed death line
            o = (DLY - y) * w * 2
            for i in range(w):
                if ((x + i) >> 2) & 1:
                    b[o + 2 * i] = GRAY >> 8
                    b[o + 2 * i + 1] = GRAY & 255
        self.lcd.blit_buffer(memoryview(b)[:w * h * 2], x, y, w, h)

    def draw_cell(self, i):
        f = self.f
        r = i // COLS
        c = i % COLS
        self.region(f.cx(r, c) - R, f.cy(r) - R, D, D)

    def draw_field(self):
        for ty in range(FY, 320, 32):
            for tx in range(FX, FX + FW, 32):
                self.region(tx, ty, 32, 32)
        self.lcd.fill_rect(LX - 16, 313, 32, 3, GRAY)
        self.lcd.fill_rect(LX - 3, 311, 6, 2, GRAY)
        if self.loaded:
            self.arrow(True)

    def draw_frame(self):
        fr = self.lcd.fill_rect
        fr(FX - 2, FY - 2, FW + 4, 2, DGRAY)
        fr(FX - 2, FY, 2, 320 - FY, DGRAY)
        fr(FX + FW, FY, 2, 320 - FY, DGRAY)

    def arrow(self, show):
        fr = self.lcd.fill_rect
        for p in self.adots:
            fr(p[0] - 1, p[1] - 1, 3, 3, BLACK)
        self.adots = []
        if show:
            c = self.cos[self.ang]
            s = self.sin[self.ang]
            for k in range(5):
                d = 17 + k * 7
                x = LX + ((c * d) >> 8)
                y = LY - ((s * d) >> 8)
                fr(x - 1, y - 1, 3, 3, WHITE if k < 4 else YELLOW)
                self.adots.append((x, y))

    def draw_top(self):
        self.lcd.fill_rect(0, 0, 240, FY - 2, BLACK)
        text(4, 6, "SC", 3, YELLOW)
        self.draw_score()
        if self.vs:
            text(150, 6, "VS", 3, RED)
            text(186, 6, "%dP" % (self.net.me + 1), 3, PCOL[self.net.me])
        else:
            text(150, 6, "LV", 3, CYAN)
            text(186, 6, str(self.level), 3, WHITE)

    def draw_score(self):
        self.lcd.fill_rect(32, 6, 96, 15, BLACK)
        text(32, 6, "%06d" % self.score, 3, WHITE)

    def draw_next(self):
        self.bubble_at(PX + 22, 44, self.nxt)

    def draw_panel(self):
        fr = self.lcd.fill_rect
        fr(PX - 2, FY, 240 - PX + 2, 320 - FY, BLACK)
        text(PX + 4, 30, "NEXT", 2, GRAY)
        self.draw_next()
        if self.vs:
            text(PX + 4, 74, "RIVAL", 2, GRAY)
            fr(PX, 92, 57, 1, DGRAY)
            fr(PX, 162, 57, 1, DGRAY)
            fr(PX, 92, 1, 71, DGRAY)
            fr(PX + 56, 92, 1, 71, DGRAY)
            self.rpar = -1
            self.draw_rival()
            text(PX + 4, 170, "IN", 2, GRAY)
            self.draw_pending()
            text(PX + 4, 194, "WINS", 2, GRAY)
            self.draw_wins()
        else:
            text(PX + 4, 74, "DROP", 2, GRAY)
            self.draw_drop()
            text(PX + 4, 120, "HI", 2, GRAY)
            text(PX + 4, 136, "%06d" % self.hi, 2, GRAY)
        self.draw_hint()

    def draw_hint(self, msg=None):
        fr = self.lcd.fill_rect
        fr(PX - 2, 246, 240 - PX + 2, 74, BLACK)
        if msg:
            for i in range(len(msg)):
                text(PX, 250 + i * 18, msg[i], 2, YELLOW if i == 0 else WHITE)
            return
        text(PX, 250, "A:SHOOT", 2, DGRAY)
        text(PX, 268, "UP:SWAP", 2, DGRAY)
        text(PX, 286, "B:QUIT" if self.vs else "B:PAUSE", 2, DGRAY)

    def draw_drop(self):
        self.lcd.fill_rect(PX + 4, 90, 60, 15, BLACK)
        n = self.pushn - self.shots % self.pushn
        text(PX + 4, 90, str(n), 3, RED if n <= 2 else WHITE)

    def draw_pending(self):
        self.lcd.fill_rect(PX + 24, 170, 40, 10, BLACK)
        text(PX + 24, 170, str(self.pending), 2, RED if self.pending else WHITE)

    def draw_wins(self):
        self.lcd.fill_rect(PX + 4, 210, 62, 15, BLACK)
        text(PX + 4, 210, "%d-%d" % (self.wins[0], self.wins[1]), 3, WHITE)

    def draw_rival(self):
        net = self.net
        fr = self.lcd.fill_rect
        og = net.og
        rv = self.rv
        full = net.opar != self.rpar
        if full:
            fr(PX + 1, 93, 55, 69, BLACK)
            fr(PX + 1, 96 + DEAD * 5 - 3, 55, 1, RED)
            self.rpar = net.opar
        for r in range(ROWS):
            off = 3 if (r + net.opar) & 1 else 0
            y = 96 + r * 5 - 2
            for c in range(COLS):
                k = r * COLS + c
                v = (og[k >> 1] >> ((k & 1) * 4)) & 15
                if v > 6:
                    v = 0
                if full or v != rv[k]:
                    rv[k] = v
                    if full and not v:
                        continue
                    fr(PX + 2 + c * 6 + off, y, 5, 4, BODY[v])
        net.odirty = False

    # ------------------------------------------------ screens
    def title(self):
        lcd = self.lcd
        lcd.fill(BLACK)
        ctext(24, "BUBBLE", 7, CYAN)
        ctext(68, "BLAST", 6, WHITE)
        for i, (x, y, v) in enumerate(((76, 112, 1), (96, 112, 2), (116, 112, 3),
                                       (136, 112, 4), (86, 129, 5), (106, 129, 6),
                                       (126, 129, 1), (96, 146, 3), (116, 146, 4))):
            self.bubble_at(x, y, v)
        self.draw_menu()
        ctext(278, "HI %06d" % self.hi, 2, GRAY)
        ctext(300, "B: GAMES MENU", 2, DGRAY)
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

    def draw_menu(self):
        fr = self.lcd.fill_rect
        fr(40, 186, 16, 58, BLACK)
        text(44, 188 + self.sel * 20, ">", 2, YELLOW)
        for i in range(len(MENU)):
            text(64, 188 + i * 20, MENU[i], 2, WHITE if self.sel == i else GRAY)

    def main(self):
        while True:
            s = self.title()
            if s < 0:
                return
            if s == 0:
                self.play_solo()
            elif s == 1:
                if self.link():
                    self.play_vs()
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
        for k, v in self.cfg.items():
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
                return
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
                break
            elif self.bb.pressed():
                vals[:] = backup
                break
            if ch:
                self.draw_item(cfg, sel, sel, j)
            sleep_ms(10)
        if k == "mac":
            vals[0] &= 0xFE
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

    # ------------------------------------------------ versus link
    def link(self):
        lcd = self.lcd
        cfg = self.cfg
        lcd.fill(BLACK)
        ctext(30, "VERSUS", 6, RED)
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
        self.snd.play(FX_LINK, 2)
        lcd.fill_rect(0, 136, 240, 40, BLACK)
        lcd.fill_rect(0, 262, 240, 20, BLACK)
        ctext(150, "LINKED! YOU ARE %dP" % (self.net.me + 1), 2, PCOL[self.net.me])
        sleep_ms(1000)
        return True

    # ------------------------------------------------ board setup
    def start_board(self, rows, ncol, pushn):
        f = self.f
        f.clear()
        self.ncol = ncol
        f.fill_rows(rows, ncol, self.rnd)
        self.pushn = pushn
        self.shots = 0
        self.ball = None
        self.anim = None
        self.res = None
        self.pending = 0
        self.cur = self.pick()
        self.nxt = self.pick()
        self.ang = 90
        self.adots = []
        self.loaded = True
        self.fdirty = True
        gc.collect()
        self.lcd.fill(BLACK)
        self.draw_frame()
        self.draw_top()
        self.draw_panel()
        self.draw_field()

    def play_solo(self):
        self.vs = False
        self.score = 0
        self.level = 1
        self.rs = (ticks_ms() & 0xFFFF) or 1
        while True:
            lv = self.level
            self.lcd.fill(BLACK)
            ctext(130, "LEVEL %d" % lv, 4, CYAN)
            self.wait(1200)
            self.start_board(min(4 + lv // 2, 8), min(3 + (lv + 1) // 2, 6),
                             max(4, 9 - lv // 2))
            r = self.round_loop()
            if r == "clear":
                bonus = 500 * lv
                self.score += bonus
                self.draw_score()
                self.snd.play(FX_CLEAR, 4)
                self.lcd.fill_rect(FX, 100, FW, 64, BLACK)
                ftext(110, "CLEAR!", 4, YELLOW)
                ftext(140, "BONUS %d" % bonus, 2, WHITE)
                self.wait(2000)
                self.level += 1
                continue
            break
        new = self.score > self.hi
        if new:
            self.hi = self.score
            save_hi(self.hi)
        if r == "quit":
            return
        self.snd.play(FX_OVER, 5)
        self.lcd.fill_rect(FX, 92, FW, 104, BLACK)
        ftext(100, "GAME", 4, RED)
        ftext(126, "OVER", 4, RED)
        if new:
            ftext(160, "NEW HI!", 2, YELLOW)
        ftext(180, "PRESS A", 2, WHITE)
        self.wait(500)
        self.wait_ab()

    def play_vs(self):
        net = self.net
        self.vs = True
        self.wins = [0, 0]
        rnd_no = 0
        r = "quit"
        while True:
            net.begin_round(rnd_no)
            if rnd_no:
                self.draw_hint(("WAITING", "B:QUIT"))
            if not net.barrier(self):
                r = "left" if net.opp_quit else "quit"
                break
            self.rs = ((net.gseed ^ (rnd_no * 4099)) & 0xFFFF) or 1
            self.score = 0
            self.start_board(5, 5, 12)
            r = self.round_loop()
            if r in ("quit", "lost", "left"):
                break
            # result screen: keep the link serviced (acks, L/Y) while waiting
            shown = None
            k = 0
            while not k:
                if r == "lose" and net.opp_lost:
                    r = "draw"              # both lost before hearing the other
                if r != shown:
                    self.show_result(r)
                    shown = r
                self.check_kbd()
                net.service()
                if net.opp_quit:
                    k = 3
                elif self.ba.pressed():
                    k = 1
                elif self.bb.pressed():
                    k = 2
                sleep_ms(10)
            if r == "win":
                self.wins[0] += 1
            elif r == "lose":
                self.wins[1] += 1
            if k != 1:
                r = "left" if k == 3 else "quit"
                break
            rnd_no += 1
        if r == "lost" or r == "left":
            self.lcd.fill_rect(FX, 90, FW, 120, BLACK)
            ftext(110, "LINK LOST" if r == "lost" else "RIVAL LEFT", 2, RED)
            ftext(140, "PRESS A", 2, WHITE)
            self.wait_ab()
        net.quit()
        self.vs = False

    def show_result(self, r):
        fr = self.lcd.fill_rect
        fr(FX, 96, FW, 84, BLACK)
        if r == "win":
            ftext(104, "YOU WIN!", 3, YELLOW)
            self.snd.play(FX_WIN, 5)
        elif r == "lose":
            ftext(104, "YOU LOSE", 3, RED)
            self.snd.play(FX_OVER, 5)
        else:
            ftext(104, "DRAW", 3, WHITE)
        w = list(self.wins)
        if r == "win":
            w[0] += 1
        elif r == "lose":
            w[1] += 1
        ftext(132, "WINS %d-%d" % (w[0], w[1]), 2, WHITE)
        ftext(154, "A:NEXT B:QUIT", 2, GRAY)

    # ------------------------------------------------ main play loop
    def round_loop(self):
        net = self.net if self.vs else None
        f = self.f
        t0 = ticks_ms()
        hb = 0
        held = 0
        quit_arm = 0
        while True:
            self.check_kbd()
            now = ticks_ms()
            a = self.ba.pressed()
            b = self.bb.pressed()
            up = self.bu.pressed()
            if self.ba.down() and self.bb.down():
                if net:
                    net.lose()
                return "quit"
            if net:
                net.service()
                if net.opp_quit:
                    return "left"
                if ticks_diff(now, net.last_rx) > 5000:
                    return "lost"
                if net.incoming:
                    self.pending += net.incoming
                    net.incoming = 0
                    self.draw_pending()
                    self.snd.play(FX_GARB, 2)
                if net.odirty:
                    self.draw_rival()
                if net.opp_lost:
                    return "win"
                if self.fdirty or ticks_diff(now, hb) > 300:
                    net.send_field(f, self.pending)
                    hb = now
                    self.fdirty = False
                if quit_arm and ticks_diff(now, quit_arm) > 2000:
                    quit_arm = 0
                    self.draw_hint()
            if b:
                if net:
                    if quit_arm:
                        net.lose()
                        return "quit"
                    quit_arm = now
                    self.draw_hint(("B AGAIN", "TO QUIT"))
                else:
                    self.draw_hint(("PAUSED", "A:GO", "B:END"))
                    k = self.wait_ab()
                    if k == 2:
                        return "quit"
                    self.draw_hint()
                    t0 = ticks_ms()
                    continue

            if self.anim:
                self.step_anim()
            elif self.ball:
                self.step_ball()
            else:
                d = 0
                if self.bl.down():
                    d = 1
                elif self.br.down():
                    d = -1
                if d:
                    held += 1
                    step = 1 if held < 15 else 2
                    na = min(168, max(12, self.ang + d * step))
                    if na != self.ang:
                        self.ang = na
                        self.arrow(True)
                else:
                    held = 0
                if up and self.nxt != self.cur:
                    self.cur, self.nxt = self.nxt, self.cur
                    self.region(LX - R, LY - R, D, D)
                    self.draw_next()
                    self.snd.play(FX_SWAP, 1)
                if a:
                    self.fire()
            if self.res:
                r = self.res
                if net and r == "over":
                    net.send_field(f, self.pending)
                    net.lose()
                    return "lose"
                return r
            t0 = self.pace(t0)

    def fire(self):
        a = self.ang
        self.ball = [LX << 8, LY << 8, self.cos[a] * 4, -self.sin[a] * 4, self.cur]
        self.loaded = False
        self.arrow(False)
        self.snd.play(FX_SHOOT, 1)

    def step_ball(self):
        b = self.ball
        f = self.f
        ox = b[0] >> 8
        oy = b[1] >> 8
        stuck = False
        for _ in range(2):              # 2 sub-steps of 4 px
            b[0] += b[2]
            b[1] += b[3]
            x = b[0] >> 8
            if x - R < FX:
                b[0] = ((FX + R) << 9) - b[0]
                b[2] = -b[2]
            elif x + R > FX + FW:
                b[0] = ((FX + FW - R) << 9) - b[0]
                b[2] = -b[2]
            if b[1] < (FY + R) << 8:
                b[1] = (FY + R) << 8
            if f.hits(b[0] >> 8, b[1] >> 8):
                stuck = True
                break
        nx = b[0] >> 8
        ny = b[1] >> 8
        self.region(min(ox, nx) - R, min(oy, ny) - R,
                    abs(nx - ox) + D, abs(ny - oy) + D)
        if stuck:
            self.land(nx, ny)

    def land(self, x, y):
        f = self.f
        g = f.g
        col = self.ball[4]
        self.ball = None
        self.region(x - R, y - R, D, D)
        i = f.snap(x, y)
        if i < 0:
            self.res = "over"
            return
        g[i] = col
        self.draw_cell(i)
        self.fdirty = True
        if i // COLS >= DEAD:
            self.res = "over"
            return
        grp = f.group(i)
        if len(grp) >= 3:
            for k in grp:
                g[k] = BURST
            fl = f.floating()
            for k in fl:
                g[k] = FALL
            cells = grp + fl
            for k in cells:
                self.draw_cell(k)
            self.anim = [cells, 10]
            self.score += 10 * len(grp) + ((10 << min(len(fl), 10)) if fl else 0)
            self.draw_score()
            self.atk = len(fl) + (len(grp) - 3) // 2
            self.snd.play(FX_DROP if fl else FX_POP, 2)
        else:
            self.snd.play(FX_STICK, 1)
            self.after_shot()

    def step_anim(self):
        cells, t = self.anim
        t -= 1
        g = self.f.g
        if t == 5:
            for k in cells:             # bursts vanish first, drops a bit later
                if g[k] == BURST:
                    g[k] = 0
                    self.draw_cell(k)
        if t > 0:
            self.anim[1] = t
            return
        for k in cells:
            g[k] = 0
        for k in cells:
            self.draw_cell(k)
        self.anim = None
        self.fdirty = True
        if self.vs and self.atk:
            n = self.atk
            c = min(n, self.pending)    # attacks cancel incoming garbage first
            self.pending -= c
            n -= c
            if n:
                self.net.attack(n)
            self.draw_pending()
        self.atk = 0
        self.after_shot()

    def garbage(self, n):
        f = self.f
        for _ in range(n):
            x = FX + R + self.rnd(FW - D + 1)
            y = DLY + 24
            while y - R > FY and not f.hits(x, y):
                y -= 4
            i = f.snap(x, y)
            if i < 0:
                break
            f.g[i] = self.pick()
            self.draw_cell(i)
        self.snd.play(FX_GARB, 3)

    def after_shot(self):
        f = self.f
        self.shots += 1
        self.fdirty = True
        if self.vs and self.pending:
            n = min(self.pending, 14)
            self.pending -= n
            self.garbage(n)
            self.draw_pending()
        if f.dead():
            self.res = "over"
            return
        if self.shots % self.pushn == 0:
            f.push(self.pick)
            self.snd.play(FX_PUSH, 2)
            self.draw_field()
            if f.dead():
                self.res = "over"
                return
        if not self.vs:
            self.draw_drop()
        if f.empty():
            if self.vs:
                f.fill_rows(4, self.ncol, self.rnd)
                self.draw_field()
            else:
                self.res = "clear"
                return
        p = f.present()
        self.cur = self.nxt
        if p and self.cur not in p:
            self.cur = self.pick()
        self.nxt = self.pick()
        self.loaded = True
        self.region(LX - R, LY - R, D, D)
        self.draw_next()
        self.arrow(True)


# ---------------------------------------------------------------- entry
def run():
    global LCD
    gc.collect()
    cfg = load_cfg()
    gc.collect()
    snd = Sound(SOUND_ON and cfg["sound"])
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
        print("bubble: free RAM", gc.mem_free())
        g.main()
    finally:
        snd.deinit()
        lcd.fill(BLACK)


if __name__ == "__main__":
    run()
