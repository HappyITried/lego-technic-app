"""
LEGO Technic — Multi-Controller (Auto-Scan Hub)
================================================
Requirements: pip install bleak pygame

Supported controllers:
  - PS4 DualShock 4 / PS5 DualSense
  - Xbox One / Series controller
  - Generic gamepad (best-effort axis mapping)
  - Keyboard (fully remappable keybinds)

On launch, the app scans for nearby LEGO BLE hubs and lets you pick one.
Input auto-switches between keyboard and gamepad whenever you touch either.
"""

import asyncio
import threading
import json
import os
import ctypes
from collections import deque
import pygame
from bleak import BleakClient, BleakScanner

# Fix blurry/pixelated rendering on Windows high-DPI displays
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass

def _font(size, bold=False):
    """Load best available UI font with fallback."""
    for name in ("consolas", "couriernew", "lucidaconsole", "bahnschrift", "calibri", "segoeuivariable", "segoeui", "tahoma", "verdana", "arial"):
        try:
            f = pygame.font.SysFont(name, size, bold=bold)
            if f: return f
        except Exception:
            pass
    return pygame.font.SysFont(None, size, bold=bold)


CHAR_UUID   = "00001624-1212-efde-1623-785feabcd123"

# Config file lives next to the script, not the working directory
SCRIPT_DIR   = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE  = os.path.join(SCRIPT_DIR, "lego_controller_config.json")

MAX_SPEED        = 60
MAX_SPEED_SLOW   = 20
MAX_SPEED_SPORT  = 100   # full throttle, max accel/decel
MAX_SPEED_GRANDMA = 17   # grandma mode
MAX_STEER        = 50
LIGHT_MODES      = [0x00, 0x04]   # off, on
DEADZONE         = 0.08

# Speed modes cycle: 0 = Normal, 1 = Sport, 2 = Slow, 3 = Grandma
SPEED_MODES = [
    {"name": "NORMAL", "cap": MAX_SPEED,         "icon": "⚡", "color_idx": 0},
    {"name": "SPORT",  "cap": MAX_SPEED_SPORT,   "icon": "🏎", "color_idx": 2},
    {"name": "SLOW",   "cap": MAX_SPEED_SLOW,    "icon": "🐢", "color_idx": 1},
    {"name": "GRANDMA","cap": MAX_SPEED_GRANDMA, "icon": "👵", "color_idx": 1},
]

LEGO_KEYWORDS  = ["lego", "technic", "hub", "boost", "spike", "mindstorms", "powered up"]

# ── Colours — dark terminal palette ──────────────────────────────────────────
BG          = ( 15,  15,  15)   # near-black background
BG2         = ( 22,  22,  22)   # slightly lighter panels
PANEL       = ( 22,  22,  22)   # panel background
PANEL2      = ( 32,  32,  32)   # inner panel / track background

ACCENT      = (240, 160,  30)   # amber header / label
ACCENT_L    = (255, 200,  80)   # lighter amber
ACCENT2     = ( 80, 200, 120)   # green (positive / connected)
ACCENT3     = (240, 160,  30)   # amber (warning)
DANGER      = (220,  80,  80)   # red (negative / error)
PURPLE_CARD = ( 80, 120, 200)   # blue accent
PURPLE_DARK = ( 50,  90, 170)

TEXT        = (200, 200, 200)   # main body text
TEXT_DIM    = (110, 110, 110)   # muted / secondary text
TEXT_BRIGHT = (240, 240, 240)   # headings
TEXT_INV    = ( 15,  15,  15)   # dark text on light bg
BORDER      = ( 45,  45,  45)   # panel border
SEL         = ( 35,  35,  35)   # selection background
LEGO_GRN    = ( 80, 200, 120)

# ── Default keybinds ─────────────────────────────────────────────────────────
DEFAULT_KEYBINDS = {
    "forward":  pygame.K_w,
    "reverse":  pygame.K_s,
    "left":     pygame.K_a,
    "right":    pygame.K_d,
    "brake":    pygame.K_SPACE,
    "lights":   pygame.K_l,
    "slow":     pygame.K_LSHIFT,
    "sport":    pygame.K_LCTRL,
}

BIND_LABELS = {
    "forward": "Forward",
    "reverse": "Reverse",
    "left":    "Steer Left",
    "right":   "Steer Right",
    "brake":   "Brake",
    "lights":  "Cycle Lights",
    "slow":    "Cycle Speed Mode",
    "sport":   "Cycle Speed Mode (alt)",
}

# Controller bind fields and their labels
CTRL_BIND_KEYS    = ["axis_steer", "axis_throttle", "axis_brake", "btn_lights", "btn_slow", "btn_sport"]
CTRL_BIND_LABELS  = {
    "axis_steer":    "Steer Axis",
    "axis_throttle": "Throttle Axis",
    "axis_brake":    "Brake Axis",
    "btn_lights":    "Lights Button",
    "btn_slow":      "Cycle Speed Mode",
    "btn_sport":     "Cycle Speed Mode (alt)",
}
CTRL_BIND_TYPES   = {
    "axis_steer":    "axis",
    "axis_throttle": "axis",
    "axis_brake":    "axis",
    "btn_lights":    "button",
    "btn_slow":      "button",
    "btn_sport":     "button",
}

def default_config():
    return {
        "keybinds":     dict(DEFAULT_KEYBINDS),
        "controllers":  {},   # controller_name -> {axis_steer, axis_throttle, ...}
        "fullscreen":   False,
    }

def load_config():
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE) as f:
                data = json.load(f)
            cfg = default_config()
            # Restore keyboard binds
            kb = data.get("keybinds", {})
            for k in DEFAULT_KEYBINDS:
                cfg["keybinds"][k] = kb.get(k, DEFAULT_KEYBINDS[k])
            # Restore controller profiles
            cfg["controllers"] = data.get("controllers", {})
            cfg["fullscreen"]  = data.get("fullscreen", False)
            return cfg
        except Exception as e:
            print(f"[CFG] Load error: {e}")
    return default_config()

def save_config(cfg):
    try:
        with open(CONFIG_FILE, "w") as f:
            json.dump(cfg, f, indent=2)
        print(f"[CFG] Saved to {CONFIG_FILE}")
    except Exception as e:
        print(f"[CFG] Save error: {e}")

def get_controller_binds(cfg, controller_name, base_profile):
    """Get controller binds for a given controller name, falling back to profile defaults."""
    saved = cfg["controllers"].get(controller_name, {})
    result = {}
    for k in CTRL_BIND_KEYS:
        result[k] = saved.get(k, base_profile[k])
    return result

def save_controller_binds(cfg, controller_name, binds):
    cfg["controllers"][controller_name] = dict(binds)

