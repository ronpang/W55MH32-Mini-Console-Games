# BLOCK FALL (falling-block puzzle) for W55MH32L-EVB + ST7789 (SPI1) - MicroPython, using st7789py (frozen in firmware)

import time, random, gc
from machine import Pin, SPI
from array import array
try:
    from machine import I2S
except ImportError:
    I2S = None
import st7789py as st7789

# ===================== PIN SETUP =====================
SPI_ID    = 1
SPI_BAUD  = 36_000_000
PIN_SCK   = None
PIN_MOSI  = None
PIN_CS    = "PC4"
PIN_DC    = "PB0"
PIN_RST   = "PC5"
PIN_BL    = "PB1"

BTN_LEFT  = "PC6"
BTN_RIGHT = "PG8"
BTN_DOWN  = "PC7"
BTN_UP    = "PA8"   # hard (fast) drop
BTN_A     = "PF13"  # Enter (menu) / Rotate (in game)
BTN_B     = "PF11"    # Back / Exit  <-- fill in
BTN_ACTIVE_LOW = True

LCD_W, LCD_H = 240, 320
ROTATION = 0
INVERT_COLORS = False   # if background shows WHITE, flip this to False (or True)

# ---- I2S audio (same setup as mp3_stable.py) ----
ENABLE_SOUND = True
I2S_ID    = 3
I2S_SCK   = "PB3"   # BCLK
I2S_WS    = "PA15"  # LRCK / WS
I2S_SD    = "PB5"   # DOUT -> amp/DAC DIN
I2S_RATE  = 16000   # standard rate; 8000 also works if your port supports it
I2S_IBUF  = 8192
I2S_STEREO = False  # mono uses half the RAM; set True if your amp needs stereo
VOLUME    = 2500    # 0..32767, keep low for small speakers

# ---- Battle mode over Ethernet (same setup as WIZnet5k_dhcp.py) ----
NET_SPI_ID   = 2
NET_SPI_BAUD = 10_000_000
NET_PIN_CS   = "PB12"
NET_PIN_RST  = "PD9"
NET_PIN_PWN  = "PE15"     # driven low at start, as in the DHCP example
# MAC / IP / DHCP / sound are set on the device: menu -> SETUP
SETTINGS_FILE = "blockfall_cfg.json"
OLD_SETTINGS = "tetris_cfg.json"   # read once if the new file is missing
UDP_PORT  = 5005
# =====================================================

rgb = st7789.color565
BLACK = rgb(0, 0, 0); WHITE = rgb(255, 255, 255)
GRAY = rgb(60, 60, 60); DIM = rgb(120, 120, 120)
CYAN = rgb(0, 240, 240); YELLOW = rgb(240, 240, 0)
GREEN = rgb(0, 240, 0); RED = rgb(240, 0, 0)
COLORS = [BLACK, rgb(255,105,180), rgb(45,212,191), rgb(170,220,40),
          rgb(120,150,255), rgb(255,200,60), rgb(200,100,255), rgb(255,90,70)]

# ---------------- Tiny 3x5 font ----------------
FONT = {
 "0":"111101101101111","1":"010110010010111","2":"111001111100111",
 "3":"111001111001111","4":"101101111001001","5":"111100111001111",
 "6":"111100111101111","7":"111001001001001","8":"111101111101111",
 "9":"111101111001111",
 "A":"010101111101101","B":"110101110101110","C":"111100100100111",
 "D":"110101101101110","E":"111100110100111","G":"111100101101111",
 "I":"111010010010111","K":"101101110101101","L":"100100100100111",
 "M":"101111111101101","N":"111101101101101","O":"111101101101111",
 "P":"111101111100100","R":"110101110101101","S":"111100111001111",
 "T":"111010010010010","U":"101101101101111","V":"101101101101010",
 "X":"101101010101101","Y":"101101010010010",":":"000010000010000",
 "-":"000000111000000"," ":"000000000000000",
}

def draw_text(lcd, x, y, txt, s=3, color=WHITE, bg=BLACK, clear_w=None):
    if clear_w:
        lcd.fill_rect(x, y, clear_w, 5 * s, bg)
    for i, ch in enumerate(txt):
        bits = FONT.get(ch, FONT[" "])
        for r in range(5):
            for c in range(3):
                if bits[r * 3 + c] == "1":
                    lcd.fill_rect(x + i * 4 * s + c * s, y + r * s, s, s, color)

