"""Fake-Gameserver: eine Webseite, die so tut als waere sie ein Minecraft/Satisfactory Server.

Komplett stateless, Konfiguration nur ueber Env-Variablen.
"""
import hashlib
import html
import json
import math
import os
import socket
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

GAME = os.environ.get("GAME", "minecraft")
GAME_LABEL = os.environ.get("GAME_LABEL", "Minecraft (Java)")
GAME_VERSION = os.environ.get("GAME_VERSION", "1.21.4")
GAME_PORT = os.environ.get("GAME_PORT", "25565")
SERVER_NAME = os.environ.get("SERVER_NAME", "Unnamed")
MAX_SLOTS = int(os.environ.get("MAX_SLOTS", "10"))
RAM_MB = int(os.environ.get("RAM_MB", "2048"))
POD = os.environ.get("HOSTNAME", socket.gethostname())
STARTED = time.time()

MOTDS = {
    "minecraft": ["Creepers are friends, not food.", "Mining away, diamonds soon.", "Do not dig straight down."],
    "satisfactory": ["The factory must grow.", "Conveyor belts are love.", "Pioneers wanted, coffee included."],
}
THEMES = {
    "minecraft": {"bg": "#1f2d1a", "fg": "#e8f5d0", "accent": "#6bbf3a", "panel": "#2b3d24"},
    "satisfactory": {"bg": "#1d1a17", "fg": "#f7e9d8", "accent": "#f08a1c", "panel": "#2d2823"},
}


def fake_players():
    """Pseudo-Spielerzahl: Sinuswelle, abhaengig von Servername und Zeit."""
    seed = int(hashlib.md5(SERVER_NAME.encode()).hexdigest()[:6], 16) % 360
    wave = (math.sin(time.time() / 40 + seed) + 1) / 2
    return max(0, min(MAX_SLOTS, round(wave * MAX_SLOTS * 0.8)))


def status():
    return {
        "name": SERVER_NAME, "game": GAME_LABEL, "version": GAME_VERSION, "port": GAME_PORT,
        "players": fake_players(), "max_players": MAX_SLOTS, "ram_mb": RAM_MB,
        "uptime_s": int(time.time() - STARTED), "pod": POD,
    }


def page(host):
    s = status()
    t = THEMES.get(GAME, THEMES["minecraft"])
    motd = MOTDS.get(GAME, MOTDS["minecraft"])[int(hashlib.md5(SERVER_NAME.encode()).hexdigest()[:4], 16) % 3]
    esc = html.escape
    pct = s["players"] * 100 // max(1, s["max_players"])
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{esc(SERVER_NAME)} | {esc(GAME_LABEL)}</title>
<style>
body{{margin:0;font-family:ui-monospace,Menlo,Consolas,monospace;background:{t['bg']};color:{t['fg']};
 min-height:100vh;display:grid;place-items:center}}
main{{width:min(560px,92vw);background:{t['panel']};border:3px solid {t['accent']};padding:28px}}
h1{{margin:0 0 4px;font-size:1.6rem}} .sub{{opacity:.7;margin-bottom:20px}}
.row{{display:flex;justify-content:space-between;padding:8px 0;border-bottom:1px dashed #ffffff30}}
.accent{{color:{t['accent']}}} .bar{{height:12px;background:#00000050;margin-top:6px}}
.bar i{{display:block;height:100%;background:{t['accent']};width:{pct}%}}
.dot{{display:inline-block;width:10px;height:10px;background:{t['accent']};border-radius:50%;margin-right:6px}}
footer{{margin-top:18px;font-size:.78rem;opacity:.6}}
</style></head><body><main>
<h1><span class="dot"></span>{esc(SERVER_NAME)}</h1>
<div class="sub">{esc(GAME_LABEL)} {esc(GAME_VERSION)} &middot; {esc(motd)}</div>
<div class="row"><span>Status</span><span class="accent">ONLINE (fake)</span></div>
<div class="row"><span>Address</span><span>{esc(host)}:{esc(GAME_PORT)}</span></div>
<div class="row"><span>Players</span><span>{s['players']} / {s['max_players']}</span></div>
<div class="bar"><i></i></div>
<div class="row"><span>RAM plan</span><span>{s['ram_mb']} MB</span></div>
<div class="row"><span>Uptime</span><span>{s['uptime_s']} s</span></div>
<div class="row"><span>Served by pod</span><span>{esc(POD)}</span></div>
<footer>Not a real game server. Just a stateless web page pretending. Refresh to watch the players move.</footer>
</main></body></html>"""


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype):
        data = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):  # noqa: N802
        if self.path == "/healthz":
            self._send(200, "ok", "text/plain")
        elif self.path == "/status.json":
            self._send(200, json.dumps(status()), "application/json")
        elif self.path in ("/", "/index.html"):
            host = (self.headers.get("Host") or "localhost").split(":")[0]
            self._send(200, page(host), "text/html; charset=utf-8")
        else:
            self._send(404, "not found", "text/plain")

    def log_message(self, fmt, *args):  # Healthcheck-Spam vermeiden
        if not self.path.startswith("/healthz"):
            super().log_message(fmt, *args)


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", int(os.environ.get("PORT", "8080"))), Handler).serve_forever()