# ── Controller profiles ───────────────────────────────────────────────────────
PROFILES = {
    "dualsense":           {"label": "PS5 DualSense",       "axis_steer": 0, "axis_throttle": 5, "axis_brake": 4, "btn_lights": 0,  "btn_slow": 10, "btn_sport": 11},
    "dualshock":           {"label": "PS4 DualShock 4",     "axis_steer": 0, "axis_throttle": 5, "axis_brake": 4, "btn_lights": 0,  "btn_slow": 10, "btn_sport": 11},
    "ps5 controller":      {"label": "PS5 Controller",      "axis_steer": 0, "axis_throttle": 5, "axis_brake": 4, "btn_lights": 0,  "btn_slow": 10, "btn_sport": 11},
    "ps4 controller":      {"label": "PS4 Controller",      "axis_steer": 0, "axis_throttle": 5, "axis_brake": 4, "btn_lights": 0,  "btn_slow": 10, "btn_sport": 11},
    "xbox wireless":       {"label": "Xbox Wireless",       "axis_steer": 0, "axis_throttle": 5, "axis_brake": 4, "btn_lights": 0,  "btn_slow": 10, "btn_sport": 9},
    "xbox":                {"label": "Xbox Controller",     "axis_steer": 0, "axis_throttle": 5, "axis_brake": 4, "btn_lights": 0,  "btn_slow": 10, "btn_sport": 9},
    "xinput":              {"label": "Xbox (XInput)",       "axis_steer": 0, "axis_throttle": 5, "axis_brake": 4, "btn_lights": 0,  "btn_slow": 10, "btn_sport": 9},
    "wireless controller": {"label": "PS4/PS5 (Wireless)", "axis_steer": 0, "axis_throttle": 5, "axis_brake": 4, "btn_lights": 0,  "btn_slow": 10, "btn_sport": 11},
}
GENERIC_PROFILE = {"label": "Generic Gamepad", "axis_steer": 0, "axis_throttle": 5, "axis_brake": 4, "btn_lights": 0, "btn_slow": 10, "btn_sport": 9}

def get_profile(name):
    nl = name.lower()
    best_key, best_profile = None, None
    for key, p in PROFILES.items():
        if key in nl:
            if best_key is None or len(key) > len(best_key):
                best_key, best_profile = key, p
    print(f"[JOY] Matched profile: {best_profile['label'] if best_profile else 'Generic Gamepad'} (key='{best_key}')")
    return best_profile if best_profile else GENERIC_PROFILE

# ── Shared state ──────────────────────────────────────────────────────────────
state = {
    "speed": 0, "steer": 0, "lights": 0x00,
    "quit": False, "status": "Scanning...",
    "hub_address": None, "hub_name": None,
    "speed_mode": 0,        # 0=Normal, 1=Slow, 2=Sport
    "slow_mode": False,     # kept for BLE compat, derived from speed_mode
    "input_mode": "keyboard",
}

scan_results = []
scan_done    = False
scan_error   = ""

# ── Helpers ───────────────────────────────────────────────────────────────────
def apply_deadzone(v, dz=DEADZONE):
    return v if abs(v) > dz else 0.0

def trigger_to_positive(raw):
    return (raw + 1.0) / 2.0

def build_drive_command(speed, steer, lights):
    speed = max(-100, min(100, int(speed)))
    steer = max(-100, min(100, int(steer)))
    return bytes([0x0d,0x00,0x81,0x36,0x11,0x51,0x00,0x03,0x00, speed&0xFF, steer&0xFF, lights, 0x00])

def build_calibrate_commands():
    return (bytes([0x0d,0x00,0x81,0x36,0x11,0x51,0x00,0x03,0x00,0x00,0x00,0x10,0x00]),
            bytes([0x0d,0x00,0x81,0x36,0x11,0x51,0x00,0x03,0x00,0x00,0x00,0x08,0x00]))

def is_lego_device(name):
    if not name: return False
    return any(k in name.lower() for k in LEGO_KEYWORDS)

def key_name(k):
    return pygame.key.name(k).upper() if k else "—"

# ── Drawing helpers ───────────────────────────────────────────────────────────
def draw_rect_rounded(surf, color, rect, r=8, border=0, border_color=None):
    pygame.draw.rect(surf, color, rect, border_radius=r)
    if border and border_color:
        pygame.draw.rect(surf, border_color, rect, border, border_radius=r)

def draw_panel(surf, rect, r=12):
    pygame.draw.rect(surf, PANEL, rect, border_radius=r)
    pygame.draw.rect(surf, BORDER, rect, 1, border_radius=r)

def draw_panel_colored(surf, rect, color, r=12):
    shadow_r = pygame.Rect(rect.x+2, rect.y+3, rect.w, rect.h)
    pygame.draw.rect(surf, tuple(max(0,c-40) for c in color), shadow_r, border_radius=r)
    pygame.draw.rect(surf, color, rect, border_radius=r)