def text_center(lcd, y, txt, s=3, color=WHITE):
    w = len(txt) * 4 * s - s
    draw_text(lcd, (LCD_W - w) // 2, y, txt, s, color)


FONT.update({"W":"101101101111101","H":"101101111101101",
             ">":"100010001010100",".":"000000000000010","!":"010010010000010",
             "F":"111100110100100"})
COLORS.append(rgb(110, 110, 110))   # 8 = garbage rows

STOP = [False]   # set by Ctrl+C caught inside the audio callback

# ---------------- Settings (saved on flash) ----------------
def default_cfg():
    try:
        import machine
        tail = list(machine.unique_id()[-3:])   # unique per chip
    except Exception:
        tail = [1, 2, random.getrandbits(8)]
    return {"mac": [0x00, 0x08, 0xDC] + tail, "dhcp": True,
            "ip": [192, 168, 1, 20], "mask": [255, 255, 255, 0],
            "gw": [192, 168, 1, 1], "sound": True,
            "peer_auto": True, "peer": [192, 168, 1, 21]}

def load_cfg():
    cfg = default_cfg()
    try:
        import json
        try:
            f = open(SETTINGS_FILE)
        except OSError:
            f = open(OLD_SETTINGS)
        with f:
            cfg.update(json.load(f))
        print("cfg: loaded", SETTINGS_FILE)
    except Exception:
        print("cfg: using defaults")
    return cfg

def save_cfg(cfg):
    import json
    with open(SETTINGS_FILE, "w") as f:
        json.dump(cfg, f)

CFG = load_cfg()
ipstr = lambda a: ".".join(str(x) for x in a)

# ---------------- Sound (I2S) ----------------
NOTES = {
 "A3":220,"C4":262,"D4":294,"E4":330,"G4":392,"GS4":415,"A4":440,"B4":494,
 "C5":523,"D5":587,"E5":659,"F5":698,"G5":784,"GS5":831,"A5":880,
 "C6":1047,"E6":1319,"G6":1568,
}
# Korobeiniki (Russian folk tune, public domain), (note, beats)
THEME = [
 ("E5",1),("B4",.5),("C5",.5),("D5",1),("C5",.5),("B4",.5),
 ("A4",1),("A4",.5),("C5",.5),("E5",1),("D5",.5),("C5",.5),
 ("B4",1.5),("C5",.5),("D5",1),("E5",1),("C5",1),("A4",1),("A4",1),("R",1),
 ("R",.5),("D5",1),("F5",.5),("A5",1),("G5",.5),("F5",.5),
 ("E5",1.5),("C5",.5),("E5",1),("D5",.5),("C5",.5),
 ("B4",1),("B4",.5),("C5",.5),("D5",1),("E5",1),("C5",1),("A4",1),("A4",1),("R",1),
]
SFX_BOOT   = [("C5",80),("E5",80),("G5",80),("C6",200)]
SFX_START  = [("E5",60),("A5",120)]
SFX_ROTATE = [("A5",25)]
SFX_LOCK   = [("C4",35)]
SFX_LINE   = [("C6",50),("E6",50),("G6",90)]
SFX_FOUR = [("C6",60),("E6",60),("G6",60),("C6",60),("E6",60),("G6",160)]
SFX_OVER   = [("E5",150),("D5",150),("C5",150),("B4",150),("A4",400)]
SFX_BACK   = [("E5",50),("C5",80)]
CHUNK   = 128        # frames per I2S write (~8 ms at 16 kHz)
GAP     = 480        # silence between notes (frames) for articulation
LEAD_MS = 120        # polling mode: audio queued ahead of real time

class Sound:
    def __init__(self):
        self.ok = False
        self.music = None
        self.sfx = []
        self.bpm = 140
        if not ENABLE_SOUND or not CFG.get("sound", True) or I2S is None or I2S_SD is None:
            print("sound: disabled"); return
        self.i2s = None
        for ibuf in (I2S_IBUF, 4096, 2048):     # fall back if heap is tight
            try:
                gc.collect()
                time.sleep_ms(50)
                self.i2s = I2S(I2S_ID, sck=Pin(I2S_SCK), ws=Pin(I2S_WS),
                               sd=Pin(I2S_SD), mode=I2S.TX, bits=16,
                               format=I2S.STEREO if I2S_STEREO else I2S.MONO,
                               rate=I2S_RATE, ibuf=ibuf)
                time.sleep_ms(20)
                print("sound: ibuf", ibuf, "free", gc.mem_free())
                break
            except MemoryError as e:
                print("sound: ibuf", ibuf, "failed:", e)
            except Exception as e:
                print("sound: init failed:", e); return
        if self.i2s is None:
            return
        gc.collect()
        self.ch = 2 if I2S_STEREO else 1
        self.silence = array("h", [0] * (CHUNK * (2 if I2S_STEREO else 1)))
        self.waves = {"R": self.silence}
        for name, hz in NOTES.items():
            try:
                self.waves[name] = self._wave(hz)
            except MemoryError:
                print("sound: low RAM, skipping note", name)
                self.waves[name] = self.silence
            gc.collect()
        print("sound: tones ready, free", gc.mem_free())
        self.mi = 0; self.mleft = 0; self.mbuf = self.silence; self.mgap = 0
        self.sleft = 0; self.sbuf = None
        self.use_irq = True
        try:
            self.i2s.irq(self._cb)       # non-blocking: refill from callback
        except Exception:
            self.use_irq = False         # fall back to paced writes in update()
        self.ok = True
        self.t0 = time.ticks_ms()
        self.sent = 0                    # frames written (polling mode)
        print("sound: I2S ok,", "irq" if self.use_irq else "polling", "mode")
        self.i2s.write(self.silence)
        self.sent += len(self.silence) // self.ch

    def update(self):
        # Polling mode: keep ~LEAD_MS of audio queued without blocking the game.
        if not self.ok or self.use_irq: return
        elapsed = time.ticks_diff(time.ticks_ms(), self.t0)
        target = (elapsed + LEAD_MS) * I2S_RATE // 1000
        n = 0
        while self.sent < target and n < 4:
            buf = self._next()
            self.i2s.write(buf)
            self.sent += len(buf) // self.ch
            n += 1
        if self.sent < elapsed * I2S_RATE // 1000:   # underrun: resync
            self.t0 = time.ticks_ms(); self.sent = 0

    def _wave(self, hz):
        period = I2S_RATE / hz
        n = max(1, int(CHUNK / period + 0.5))
        L = int(n * period + 0.5)
        half = period / 2
        rep = 2 if I2S_STEREO else 1
        a = array("h", [0] * (L * rep))
        for i in range(L):
            v = VOLUME if (i % period) < half else -VOLUME
            for c in range(rep):
                a[i * rep + c] = v
        return a

    def _cb(self, _):
        try:
            self.i2s.write(self._next())
        except KeyboardInterrupt:
            STOP[0] = True           # pass Ctrl+C on to the main loop
        except Exception:
            pass

    def _next(self):
        # sound effects take priority (music pauses meanwhile)
        if self.sbuf is not None or self.sfx:
            if self.sleft <= 0:
                if self.sfx:
                    n, ms = self.sfx.pop(0)
                    self.sbuf = self.waves.get(n, self.silence)
                    self.sleft = ms * I2S_RATE // 1000
                else:
                    self.sbuf = None
            if self.sbuf is not None:
                self.sleft -= len(self.sbuf) // self.ch
                return self.sbuf
        if self.music:
            if self.mleft <= 0:
                if self.mgap > 0:
                    self.mgap -= CHUNK
                    return self.silence
                n, beats = self.music[self.mi]
                self.mi = (self.mi + 1) % len(self.music)
                self.mbuf = self.waves.get(n, self.silence)
                self.mleft = int(beats * I2S_RATE * 60 / self.bpm) - GAP
                self.mgap = GAP
            self.mleft -= len(self.mbuf) // self.ch
            return self.mbuf
        return self.silence

    def play_music(self, song=THEME):
        self.mi = 0; self.mleft = 0; self.mgap = 0
        self.music = song

    def stop_music(self):
        self.music = None

    def play(self, fx):
        self.sfx = list(fx); self.sleft = 0

    def set_level(self, level):
        self.bpm = min(140 + (level - 1) * 8, 230)   # speed up with level

    def wait_sfx(self, timeout_ms=2000):
        t = time.ticks_ms()
        while self.ok and (self.sfx or self.sbuf is not None):
            if time.ticks_diff(time.ticks_ms(), t) > timeout_ms: break
            self.update()
            time.sleep_ms(5)

    def deinit(self):
        if self.ok:
            self.music = None; self.sfx = []
            try:
                time.sleep_ms(50)
                self.i2s.deinit()
                time.sleep_ms(150)
            except Exception as e:
                print("i2s.deinit error:", e)
            self.ok = False

# ---------------- Buttons ----------------
class Button:
    def __init__(self, pin_id, repeat_ms=120):
        self.p = None if pin_id is None else Pin(
            pin_id, Pin.IN, Pin.PULL_UP if BTN_ACTIVE_LOW else Pin.PULL_DOWN)
        self.last = 0; self.held = False; self.repeat = repeat_ms
    def is_down(self):
        if self.p is None: return False
        v = self.p.value()
        return v == 0 if BTN_ACTIVE_LOW else v == 1
    def pressed(self, now):
        if not self.is_down():
            self.held = False; return False
        if not self.held:
            self.held = True; self.last = now; return True
        if self.repeat and time.ticks_diff(now, self.last) > self.repeat:
            self.last = now; return True
        return False

# ---------------- Game ----------------
COLS, ROWS = 10, 20
CELL = min((LCD_W - 80) // COLS, (LCD_H - 10) // ROWS)
BX = 5
BY = (LCD_H - ROWS * CELL) // 2
SX = BX + COLS * CELL + 10          # side panel x

SHAPES = [None,
    [(0,1),(1,1),(2,1),(3,1)], [(1,0),(2,0),(1,1),(2,1)],
    [(1,0),(0,1),(1,1),(2,1)], [(1,0),(2,0),(0,1),(1,1)],
    [(0,0),(1,0),(1,1),(2,1)], [(0,0),(0,1),(1,1),(2,1)],
    [(2,0),(0,1),(1,1),(2,1)]]

# ---------------- Network (battle) ----------------
class Link:
    """UDP link to the other board. Finds the peer by broadcast if PEER_IP is None."""
    def __init__(self):
        self.ok = False; self.sock = None; self.last_rx = 0
        self.peer = None; self.ip = None; self.bcast = None
        try:
            import network, socket
            self.socket = socket
            spi = SPI(NET_SPI_ID, baudrate=NET_SPI_BAUD, polarity=0, phase=0)
            cs  = Pin(NET_PIN_CS, Pin.OUT)
            rst = Pin(NET_PIN_RST, Pin.OUT)
            if NET_PIN_PWN is not None:
                self.pwn = Pin(NET_PIN_PWN, Pin.OUT, value=0)
            nic = network.WIZNET5K(spi, cs, rst)
            nic.active(True)
            try: nic.config(mac=bytes(CFG["mac"]))
            except Exception as e: print("net: mac not set:", e)
            if CFG["dhcp"]:
                nic.ifconfig("dhcp")
            else:
                nic.ifconfig((ipstr(CFG["ip"]), ipstr(CFG["mask"]),
                              ipstr(CFG["gw"]), "8.8.8.8"))
            t = time.ticks_ms()
            while not nic.isconnected():
                if time.ticks_diff(time.ticks_ms(), t) > 10000:
                    print("net: no link / DHCP timeout"); return
                time.sleep_ms(200)
            cfg = nic.ifconfig()
            self.ip = cfg[0]
            print("net: IP", cfg[0], "mask", cfg[1], "gw", cfg[2])
            ip = [int(x) for x in cfg[0].split(".")]
            sn = [int(x) for x in cfg[1].split(".")]
            self.bcast = (".".join(str(ip[i] | (~sn[i] & 255)) for i in range(4)),
                          UDP_PORT)
            if not CFG.get("peer_auto", True):
                self.peer = (ipstr(CFG["peer"]), UDP_PORT)
                print("net: peer fixed to", self.peer[0])
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.bind(("0.0.0.0", UDP_PORT))
            s.setblocking(False)
            self.sock = s
            self.ok = True
        except Exception as e:
            print("net: init failed:", e)

    def send(self, msg):
        if not self.ok: return
        try: self.sock.sendto(msg.encode(), self.peer or self.bcast)
        except Exception: pass

    def recv_all(self):
        out = []
        if not self.ok: return out
        for _ in range(8):
            try:
                data, addr = self.sock.recvfrom(64)
            except OSError:
                break
            if not data or addr[0] == self.ip:     # ignore our own broadcast
                continue
            if self.peer is None:
                self.peer = (addr[0], UDP_PORT)
                print("net: found peer", addr[0])
            elif addr[0] != self.peer[0]:
                continue                            # not our opponent
            self.last_rx = time.ticks_ms()
            out.append(data.decode())
        return out

    def flush(self):
        self.recv_all()

GARBAGE_SEND = (0, 0, 1, 2, 4)   # lines cleared -> garbage rows sent

# ---------------- Game ----------------
class Game:
    def __init__(self, lcd):
        self.lcd = lcd
        self.battle = False

    def reset(self, battle=False):
        self.battle = battle
        self.board = [[0] * COLS for _ in range(ROWS)]
        self.score = 0; self.lines = 0; self.level = 1
        self.over = False
        self.pending = 0        # garbage waiting to rise
        self.outgoing = 0       # garbage to send after a clear
        self.opp_h = 0
        self.lcd.fill(BLACK)
        self.lcd.fill_rect(BX - 2, BY - 2, COLS * CELL + 4, ROWS * CELL + 4, GRAY)
        self.lcd.fill_rect(BX, BY, COLS * CELL, ROWS * CELL, BLACK)
        draw_text(self.lcd, SX, BY, "SCORE", 2, DIM)
        draw_text(self.lcd, SX, BY + 40, "LEVEL", 2, DIM)
        draw_text(self.lcd, SX, BY + 80, "NEXT", 2, DIM)
        if battle:
            draw_text(self.lcd, SX, BY + 135, "VS", 2, RED)
            draw_text(self.lcd, SX + 30, BY + 135, "IN", 2, DIM)
        draw_text(self.lcd, SX, BY + ROWS * CELL - 10, "B:BACK", 2, DIM)
        self.next_kind = random.randint(1, 7)
        self.spawn()
        self.draw_score()
        self.draw_battle()

    def spawn(self):
        self.kind = self.next_kind
        self.next_kind = random.randint(1, 7)
        self.cells = list(SHAPES[self.kind])
        self.px, self.py = 3, 0
        if not self.fits(self.cells, self.px, self.py):
            self.over = True
        self.draw_next()
        self.draw_piece(COLORS[self.kind])

    def fits(self, cells, px, py):
        for x, y in cells:
            x += px; y += py
            if x < 0 or x >= COLS or y >= ROWS: return False
            if y >= 0 and self.board[y][x]: return False
        return True

    def draw_cell(self, x, y, color):
        if y < 0: return
        if color:
            self.lcd.fill_rect(BX + x * CELL, BY + y * CELL, CELL - 1, CELL - 1, color)
        else:
            self.lcd.fill_rect(BX + x * CELL, BY + y * CELL, CELL, CELL, BLACK)

    def draw_piece(self, color):
        for x, y in self.cells:
            self.draw_cell(x + self.px, y + self.py, color)

    def move(self, dx, dy, cells=None):
        new = cells or self.cells
        if self.fits(new, self.px + dx, self.py + dy):
            self.draw_piece(0)
            self.cells = new; self.px += dx; self.py += dy
            self.draw_piece(COLORS[self.kind])
            return True
        return False

    def rotate(self):
        if self.kind == 2: return False
        size = 4 if self.kind == 1 else 3
        rot = [(size - 1 - y, x) for x, y in self.cells]
        for kick in (0, -1, 1, -2, 2):
            if self.move(kick, 0, rot): return True
        return False

    def height(self):
        for y in range(ROWS):
            if any(self.board[y]): return ROWS - y
        return 0

    def lock(self):
        for x, y in self.cells:
            if self.py + y < 0:
                self.over = True; return 0
            self.board[self.py + y][self.px + x] = self.kind
        full = [r for r in range(ROWS) if all(self.board[r])]
        n = len(full)
        dirty = False
        if full:
            for r in full:
                del self.board[r]
                self.board.insert(0, [0] * COLS)
            self.lines += n
            self.score += (0, 100, 300, 500, 800)[n] * self.level
            self.level = 1 + self.lines // 10
            dirty = True
            if self.battle:
                send = GARBAGE_SEND[n]
                cancel = min(send, self.pending)   # clears block incoming first
                self.pending -= cancel
                self.outgoing += send - cancel
        if self.battle and self.pending and not full:
            dirty = self.rise(self.pending) or True
            self.pending = 0
        if dirty:
            self.redraw_board()
            self.draw_score()
            self.draw_battle()
        if not self.over:
            self.spawn()
        return n

    def rise(self, k):
        k = min(k, ROWS)
        for _ in range(k):
            if any(self.board[0]): self.over = True
            del self.board[0]
            hole = random.randint(0, COLS - 1)
            row = [8] * COLS; row[hole] = 0
            self.board.append(row)
        return True

    def redraw_board(self):
        for y in range(ROWS):
            for x in range(COLS):
                self.draw_cell(x, y, COLORS[self.board[y][x]])

    def draw_score(self):
        w = LCD_W - SX
        draw_text(self.lcd, SX, BY + 15, str(self.score), 3, WHITE, BLACK, w)
        draw_text(self.lcd, SX, BY + 55, str(self.level), 3, CYAN, BLACK, w)

    def draw_battle(self):
        if not self.battle: return
        # opponent stack height (bar) and incoming garbage (red bar)
        x, top, h = SX + 4, BY + 150, ROWS * 6
        self.lcd.fill_rect(x - 1, top - 1, 14, h + 2, GRAY)
        self.lcd.fill_rect(x, top, 12, h, BLACK)
        oh = min(self.opp_h, ROWS) * 6
        if oh: self.lcd.fill_rect(x, top + h - oh, 12, oh, CYAN)
        x2 = SX + 34
        self.lcd.fill_rect(x2 - 1, top - 1, 14, h + 2, GRAY)
        self.lcd.fill_rect(x2, top, 12, h, BLACK)
        ph = min(self.pending, ROWS) * 6
        if ph: self.lcd.fill_rect(x2, top + h - ph, 12, ph, RED)

    def draw_next(self):
        sy = BY + 95; c = max(CELL // 2, 6)
        self.lcd.fill_rect(SX, sy, 4 * c, 2 * c, BLACK)
        for x, y in SHAPES[self.next_kind]:
            self.lcd.fill_rect(SX + x * c, sy + y * c, c - 1, c - 1,
                               COLORS[self.next_kind])

    def fall_interval(self):
        return max(80, 600 - (self.level - 1) * 50)

    def show_banner(self, title, color):
        y = BY + ROWS * CELL // 2 - 30
        self.lcd.fill_rect(BX, y, COLS * CELL, 60, BLACK)
        draw_text(self.lcd, BX + 8, y + 5, title, 2, color)
        draw_text(self.lcd, BX + 8, y + 25, "A:RETRY", 2, WHITE)
        draw_text(self.lcd, BX + 8, y + 42, "B:MENU", 2, WHITE)

    def show_game_over(self):
        self.show_banner("GAME OVER", RED)

# ---------------- Screens ----------------
MENU = ["SOLO", "BATTLE", "SETUP"]

# ---------------- On-screen setup ----------------
ITEMS = ["MAC", "MODE", "IP", "MASK", "GW", "PEER", "P.IP", "SOUND", "SAVE", "BACK"]

def item_fields(cfg, key):
    """-> (list of field strings, separator)"""
    if key == "MAC":   return ["%02X" % b for b in cfg["mac"]], ":"
    if key == "MODE":  return ["DHCP" if cfg["dhcp"] else "STATIC"], ""
    if key == "IP":    return [str(x) for x in cfg["ip"]], "."
    if key == "MASK":  return [str(x) for x in cfg["mask"]], "."
    if key == "GW":    return [str(x) for x in cfg["gw"]], "."
    if key == "PEER":  return ["AUTO" if cfg["peer_auto"] else "MANUAL"], ""
    if key == "P.IP":  return [str(x) for x in cfg["peer"]], "."
    if key == "SOUND": return ["ON" if cfg["sound"] else "OFF"], ""
    if key == "SAVE":  return ["AND RESET"], ""
    return [""], ""

def draw_item(lcd, cfg, i, sel, field=-1):
    key = ITEMS[i]; y = 44 + i * 23
    lcd.fill_rect(0, y - 2, LCD_W, 14, BLACK)
    static_only = ((key in ("IP", "MASK", "GW") and cfg["dhcp"]) or
                   (key == "P.IP" and cfg["peer_auto"]))
    on = i == sel
    draw_text(lcd, 4, y, (">" if on else " ") + key, 2, YELLOW if on else DIM)
    fields, sep = item_fields(cfg, key)
    x = 70
    for j, f in enumerate(fields):
        if j == field:       c = YELLOW
        elif static_only:    c = GRAY
        else:                c = WHITE if on else DIM
        draw_text(lcd, x, y, f, 2, c); x += len(f) * 8
        if sep and j < len(fields) - 1:
            draw_text(lcd, x, y, sep, 2, DIM); x += 8

def setup_help(lcd, editing):
    lcd.fill_rect(0, 285, LCD_W, 20, BLACK)
    text_center(lcd, 290, "UD:VALUE LR:MOVE A:OK" if editing
                else "A:EDIT  B:BACK", 2, DIM)

def run_setup(lcd):
    cfg = {k: (list(v) if isinstance(v, list) else v) for k, v in CFG.items()}
    up, dn = Button(BTN_UP, 150), Button(BTN_DOWN, 150)
    lf, rt = Button(BTN_LEFT, 200), Button(BTN_RIGHT, 200)
    a, b = Button(BTN_A, 0), Button(BTN_B, 0)
    lcd.fill(BLACK)
    text_center(lcd, 12, "SETUP", 3, WHITE)
    sel = 0
    for i in range(len(ITEMS)): draw_item(lcd, cfg, i, sel)
    if LINK and LINK.ip:
        text_center(lcd, 268, "NOW " + LINK.ip, 2, GRAY)
    setup_help(lcd, False)
    wait_release(a)
    while True:
        now = time.ticks_ms()
        if snd: snd.update()
        if up.pressed(now) or dn.pressed(now):
            old = sel
            sel = (sel + (1 if dn.held and not up.held else -1)) % len(ITEMS)
            draw_item(lcd, cfg, old, sel); draw_item(lcd, cfg, sel, sel)
        elif b.pressed(now):
            wait_release(b); return               # discard changes
        elif a.pressed(now):
            wait_release(a)
            key = ITEMS[sel]
            if key == "BACK": return
            if key == "MODE":
                cfg["dhcp"] = not cfg["dhcp"]
                for i in range(len(ITEMS)): draw_item(lcd, cfg, i, sel)
            elif key == "PEER":
                cfg["peer_auto"] = not cfg["peer_auto"]
                for i in range(len(ITEMS)): draw_item(lcd, cfg, i, sel)
            elif key == "SOUND":
                cfg["sound"] = not cfg["sound"]; draw_item(lcd, cfg, sel, sel)
            elif key == "SAVE":
                save_cfg(cfg)
                lcd.fill(BLACK)
                text_center(lcd, 130, "SAVED", 4, GREEN)
                text_center(lcd, 175, "RESETTING...", 2, DIM)
                if snd: snd.deinit()
                time.sleep_ms(800)
                import machine
                machine.reset()
            else:
                edit_list(lcd, cfg, sel, {"MAC": "mac", "IP": "ip",
                          "MASK": "mask", "GW": "gw", "P.IP": "peer"}[key], up, dn, lf, rt, a, b)
        time.sleep_ms(10)

def edit_list(lcd, cfg, sel, k, up, dn, lf, rt, a, b):
    vals = cfg[k]; backup = list(vals); j = 0
    setup_help(lcd, True)
    draw_item(lcd, cfg, sel, sel, j)
    while True:
        now = time.ticks_ms()
        if snd: snd.update()
        ch = False
        if up.pressed(now):   vals[j] = (vals[j] + 1) & 0xFF; ch = True
        elif dn.pressed(now): vals[j] = (vals[j] - 1) & 0xFF; ch = True
        elif rt.pressed(now): j = (j + 1) % len(vals); ch = True
        elif lf.pressed(now): j = (j - 1) % len(vals); ch = True
        elif a.pressed(now):
            wait_release(a); break
        elif b.pressed(now):
            vals[:] = backup; wait_release(b); break
        if ch: draw_item(lcd, cfg, sel, sel, j)
        time.sleep_ms(10)
    if k == "mac": vals[0] &= 0xFE            # keep MAC unicast
    draw_item(lcd, cfg, sel, sel)
    setup_help(lcd, False)

def start_screen(lcd, sel=0):
    lcd.fill(BLACK)
    c = 16; x0 = (LCD_W - 7 * c) // 2
    for i in range(7):
        lcd.fill_rect(x0 + i * c, 40, c - 2, c - 2, COLORS[i + 1])
    text_center(lcd, 84, "BLOCK FALL", 5, WHITE)
    for i in range(7):
        lcd.fill_rect(x0 + i * c, 125, c - 2, c - 2, COLORS[7 - i])
    draw_menu(lcd, sel)
    text_center(lcd, 280, "A:OK  B:EXIT", 2, DIM)

def draw_menu(lcd, sel):
    for i, name in enumerate(MENU):
        y = 165 + i * 32
        lcd.fill_rect(0, y, LCD_W, 15, BLACK)
        col = GREEN if i == sel else DIM
        text_center(lcd, y, (">" if i == sel else " ") + name, 3, col)

def wait_screen(lcd):
    lcd.fill(BLACK)
    text_center(lcd, 110, "WAITING", 4, WHITE)
    text_center(lcd, 150, "FOR PLAYER 2", 2, DIM)
    text_center(lcd, 175, "MY IP " + (LINK.ip or "-"), 2, DIM)
    text_center(lcd, 260, "B:CANCEL", 2, DIM)

def wait_release(*btns):
    while any(b.is_down() for b in btns):
        if snd: snd.update()
        time.sleep_ms(10)

def main():
    global snd
    gc.collect()
    print("startup free:", gc.mem_free())
    snd = Sound()                     # allocate I2S buffers first
    spi = SPI(SPI_ID, baudrate=SPI_BAUD, polarity=0, phase=0)
    lcd = st7789.ST7789(
        spi, LCD_W, LCD_H,
        reset=Pin(PIN_RST, Pin.OUT),
        dc=Pin(PIN_DC, Pin.OUT),
        cs=Pin(PIN_CS, Pin.OUT) if PIN_CS is not None else None,
        backlight=Pin(PIN_BL, Pin.OUT) if PIN_BL is not None else None,
        rotation=ROTATION)
    try:
        lcd.inversion_mode(INVERT_COLORS)
    except AttributeError:
        pass

    lcd.fill(BLACK)
    text_center(lcd, 150, "NETWORK...", 2, DIM)
    global LINK
    link = LINK = Link()

    if snd.ok:
        snd.play(SFX_BOOT); snd.wait_sfx()
    random.seed(time.ticks_us())
    game = Game(lcd)
    bl, br = Button(BTN_LEFT), Button(BTN_RIGHT)
    bdn = Button(BTN_DOWN, 50)
    bup = Button(BTN_UP, 0)
    ba, bb = Button(BTN_A, 0), Button(BTN_B, 0)
    bmu, bmd = Button(BTN_UP, 0), Button(BTN_DOWN, 0)   # menu navigation

    sel = 0
    state = "menu"
    start_screen(lcd, sel)
    snd.set_level(1); snd.play_music()
    last_fall = time.ticks_ms()
    last_tx = 0

    def new_game(battle=False):
        game.reset(battle); snd.set_level(1); snd.play(SFX_START); snd.play_music()

    def to_menu():
        snd.play(SFX_BACK); snd.set_level(1); snd.play_music()
        start_screen(lcd, sel)

    while True:
        if STOP[0]:
            raise KeyboardInterrupt
        if bb.is_down() and ba.is_down():          # A+B held = emergency exit
            raise KeyboardInterrupt
        now = time.ticks_ms()

        # ---------- menu ----------
        if state == "menu":
            if bmu.pressed(now) or bmd.pressed(now):
                sel = (sel + (1 if bmd.held and not bmu.held else -1)) % len(MENU)
                draw_menu(lcd, sel); snd.play(SFX_ROTATE)
            elif ba.pressed(now):
                wait_release(ba)
                if MENU[sel] == "SOLO":
                    new_game(False); state = "play"; last_fall = time.ticks_ms()
                elif MENU[sel] == "SETUP":
                    run_setup(lcd); start_screen(lcd, sel)
                elif not link.ok:
                    text_center(lcd, 250, "NO NETWORK!", 2, RED)
                else:
                    link.flush(); wait_screen(lcd); state = "wait"
            elif bb.pressed(now):
                snd.stop_music(); snd.play(SFX_BACK)
                lcd.fill(BLACK)
                text_center(lcd, LCD_H // 2 - 10, "BYE", 4, WHITE)
                snd.wait_sfx(800)
                time.sleep_ms(300)
                snd.deinit()
                lcd.fill(BLACK)
                if PIN_BL is not None:
                    Pin(PIN_BL, Pin.OUT).value(0)
                return
            link.recv_all()          # drop stray packets
            snd.update()
            time.sleep_ms(10)
            continue

        # ---------- waiting for opponent ----------
        if state == "wait":
            if bb.pressed(now):
                wait_release(bb); link.send("Q"); to_menu(); state = "menu"; continue
            if time.ticks_diff(now, last_tx) > 300:
                link.send("R"); last_tx = now
            for m in link.recv_all():
                if m == "R" or m == "G0":
                    link.send("G0")          # "go" ack
                    new_game(True); state = "play"; last_fall = time.ticks_ms()
                    break
            snd.update()
            time.sleep_ms(10)
            continue

        # ---------- game over / win ----------
        if state == "over":
            if ba.pressed(now):
                wait_release(ba)
                if game.battle:
                    link.flush(); wait_screen(lcd); state = "wait"
                else:
                    new_game(False); state = "play"; last_fall = time.ticks_ms()
            elif bb.pressed(now):
                wait_release(bb); to_menu(); state = "menu"
            if game.battle:
                for m in link.recv_all():
                    pass
            snd.update()
            time.sleep_ms(10)
            continue

        # ---------- playing ----------
        if bb.pressed(now):
            wait_release(bb)
            if game.battle: link.send("L")       # forfeit
            to_menu(); state = "menu"
            continue

        if game.battle:
            for m in link.recv_all():
                if m.startswith("A"):            # attack: garbage rows
                    try: game.pending += int(m[1:])
                    except ValueError: pass
                    game.draw_battle(); snd.play(SFX_LOCK)
                elif m.startswith("H"):          # opponent height
                    try: game.opp_h = int(m[1:])
                    except ValueError: pass
                    game.draw_battle()
                elif m == "L":                   # opponent topped out / quit
                    snd.stop_music(); snd.play(SFX_FOUR)
                    game.show_banner("YOU WIN!", GREEN); state = "over"
                    break
            if state == "over":
                continue
            if time.ticks_diff(now, last_tx) > 500:
                link.send("H%d" % game.height()); last_tx = now

        if bl.pressed(now):  game.move(-1, 0)
        if br.pressed(now):  game.move(1, 0)
        if ba.pressed(now) and game.rotate(): snd.play(SFX_ROTATE)
        if bdn.pressed(now):
            if game.move(0, 1): game.score += 1
            last_fall = now
        drop = False
        if bup.pressed(now):
            rows = 0
            while game.move(0, 1): rows += 1
            game.score += rows * 2
            game.draw_score()
            drop = True

        if drop or time.ticks_diff(now, last_fall) >= game.fall_interval():
            last_fall = now
            if not game.move(0, 1):
                lvl = game.level
                n = game.lock()
                if game.battle and game.outgoing:
                    link.send("A%d" % game.outgoing); game.outgoing = 0
                if game.over:
                    snd.stop_music(); snd.play(SFX_OVER)
                    if game.battle:
                        for _ in range(3): link.send("L")   # UDP: send a few times
                        game.show_banner("YOU LOSE", RED)
                    else:
                        game.show_game_over()
                    state = "over"
                elif n == 4: snd.play(SFX_FOUR)
                elif n:      snd.play(SFX_LINE)
                else:        snd.play(SFX_LOCK)
                if game.level != lvl: snd.set_level(game.level)
        snd.update()
        time.sleep_ms(5)

snd = None
LINK = None
try:
    main()
except KeyboardInterrupt:
    print("Stopped by Ctrl+C")
finally:
    if snd is not None:
        try: snd.i2s.irq(None)
        except Exception: pass
        snd.deinit()
    print("exited cleanly")
    gc.collect()
