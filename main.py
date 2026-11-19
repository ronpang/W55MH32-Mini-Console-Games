# Game launcher for W55MH32L-EVB + ST7789.
# Picking a game writes a one-shot flag file and resets the board, so each
# game starts with the whole heap. B on a game's title screen comes back here,
# and so does any machine.reset() from a game (no flag file = games menu).
import os, time, machine
from machine import Pin

GAMES = (("BLOCK FALL", "blockfall", "run_blockfall"),
         ("TANK BATTLE", "tank", "run_tank"),
         ("BUBBLE BLAST", "bubble", "run_bubble"))


def requested(flag):
    try:
        os.stat(flag)
    except OSError:
        return False
    os.remove(flag)
    return True


def launch(mod):
    try:
        m = __import__(mod)             # blockfall starts running on import
        if hasattr(m, "run"):
            m.run()                     # returns on B in the game's title
        machine.reset()                 # back to this menu
    except KeyboardInterrupt:
        print("stopped, staying in REPL")
    except Exception as e:
        print(mod, "error:", e)
        time.sleep(3)
        machine.reset()


def menu():
    from machine import SPI
    import st7789py as st7789
    spi = SPI(1, baudrate=36_000_000, polarity=0, phase=0)
    lcd = st7789.ST7789(spi, 240, 320,
                        reset=Pin("PC5", Pin.OUT), dc=Pin("PB0", Pin.OUT),
                        cs=Pin("PC4", Pin.OUT), backlight=Pin("PB1", Pin.OUT),
                        rotation=0)
    lcd.inversion_mode(False)
    c = st7789.color565
    W, G, Y = c(255, 255, 255), c(110, 110, 110), c(255, 220, 40)
    FCH = "ABCDEFGHIJKLMNOPQRSTUVWXYZ:>"
    FDAT = ("25755656563444365556746477464434553557557222711152"
            "55655444475775565555255526564425563656553421672222"
            "5555755552557755525555222712470202042124")

    def text(x, y, s, sc, col):
        for ch in s:
            i = FCH.find(ch)
            if i >= 0:
                for r in range(5):
                    v = ord(FDAT[i * 5 + r]) - 48
                    for k in range(3):
                        if v & (4 >> k):
                            lcd.fill_rect(x + k * sc, y + r * sc, sc, sc, col)
            x += 4 * sc

    def ctext(y, s, sc, col):
        text((240 - (len(s) * 4 * sc - sc)) // 2, y, s, sc, col)

    def draw(sel):
        for i in range(len(GAMES)):
            y = 120 + i * 40
            lcd.fill_rect(0, y, 240, 15, 0)
            ctext(y, (">" if i == sel else " ") + GAMES[i][0], 3, Y if i == sel else G)

    up, dn, a = (Pin(p, Pin.IN, Pin.PULL_UP) for p in ("PA8", "PC7", "PF13"))
    lcd.fill(0)
    ctext(40, "GAMES", 6, W)
    ctext(290, "UP DOWN  A:PLAY", 2, G)
    sel = 0
    draw(sel)
    last = (1, 1, 1)
    while True:
        now = (up.value(), dn.value(), a.value())
        if now[0] == 0 and last[0]:
            sel = (sel - 1) % len(GAMES)
            draw(sel)
        elif now[1] == 0 and last[1]:
            sel = (sel + 1) % len(GAMES)
            draw(sel)
        elif now[2] == 0 and last[2]:
            open(GAMES[sel][2], "w").close()
            lcd.fill(0)
            machine.reset()
        last = now
        time.sleep_ms(30)


for name, mod, flag in GAMES:
    if requested(flag):
        launch(mod)
        break
else:
    try:
        menu()
    except KeyboardInterrupt:
        print("stopped, staying in REPL")