def draw_pill(surf, color, rect, label, font, text_color=TEXT_BRIGHT):
    draw_rect_rounded(surf, color, rect, r=6)
    t = font.render(label, True, text_color)
    surf.blit(t, (rect.x + (rect.w - t.get_width())//2, rect.y + (rect.h - t.get_height())//2))

def draw_speedbar(surf, rect, value, max_val, color_pos, color_neg):
    pygame.draw.rect(surf, PANEL2, rect, border_radius=4)
    if value != 0:
        w = int(abs(value) / max_val * rect.w)
        col = color_pos if value > 0 else color_neg
        pygame.draw.rect(surf, col, (rect.x, rect.y, w, rect.h), border_radius=4)
    pygame.draw.rect(surf, BORDER, rect, 1, border_radius=4)

def draw_tag(surf, font, text, x, y, color):
    tw = font.size(text)[0]
    r = pygame.Rect(x, y, tw+12, 20)
    c_bg = tuple(min(255, v+150) for v in color)
    pygame.draw.rect(surf, c_bg, r, border_radius=10)
    surf.blit(font.render(text, True, color), (x+6, y+2))

# ── BLE Scanner ───────────────────────────────────────────────────────────────
scan_status   = "Idle"        # human-readable status line
scan_found    = None          # (name, address) once a LEGO device is found
scan_running  = False
scan_elapsed  = 0.0           # seconds since scan started

SCAN_TIMEOUT  = 600.0         # 10 minutes

async def _continuous_scan():
    global scan_status, scan_found, scan_running, scan_elapsed
    import time
    scan_elapsed = 0.0
    t0 = time.monotonic()
    attempt = 0
    while scan_running:
        elapsed = time.monotonic() - t0
        scan_elapsed = elapsed
        if elapsed >= SCAN_TIMEOUT:
            scan_status = "Timeout — no LEGO hub found after 10 min"
            scan_running = False
            return
        attempt += 1
        scan_status = f"Scanning... (attempt {attempt})"
        try:
            devices = await BleakScanner.discover(timeout=3.0)
            for d in devices:
                name = d.name or ""
                if is_lego_device(name):
                    scan_found   = (name, d.address)
                    scan_status  = f"Found: {name}"
                    scan_running = False
                    return
        except Exception as e:
            scan_status = f"Scan error: {e}"
            await asyncio.sleep(1.0)

def start_scan():
    global scan_running, scan_found, scan_status, scan_elapsed
    scan_running = True
    scan_found   = None
    scan_status  = "Starting..."
    scan_elapsed = 0.0
    asyncio.run(_continuous_scan())

import math as _math

def draw_logo(surf, x, y, size=28):
    """Smiley face logo for Happy I Tried."""
    cx, cy, r = x + size//2, y + size//2, size//2 - 1
    pygame.draw.circle(surf, ACCENT, (cx, cy), r)
    pygame.draw.circle(surf, (255,255,255), (cx-r//3, cy-r//4), r//5)
    pygame.draw.circle(surf, (255,255,255), (cx+r//3, cy-r//4), r//5)
    for deg in range(200, 341, 15):
        a = _math.radians(deg)
        pygame.draw.circle(surf, (255,255,255),
            (int(cx + r*0.52*_math.cos(a)), int(cy + r*0.52*_math.sin(a))), 2)

_tb_fonts = {}   # lazy-init so pygame is ready before font creation

def draw_titlebar(surf, font_title, font_small, W, fullscreen=False):
    """Custom frameless titlebar. Returns (close_rect, minimize_rect, fullscreen_rect)."""
    if not _tb_fonts:
        _tb_fonts['t'] = _font(18, bold=True)
        _tb_fonts['s'] = _font(15)
    tf = _tb_fonts['t']
    sf = _tb_fonts['s']
    TB_H = 30
    pygame.draw.rect(surf, BG2, (0, 0, W, TB_H))
    pygame.draw.line(surf, BORDER, (0, TB_H - 1), (W, TB_H - 1), 1)
    t_surf = tf.render("Happy I Tried", True, ACCENT)
    surf.blit(t_surf, (10, (TB_H - t_surf.get_height()) // 2))
    url_x = 10 + t_surf.get_width() + 10
    u_surf = sf.render("youtube.com/@HappyITried", True, TEXT_DIM)
    surf.blit(u_surf, (url_x, (TB_H - u_surf.get_height()) // 2))
    close_r = pygame.Rect(W - 28, 6, 18, 18)
    pygame.draw.circle(surf, DANGER, close_r.center, 8)
    min_r = pygame.Rect(W - 52, 6, 18, 18)
    pygame.draw.circle(surf, BORDER, min_r.center, 8)
    pygame.draw.rect(surf, TEXT_DIM, (min_r.centerx - 5, min_r.centery - 1, 10, 2))
    fs_r = pygame.Rect(W - 76, 6, 18, 18)
    fs_col = ACCENT if fullscreen else BORDER
    pygame.draw.circle(surf, fs_col, fs_r.center, 8)
    # Draw a small square-arrows icon inside the circle
    cx, cy = fs_r.centerx, fs_r.centery
    if fullscreen:
        # Two inward arrows (compress icon)
        pygame.draw.line(surf, BG2, (cx-4, cy-1), (cx-1, cy-1), 2)
        pygame.draw.line(surf, BG2, (cx-1, cy-4), (cx-1, cy-1), 2)
        pygame.draw.line(surf, BG2, (cx+1, cy+1), (cx+4, cy+1), 2)
        pygame.draw.line(surf, BG2, (cx+1, cy+1), (cx+1, cy+4), 2)
    else:
        # Two outward arrows (expand icon)
        pygame.draw.line(surf, BG2, (cx-4, cy-4), (cx-1, cy-4), 2)
        pygame.draw.line(surf, BG2, (cx-4, cy-4), (cx-4, cy-1), 2)
        pygame.draw.line(surf, BG2, (cx+1, cy+1), (cx+4, cy+1), 2)
        pygame.draw.line(surf, BG2, (cx+1, cy+1), (cx+1, cy+4), 2)
    return close_r, min_r, fs_r


# ═══════════════════════════════════════════════════════════════════════════════
# HUB SELECTION SCREEN  (auto-connects on first LEGO device found)
# ═══════════════════════════════════════════════════════════════════════════════
def hub_selection_screen():
    global scan_running

    pygame.init()
    cfg_scan   = load_config()
    is_fullscreen = cfg_scan.get("fullscreen", False)
    BASE_W, BASE_H = 1100, 620
    if is_fullscreen:
        info = pygame.display.Info()
        W, H = info.current_w, info.current_h
        screen = pygame.display.set_mode((W, H), pygame.FULLSCREEN)
    else:
        W, H   = BASE_W, BASE_H
        screen = pygame.display.set_mode((W, H), pygame.NOFRAME)
    pygame.display.set_caption("LEGO Hub Connect")

    font_title = _font(30, bold=True)
    font       = _font(24)
    font_small = _font(20)
    clock      = pygame.time.Clock()
    TB_H       = 30

    scan_thread = threading.Thread(target=start_scan, daemon=True)
    scan_thread.start()

    dot_timer = 0
    dots      = 0
    dragging  = False
    drag_ox = drag_oy = 0

    while True:
        clock.tick(30)
        dot_timer += 1
        if dot_timer % 8 == 0:
            dots = (dots + 1) % 4

        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                scan_running = False; pygame.display.quit(); return None
            elif event.type == pygame.KEYDOWN and event.key == pygame.K_ESCAPE:
                scan_running = False; pygame.display.quit(); return None
            elif event.type == pygame.KEYDOWN and event.key == pygame.K_F11:
                is_fullscreen = not is_fullscreen
                if is_fullscreen:
                    info = pygame.display.Info()
                    W, H = info.current_w, info.current_h
                    screen = pygame.display.set_mode((W, H), pygame.FULLSCREEN)
                else:
                    W, H = BASE_W, BASE_H
                    screen = pygame.display.set_mode((W, H), pygame.NOFRAME)
                cfg_scan["fullscreen"] = is_fullscreen
                save_config(cfg_scan)
            elif event.type == pygame.MOUSEBUTTONDOWN and event.button == 1:
                mx, my = event.pos
                close_r, min_r, fs_r = draw_titlebar(screen, font_title, font_small, W, is_fullscreen)
                if close_r.collidepoint(mx, my):
                    scan_running = False; pygame.display.quit(); return None
                elif min_r.collidepoint(mx, my):
                    if not is_fullscreen:
                        ctypes.windll.user32.ShowWindow(pygame.display.get_wm_info()["window"], 6)
                elif fs_r.collidepoint(mx, my):
                    is_fullscreen = not is_fullscreen
                    if is_fullscreen:
                        info = pygame.display.Info()
                        W, H = info.current_w, info.current_h
                        screen = pygame.display.set_mode((W, H), pygame.FULLSCREEN)
                    else:
                        W, H = BASE_W, BASE_H
                        screen = pygame.display.set_mode((W, H), pygame.NOFRAME)
                    cfg_scan["fullscreen"] = is_fullscreen
                    save_config(cfg_scan)
                elif my < TB_H and not is_fullscreen:
                    hwnd = pygame.display.get_wm_info()["window"]
                    r = (ctypes.c_long * 4)()
                    ctypes.windll.user32.GetWindowRect(hwnd, r)
                    pt = (ctypes.c_long * 2)()
                    ctypes.windll.user32.GetCursorPos(pt)
                    drag_ox, drag_oy = pt[0] - r[0], pt[1] - r[1]
                    dragging = True
            elif event.type == pygame.MOUSEMOTION:
                if dragging:
                    hwnd = pygame.display.get_wm_info()["window"]
                    pt = (ctypes.c_long * 2)()
                    ctypes.windll.user32.GetCursorPos(pt)
                    ctypes.windll.user32.MoveWindow(hwnd, pt[0]-drag_ox, pt[1]-drag_oy, W, H, False)
            elif event.type == pygame.MOUSEBUTTONUP and event.button == 1:
                dragging = False

        if scan_found is not None:
            pygame.display.quit(); return scan_found

        screen.fill(BG)
        close_r, _, _fs_r = draw_titlebar(screen, font_title, font_small, W, is_fullscreen)

        card_r = pygame.Rect(24, TB_H+14, W-48, H-TB_H-28)
        draw_panel(screen, card_r)

        spr     = 32
        card_cy = TB_H + 14 + (H - TB_H - 28) // 2
        block_x = W // 2 - 360
        spx     = block_x + spr
        spy     = card_cy
        tx      = block_x + spr * 2 + 32
        ty      = card_cy - 60

        pygame.draw.circle(screen, BG2, (spx, spy), spr)
        angle = (dot_timer * 7) % 360
        for seg in range(10):
            a   = _math.radians(angle + seg*36)
            alp = int(220*seg/10)
            col = (max(0, ACCENT[0]-(220-alp)//3), max(0, ACCENT[1]-(220-alp)//5), min(255, ACCENT[2]))
            pygame.draw.circle(screen, col,
                (int(spx+spr*_math.cos(a)), int(spy+spr*_math.sin(a))), 4)

        screen.blit(font.render("Scanning for LEGO hub...", True, TEXT_BRIGHT), (tx, ty))
        screen.blit(font_small.render(
            scan_status + ("." * dots if scan_running else ""), True, TEXT_DIM), (tx, ty+36))

        elapsed  = min(scan_elapsed, SCAN_TIMEOUT)
        pct      = elapsed / SCAN_TIMEOUT
        bar_r    = pygame.Rect(tx, ty+72, W - tx - 60, 8)
        pygame.draw.rect(screen, BG2, bar_r, border_radius=4)
        pygame.draw.rect(screen, ACCENT,
            (bar_r.x, bar_r.y, max(8, int(bar_r.w*pct)), bar_r.h), border_radius=4)

        mins = int(elapsed)//60; secs = int(elapsed)%60
        rem  = int(SCAN_TIMEOUT - elapsed); rm = rem//60; rs = rem%60
        screen.blit(font_small.render(
            f"{mins:02d}:{secs:02d} elapsed  \u00b7  {rm:02d}:{rs:02d} remaining", True, TEXT_DIM),
            (tx, ty+92))

        if not scan_running and scan_found is None:
            draw_tag(screen, font_small, "Timed out - no hub found", tx, ty+122, DANGER)
        else:
            screen.blit(font_small.render(
                "Will connect automatically  \u00b7  Esc to cancel", True, TEXT_DIM), (tx, ty+122))
        screen.blit(font_small.render(
            "Press the green button and make sure it's blinking", True, LEGO_GRN), (tx, ty+152))
        screen.blit(font_small.render(
            "Make sure the Lego Car was paired with this computer", True, TEXT_DIM), (tx, ty+182))
        screen.blit(font_small.render(
            "before opening this app.", True, TEXT_DIM), (tx, ty+207))

        pygame.draw.rect(screen, BORDER, (0, 0, W, H), 1, border_radius=2)
        pygame.display.flip()

    return None


# ═══════════════════════════════════════════════════════════════════════════════
# KEYBIND EDITOR SCREEN
# ═══════════════════════════════════════════════════════════════════════════════
def keybind_screen(cfg, connected_controllers):
    BASE_W, BASE_H = 1100, 620
    is_fullscreen = cfg.get("fullscreen", False)

    def _apply_display(fullscreen):
        if fullscreen:
            info = pygame.display.Info()
            return pygame.display.set_mode((info.current_w, info.current_h), pygame.FULLSCREEN), info.current_w, info.current_h
        else:
            return pygame.display.set_mode((BASE_W, BASE_H), pygame.NOFRAME), BASE_W, BASE_H

    screen, W, H = _apply_display(is_fullscreen)
    pygame.display.set_caption("Keybind Editor")

    font_title = _font(22, bold=True)
    font       = _font(22)
    font_small = _font(20)
    clock      = pygame.time.Clock()
    TB_H       = 30
    TAB_H      = 40
    LIST_Y     = TB_H + TAB_H + 28

    tabs       = [("Keyboard", "keyboard")]
    for name, _ in connected_controllers:
        tabs.append((name[:22], name))
    active_tab = 0

    kb_binds   = dict(cfg["keybinds"])
    ctrl_binds = {name: get_controller_binds(cfg, name, p) for name, p in connected_controllers}
    kb_actions = list(DEFAULT_KEYBINDS.keys())
    selected   = 0
    listening  = False
    dragging   = False
    drag_ox = drag_oy = 0

    while True:
        clock.tick(30)
        # Recalculate layout in case W/H changed (fullscreen toggle)
        LIST_Y       = TB_H + TAB_H + 28
        available    = H - LIST_Y - 52
        ROW_H        = max(38, available // max(len(kb_actions), 1))
        save_rect    = pygame.Rect(W-168, H-42, 148, 32)
        default_rect = pygame.Rect(16,    H-42, 148, 32)
        back_rect    = pygame.Rect(W//2-62, H-42, 124, 32)

        tab_name = tabs[active_tab][1]
        is_kb    = tab_name == "keyboard"
        actions  = kb_actions if is_kb else CTRL_BIND_KEYS
        binds    = kb_binds if is_kb else ctrl_binds.get(tab_name, {})

        for event in pygame.event.get():
            if event.type == pygame.QUIT: return cfg
            elif event.type == pygame.KEYDOWN:
                if event.key == pygame.K_F11 and not listening:
                    is_fullscreen = not is_fullscreen
                    screen, W, H = _apply_display(is_fullscreen)
                    cfg["fullscreen"] = is_fullscreen
                elif listening and is_kb:
                    if event.key != pygame.K_ESCAPE: binds[actions[selected]] = event.key
                    listening = False
                elif not listening:
                    if event.key == pygame.K_ESCAPE: return cfg
                    elif event.key == pygame.K_UP:   selected = (selected-1)%len(actions)
                    elif event.key == pygame.K_DOWN: selected = (selected+1)%len(actions)
                    elif event.key == pygame.K_RETURN: listening = True
                    elif event.key == pygame.K_TAB:
                        active_tab = (active_tab+1)%len(tabs); selected=0; listening=False
            elif event.type == pygame.JOYBUTTONDOWN and listening and not is_kb:
                if CTRL_BIND_TYPES[actions[selected]] == "button":
                    binds[actions[selected]] = event.button; listening = False
            elif event.type == pygame.JOYAXISMOTION and listening and not is_kb:
                if CTRL_BIND_TYPES[actions[selected]] == "axis" and abs(event.value)>0.5:
                    binds[actions[selected]] = event.axis; listening = False
            elif event.type == pygame.MOUSEMOTION:
                if dragging:
                    hwnd = pygame.display.get_wm_info()["window"]
                    pt = (ctypes.c_long * 2)()
                    ctypes.windll.user32.GetCursorPos(pt)
                    ctypes.windll.user32.MoveWindow(hwnd, pt[0]-drag_ox, pt[1]-drag_oy, W, H, False)
            elif event.type == pygame.MOUSEBUTTONUP and event.button == 1:
                dragging = False
            elif event.type == pygame.MOUSEBUTTONDOWN and event.button == 1:
                mx, my = event.pos
                close_r, min_r, fs_r = draw_titlebar(screen, font_title, font_small, W, is_fullscreen)
                if close_r.collidepoint(mx, my): return cfg
                if min_r.collidepoint(mx, my):
                    if not is_fullscreen:
                        ctypes.windll.user32.ShowWindow(pygame.display.get_wm_info()["window"], 6)
                elif fs_r.collidepoint(mx, my):
                    is_fullscreen = not is_fullscreen
                    screen, W, H = _apply_display(is_fullscreen)
                    cfg["fullscreen"] = is_fullscreen
                elif my < TB_H and not is_fullscreen:
                    hwnd = pygame.display.get_wm_info()["window"]
                    r = (ctypes.c_long * 4)()
                    ctypes.windll.user32.GetWindowRect(hwnd, r)
                    pt = (ctypes.c_long * 2)()
                    ctypes.windll.user32.GetCursorPos(pt)
                    drag_ox, drag_oy = pt[0] - r[0], pt[1] - r[1]
                    dragging = True
                tab_w = W // len(tabs)
                if TB_H <= my < TB_H+TAB_H:
                    ct = mx // tab_w
                    if ct < len(tabs): active_tab=ct; selected=0; listening=False
                elif save_rect.collidepoint(mx, my):
                    cfg["keybinds"] = kb_binds
                    for name, _ in connected_controllers:
                        save_controller_binds(cfg, name, ctrl_binds[name])
                    save_config(cfg); return cfg
                elif default_rect.collidepoint(mx, my):
                    if is_kb: kb_binds.update(DEFAULT_KEYBINDS)
                    else:
                        base = next(p for n, p in connected_controllers if n == tab_name)
                        ctrl_binds[tab_name] = {k: base[k] for k in CTRL_BIND_KEYS}
                elif back_rect.collidepoint(mx, my): return cfg
                else:
                    row = (my - LIST_Y) // ROW_H
                    if 0 <= row < len(actions):
                        if selected == row and not listening: listening = True
                        else: selected = row; listening = False

        screen.fill(BG)
        close_r, _, _fs_r = draw_titlebar(screen, font_title, font_small, W, is_fullscreen)

        tab_w = W // len(tabs)
        for i, (tlabel, _) in enumerate(tabs):
            tx = i * tab_w
            is_sel = i == active_tab
            pygame.draw.rect(screen, PANEL if is_sel else BG, (tx, TB_H, tab_w, TAB_H))
            if is_sel:
                pygame.draw.rect(screen, ACCENT, (tx, TB_H+TAB_H-2, tab_w, 2))
            t = font.render(tlabel, True, TEXT_BRIGHT if is_sel else TEXT_DIM)
            screen.blit(t, (tx+(tab_w-t.get_width())//2, TB_H+(TAB_H-t.get_height())//2))
        pygame.draw.line(screen, BORDER, (0, TB_H+TAB_H), (W, TB_H+TAB_H), 1)

        hint = "Click or Enter to remap  |  press key to assign" if is_kb else "Click or Enter  |  move axis or press button"
        screen.blit(font_small.render(hint, True, TEXT_DIM), (20, TB_H+TAB_H+5))

        for i, action in enumerate(actions):
            ry   = LIST_Y + i * ROW_H
            sel  = i == selected
            card = pygame.Rect(16, ry+2, W-32, ROW_H-4)
            if sel:
                draw_panel_colored(screen, card, SEL)
                pygame.draw.rect(screen, ACCENT, card, 2, border_radius=8)
            else:
                pygame.draw.rect(screen, PANEL2, card, border_radius=8)

            label = BIND_LABELS.get(action) or CTRL_BIND_LABELS.get(action, action)
            cy = ry + (ROW_H - font.size("A")[1]) // 2
            screen.blit(font.render(label, True, TEXT_BRIGHT if sel else TEXT), (32, cy))

            if not is_kb:
                btype = CTRL_BIND_TYPES[action]
                draw_tag(screen, font_small, btype, 260, cy,
                         ACCENT if btype == "axis" else ACCENT2)

            if sel and listening:
                val_text = "Press button/axis..." if not is_kb else "Press any key..."
                val_col  = ACCENT3
            else:
                v = binds.get(action, "?")
                val_text = key_name(v) if (is_kb and isinstance(v, int)) else str(v)
                val_col  = ACCENT if sel else TEXT_DIM

            pill_w = max(60, font.size(val_text)[0] + 24)
            pill_r = pygame.Rect(W-32-pill_w, ry + (ROW_H-26)//2, pill_w, 26)
            c_bg   = tuple(min(255, v+140) for v in ACCENT) if sel else BG2
            pygame.draw.rect(screen, c_bg, pill_r, border_radius=13)
            pygame.draw.rect(screen, val_col, pill_r, 1, border_radius=13)
            t = font.render(val_text, True, val_col)
            screen.blit(t, (pill_r.x+(pill_r.w-t.get_width())//2, pill_r.y+(pill_r.h-t.get_height())//2))

        for rect, bg, border, label in [
            (save_rect,    ACCENT2, None,   "Save & Exit"),
            (default_rect, BG2,    BORDER,  "Reset Defaults"),
            (back_rect,    BG2,    BORDER,  "Back"),
        ]:
            pygame.draw.rect(screen, bg, rect, border_radius=8)
            if border: pygame.draw.rect(screen, border, rect, 1, border_radius=8)
            t = font.render(label, True, TEXT_INV if bg == ACCENT2 else TEXT)
            screen.blit(t, (rect.x+(rect.w-t.get_width())//2, rect.y+(rect.h-t.get_height())//2))

        pygame.draw.rect(screen, BORDER, (0, 0, W, H), 1, border_radius=2)
        pygame.display.flip()
    return cfg


# ═══════════════════════════════════════════════════════════════════════════════
# MAIN CONTROLLER SCREEN
# ═══════════════════════════════════════════════════════════════════════════════
def pygame_loop(hub_name, hub_address):
    BASE_W, BASE_H = 1100, 620
    # pygame is already initialised by hub_selection_screen — just open the new window
    pygame.joystick.init()

    cfg        = load_config()
    keybinds   = cfg["keybinds"]
    is_fullscreen = cfg.get("fullscreen", False)

    def apply_display_mode(fullscreen):
        if not pygame.display.get_init():
            pygame.display.init()
        if fullscreen:
            info = pygame.display.Info()
            return pygame.display.set_mode((info.current_w, info.current_h), pygame.FULLSCREEN), info.current_w, info.current_h
        else:
            return pygame.display.set_mode((BASE_W, BASE_H), pygame.NOFRAME), BASE_W, BASE_H

    screen, W, H = apply_display_mode(is_fullscreen)

    font_title  = _font(22, bold=True)
    font        = _font(22)
    font_small  = _font(20)
    font_large  = _font(120, bold=True)
    clock       = pygame.time.Clock()
    light_idx  = 0
    joysticks  = {}   # instance_id -> Joystick
    joy        = None
    profile    = None   # base profile from PROFILES
    ctrl_binds = None   # active per-controller binds (may override profile)
    slow_flash = 0
    kb = {k: False for k in ["forward","reverse","left","right","brake"]}
    dragging = False
    drag_ox = drag_oy = 0
    speed_hist = deque(maxlen=140)

    # No vertical panels — Hub spans full width below the chart
    LP_W = W; MP_W = 0
    RP_X = 0; RP_W = W

    # Hub columns: left half / right half
    COL_W = W // 2

    keybind_btn = pygame.Rect(W // 2 + 10, H - 30, W // 2 - 20, 24)
    light_btn   = pygame.Rect(10,          H - 30, W // 2 - 20, 24)


    def add_joystick(device_index):
        """Init a joystick by its device_index (from JOYDEVICEADDED event)."""
        try:
            j = pygame.joystick.Joystick(device_index)
            j.init()
            iid = j.get_instance_id()
            joysticks[iid] = j
            print(f"[JOY] Added [{iid}] '{j.get_name()}'")
            return j
        except Exception as e:
            print(f"[JOY] Could not init joystick index {device_index}: {e}")
            return None

    def activate_joystick(j):
        nonlocal joy, profile, ctrl_binds
        if joy is not j:
            joy = j
            profile    = get_profile(j.get_name())
            ctrl_binds = get_controller_binds(cfg, j.get_name(), profile)
            state["input_mode"] = "controller"
            print(f"[JOY] Active: '{j.get_name()}' -> {profile['label']}")

    # Init any joysticks already connected at startup
    for i in range(pygame.joystick.get_count()):
        add_joystick(i)
    if joysticks:
        activate_joystick(next(iter(joysticks.values())))
    else:
        state["input_mode"] = "keyboard"

    while not state["quit"]:
        dt = clock.tick(60)
        slow_flash = (slow_flash + dt) % 1000

        try:
            events = pygame.event.get()
        except Exception as e:
            print(f"[EVT] pygame.event.get() error (hot-plug?): {e}")
            pygame.event.clear()
            # Re-scan joysticks after pygame internal glitch
            for i in range(pygame.joystick.get_count()):
                try:
                    j = pygame.joystick.Joystick(i)
                    j.init()
                    iid = j.get_instance_id()
                    if iid not in joysticks:
                        joysticks[iid] = j
                        print(f"[JOY] Recovered [{iid}] '{j.get_name()}'")
                        if joy is None:
                            activate_joystick(j)
                except Exception:
                    pass
            events = []

        for event in events:
            # ── Each event type uses its own `if`, not `elif`, so multiple  ──
            # ── event types in the same frame are all handled correctly.     ──

            if event.type == pygame.QUIT:
                state["quit"] = True

            # ── Keyboard events ──────────────────────────────────────────────
            if event.type == pygame.KEYDOWN:
                state["input_mode"] = "keyboard"
                k = event.key
                if k == pygame.K_F11:
                    is_fullscreen = not is_fullscreen
                    screen, W, H = apply_display_mode(is_fullscreen)
                if k == keybinds["lights"]:
                    light_idx = (light_idx+1) % len(LIGHT_MODES)
                    state["lights"] = LIGHT_MODES[light_idx]
                if k == keybinds["slow"]:    state["speed_mode"] = (state["speed_mode"] + 1) % len(SPEED_MODES)
                if k == keybinds["sport"]:   state["speed_mode"] = (state["speed_mode"] + 1) % len(SPEED_MODES)
                if k == keybinds["forward"]: kb["forward"] = True
                if k == keybinds["reverse"]: kb["reverse"] = True
                if k == keybinds["left"]:    kb["left"]    = True
                if k == keybinds["right"]:   kb["right"]   = True
                if k == keybinds["brake"]:   kb["brake"]   = True

            if event.type == pygame.KEYUP:
                k = event.key


                if k == keybinds["forward"]: kb["forward"] = False
                if k == keybinds["reverse"]: kb["reverse"] = False
                if k == keybinds["left"]:    kb["left"]    = False
                if k == keybinds["right"]:   kb["right"]   = False
                if k == keybinds["brake"]:   kb["brake"]   = False

            # ── Mouse / button events ────────────────────────────────────────
            if event.type == pygame.MOUSEMOTION:
                if dragging:
                    hwnd = pygame.display.get_wm_info()["window"]
                    pt = (ctypes.c_long * 2)()
                    ctypes.windll.user32.GetCursorPos(pt)
                    ctypes.windll.user32.MoveWindow(hwnd, pt[0]-drag_ox, pt[1]-drag_oy, W, H, False)

            if event.type == pygame.MOUSEBUTTONUP and event.button == 1:
                dragging = False

            if event.type == pygame.MOUSEBUTTONDOWN and event.button == 1:
                mx, my = event.pos
                close_r, min_r, fs_r = draw_titlebar(screen, font_title, font_small, W, is_fullscreen)
                if close_r.collidepoint(mx, my): state["quit"] = True
                elif min_r.collidepoint(mx, my):
                    if not is_fullscreen:
                        ctypes.windll.user32.ShowWindow(pygame.display.get_wm_info()["window"], 6)
                elif fs_r.collidepoint(mx, my):
                    is_fullscreen = not is_fullscreen
                    screen, W, H = apply_display_mode(is_fullscreen)
                elif my < 30 and not is_fullscreen:
                    hwnd = pygame.display.get_wm_info()["window"]
                    r = (ctypes.c_long * 4)()
                    ctypes.windll.user32.GetWindowRect(hwnd, r)
                    pt = (ctypes.c_long * 2)()
                    ctypes.windll.user32.GetCursorPos(pt)
                    drag_ox, drag_oy = pt[0] - r[0], pt[1] - r[1]
                    dragging = True
                if keybind_btn.collidepoint(mx, my):
                    connected = [(j.get_name(), get_profile(j.get_name())) for j in joysticks.values()]
                    cfg = keybind_screen(cfg, connected)
                    keybinds   = cfg["keybinds"]
                    if joy:
                        ctrl_binds = get_controller_binds(cfg, joy.get_name(), profile)
                    screen, W, H = apply_display_mode(is_fullscreen)
                elif light_btn.collidepoint(mx, my):
                    light_idx = (light_idx+1) % len(LIGHT_MODES)
                    state["lights"] = LIGHT_MODES[light_idx]

            # ── Gamepad events ───────────────────────────────────────────────
            if event.type == pygame.JOYAXISMOTION:
                if abs(event.value) > DEADZONE * 3:
                    j = joysticks.get(event.instance_id)
                    if j:
                        activate_joystick(j)
                        state["input_mode"] = "controller"

            if event.type == pygame.JOYBUTTONDOWN:
                j = joysticks.get(event.instance_id)
                if j:
                    activate_joystick(j)
                state["input_mode"] = "controller"
                binds_now = ctrl_binds if ctrl_binds else profile
                if binds_now:

                    if event.button == binds_now["btn_lights"]:
                        light_idx = (light_idx+1) % len(LIGHT_MODES)
                        state["lights"] = LIGHT_MODES[light_idx]
                    if event.button == binds_now["btn_slow"]:   state["speed_mode"] = (state["speed_mode"] + 1) % len(SPEED_MODES)
                    if event.button == binds_now["btn_sport"]:  state["speed_mode"] = (state["speed_mode"] + 1) % len(SPEED_MODES)

            if event.type == pygame.JOYDEVICEREMOVED:
                iid = event.instance_id
                if iid in joysticks:
                    print(f"[JOY] Disconnected '{joysticks[iid].get_name()}'")
                    gone = joysticks.pop(iid)
                    if joy is gone:
                        joy = None; profile = None
                        if joysticks:
                            activate_joystick(next(iter(joysticks.values())))
                        else:
                            state["input_mode"] = "keyboard"

            if event.type == pygame.JOYDEVICEADDED:
                # Hotplug invalidates existing Joystick objects — rebuild all refs
                prev_iid = joy.get_instance_id() if joy else None
                joysticks.clear()
                for i in range(pygame.joystick.get_count()):
                    add_joystick(i)
                if prev_iid and prev_iid in joysticks:
                    activate_joystick(joysticks[prev_iid])
                elif joysticks and joy is None:
                    activate_joystick(next(iter(joysticks.values())))

        # ── Update speed/steer ───────────────────────────────────────────────
        spd_cap = SPEED_MODES[state["speed_mode"]]["cap"]
        binds   = ctrl_binds if ctrl_binds else profile

        if state["input_mode"] == "controller" and joy and binds:
            raw_steer = apply_deadzone(joy.get_axis(binds["axis_steer"]))
            raw_t     = trigger_to_positive(joy.get_axis(binds["axis_throttle"]))
            raw_b     = trigger_to_positive(joy.get_axis(binds["axis_brake"]))
            state["speed"] = int((raw_t - raw_b) * spd_cap)
            state["steer"] = int(raw_steer * MAX_STEER)
        else:
            spd = 0 if kb["brake"] else \
                  (spd_cap if kb["forward"] else 0) - (spd_cap if kb["reverse"] else 0)
            state["speed"] = spd
            state["steer"] = (MAX_STEER if kb["right"] else 0) - (MAX_STEER if kb["left"] else 0)

        # ════════════════════════════════════════════════════════════════════
        # DRAW — dark terminal layout
        # ════════════════════════════════════════════════════════════════════
        screen.fill(BG)

        spd = state["speed"]
        spr = state["steer"]
        speed_hist.append(spd)

        if joy is not None:
            pressed = [i for i in range(joy.get_numbuttons()) if joy.get_button(i)]
            axes    = [round(joy.get_axis(i), 2) for i in range(joy.get_numaxes())]
            print(f"\r[DBG] pressed={pressed}  axes={axes}      ", end="", flush=True)

        close_r, _, fs_r = draw_titlebar(screen, font_title, font_small, W, is_fullscreen)


        PANEL_Y   = 30
        RH        = 28
        HDR_Y     = PANEL_Y + 12
        SEP_Y     = PANEL_Y + 36
        ROW_START = PANEL_Y + 44

        mode    = SPEED_MODES[state["speed_mode"]]
        spd_col = ACCENT2 if spd > 0 else DANGER if spd < 0 else TEXT_DIM
        spr_col = ACCENT  if spr != 0 else TEXT_DIM
        st_col  = ACCENT2 if "Ready" in state["status"] else ACCENT3

        def draw_row(x, w, y, label, value, vc=TEXT):
            screen.blit(font.render(label, True, TEXT_DIM), (x + 10, y))
            vs = font.render(str(value), True, vc)
            screen.blit(vs, (x + w - 10 - vs.get_width(), y))

        # ── Speed header ──────────────────────────────────────────────────
        screen.blit(font.render(f"Speed  |  {mode['name']}", True, ACCENT), (10, HDR_Y))
        pygame.draw.line(screen, BORDER, (0, SEP_Y), (W, SEP_Y), 1)

        # ── Full-width chart ──────────────────────────────────────────────
        CH_Y, CH_H = ROW_START, 200
        CH_X, CH_W = 1, W - 2
        pygame.draw.rect(screen, BG2, (CH_X, CH_Y, CH_W, CH_H))
        mid_y = CH_Y + CH_H // 2
        pygame.draw.line(screen, PANEL2, (CH_X, mid_y), (CH_X + CH_W, mid_y), 1)
        if len(speed_hist) > 1:
            pts = []
            for i, v in enumerate(speed_hist):
                px = CH_X + 1 + int(i / (len(speed_hist) - 1) * (CH_W - 3))
                py = mid_y - int(v / MAX_SPEED_SPORT * (CH_H // 2 - 6))
                pts.append((px, py))
            pygame.draw.lines(screen, spd_col, False, pts, 2)
        mx_v = max(speed_hist) if speed_hist else 0
        mn_v = min(speed_hist) if speed_hist else 0
        screen.blit(font_small.render(f"{mx_v:+d}", True, TEXT_DIM), (CH_X + 4, CH_Y + 4))
        screen.blit(font_small.render(f"{mn_v:+d}", True, TEXT_DIM), (CH_X + 4, CH_Y + CH_H - 22))
        cur_surf = font_small.render(f"{spd:+d}", True, spd_col)
        screen.blit(cur_surf, (CH_X + CH_W - cur_surf.get_width() - 4, CH_Y + CH_H // 2 - 10))
        pygame.draw.rect(screen, BORDER, (CH_X, CH_Y, CH_W, CH_H), 1)

        # ── Hub info — two columns below chart ────────────────────────────
        HUB_Y = CH_Y + CH_H + 6
        pygame.draw.line(screen, BORDER, (0, CH_Y + CH_H + 3), (W, CH_Y + CH_H + 3), 1)
        # vertical centre divider
        pygame.draw.line(screen, BORDER, (W // 2, CH_Y + CH_H + 3), (W // 2, H - 36), 1)

        is_ctrl = state["input_mode"] == "controller"
        im_col  = ACCENT2 if is_ctrl else TEXT
        hn      = hub_name[:24] if len(hub_name) > 24 else hub_name
        pad_lbl = profile["label"][:20] if (is_ctrl and profile) else f"{len(joysticks)} connected"

        # Left column: hub status / identity
        status_txt = "Ready!" if "Ready" in state["status"] else state["status"][:18]
        draw_row(0,      W//2, HUB_Y,        "status",  status_txt,           st_col)
        draw_row(0,      W//2, HUB_Y + RH,   "name",    hn,                   TEXT)
        draw_row(0,      W//2, HUB_Y + RH*2, "address", hub_address,          TEXT_DIM)
        input_val = profile["label"][:20] if (is_ctrl and profile) else "keyboard"
        draw_row(0,      W//2, HUB_Y + RH*3, "input",         input_val,                  im_col)
        n_inputs = len(joysticks) + 1  # +1 for keyboard
        draw_row(0,      W//2, HUB_Y + RH*4, "input methods", f"{n_inputs} connected", TEXT_DIM)
        draw_row(0,      W//2, HUB_Y + RH*5, "speed",   f"{spd:+d}",          spd_col)
        draw_row(0,      W//2, HUB_Y + RH*6, "steer",   f"{spr:+d}",          spr_col)
        draw_row(0,      W//2, HUB_Y + RH*7, "lights",  f"0x{LIGHT_MODES[light_idx]:02X}", TEXT)

        # Right column: binds (keyboard or controller depending on active input)
        KB_TO_CTRL = {
            "forward": ("axis_throttle", "axis"),
            "reverse": ("axis_brake",    "axis"),
            "left":    ("axis_steer",    "axis"),
            "right":   ("axis_steer",    "axis"),
            "brake":   ("axis_brake",    "axis"),
            "lights":  ("btn_lights",    "button"),
            "slow":    ("btn_slow",      "button"),
        }
        binds_now = ctrl_binds if ctrl_binds else profile
        bind_actions = ["forward", "reverse", "left", "right", "brake", "lights", "slow"]
        for bi, ba in enumerate(bind_actions):
            if is_ctrl and binds_now and ba in KB_TO_CTRL:
                ctrl_key, ctrl_type = KB_TO_CTRL[ba]
                val = binds_now.get(ctrl_key)
                bind_val = (f"Axis {val}" if ctrl_type == "axis" else f"Btn {val}") if val is not None else "?"
                active = False
            else:
                bind_val = key_name(keybinds[ba])
                active = kb.get(ba, False)
            draw_row(W//2, W//2, HUB_Y + bi * RH, BIND_LABELS[ba][:20],
                     bind_val, ACCENT2 if active else TEXT)

        # Buttons
        pygame.draw.rect(screen, BG2,    light_btn)
        pygame.draw.rect(screen, BORDER, light_btn, 1)
        lbl = font_small.render("Cycle Lights", True, TEXT)
        screen.blit(lbl, (light_btn.centerx - lbl.get_width() // 2, light_btn.y + 3))
        pygame.draw.rect(screen, BG2,    keybind_btn)
        pygame.draw.rect(screen, ACCENT, keybind_btn, 1)
        lbl2 = font_small.render("Edit Keybinds", True, ACCENT)
        screen.blit(lbl2, (keybind_btn.centerx - lbl2.get_width() // 2, keybind_btn.y + 3))

        pygame.draw.rect(screen, BORDER, (0, 0, W, H), 1)

        pygame.display.flip()
    # Save config (keybinds + any controller binds already saved via editor)
    cfg["fullscreen"] = is_fullscreen
    save_config(cfg)
    pygame.quit()


# ── BLE ───────────────────────────────────────────────────────────────────────
async def pair_device(address):
    try:
        import winrt.windows.devices.bluetooth as bluetooth
        addr_int   = int(address.replace(":", ""), 16)
        ble_device = await bluetooth.BluetoothLEDevice.from_bluetooth_address_async(addr_int)
        if ble_device is None: return False
        pairing = ble_device.device_information.pairing
        if pairing.is_paired: return True
        result = await pairing.pair_async()
        return True
    except Exception as e:
        print(f"[BLE] Pairing skipped ({e})")
        return False

async def ble_loop():
    address = state["hub_address"]
    state["status"] = "Pairing..."
    await pair_device(address)
    state["status"] = "Connecting..."
    try:
        async with BleakClient(address, timeout=20) as client:
            state["status"] = "Calibrating..."
            cal1, cal2 = build_calibrate_commands()
            await client.write_gatt_char(CHAR_UUID, cal1, response=False)
            await asyncio.sleep(0.5)
            await client.write_gatt_char(CHAR_UUID, cal2, response=False)
            await asyncio.sleep(0.5)
            state["status"] = "Ready! Keep this window focused."
            while not state["quit"]:
                cmd = build_drive_command(state["speed"], state["steer"], state["lights"])
                try:
                    await client.write_gatt_char(CHAR_UUID, cmd, response=False)
                except Exception as e:
                    state["status"] = f"Error: {e}"
                await asyncio.sleep(0.05)
            await client.write_gatt_char(CHAR_UUID, build_drive_command(0,0,state["lights"]), response=False)
    except Exception as e:
        state["status"] = f"BLE Error: {e}"
        state["quit"] = True

# ── Entry ─────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    result = hub_selection_screen()
    if result is None:
        print("No hub selected. Exiting.")
        exit(0)

    hub_name, hub_address = result
    state["hub_address"] = hub_address
    state["hub_name"]    = hub_name

    def run_ble():
        asyncio.run(ble_loop())

    ble_thread = threading.Thread(target=run_ble, daemon=True)
    ble_thread.start()

    pygame_loop(hub_name, hub_address)
    ble_thread.join(timeout=3)
    print("Done.")
