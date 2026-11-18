"""Fake game server: a stateless web page pretending to be a game server.

Configured entirely via environment variables injected by the Kubernetes worker.
"""
import hashlib
import html
import json
import math
import os
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

GAME = os.environ.get("GAME", "minecraft")
GAME_LABEL = os.environ.get("GAME_LABEL", "Minecraft (Java)")
GAME_VERSION = os.environ.get("GAME_VERSION", "1.21.4")
GAME_PORT = os.environ.get("GAME_PORT", "25565")
SERVER_NAME = os.environ.get("SERVER_NAME", "Unnamed")
MOTD_ENV = os.environ.get("MOTD", "")
MAX_SLOTS = int(os.environ.get("MAX_SLOTS", "10"))
RAM_MB = int(os.environ.get("RAM_MB", "2048"))
POD = os.environ.get("HOSTNAME", socket.gethostname())
STARTED = time.time()
_stress_until = 0.0
_extra_logs = []


def _run_command(cmd):
    cmd = cmd.strip()
    if not cmd:
        return "Empty command."
    _extra_logs.append(f"> {cmd}")
    low = cmd.lower()
    if low == "/help":
        reply = "[Console]: Commands: /say <msg>, /time set day, /save-all, /players, /status"
    elif low.startswith("/say "):
        reply = f"[Server]: {cmd[5:]}"
    elif low.startswith("/time "):
        reply = f"[World]: Time updated ({cmd[6:]})."
    elif low == "/save-all":
        reply = "[Save]: Saved world state to memory (14ms)."
    elif low == "/players":
        reply = f"[Players]: {fake_players()} / {MAX_SLOTS} simulated players online."
    elif low == "/status":
        reply = f"[Status]: Pod {POD} healthy, {RAM_MB} MB allocated."
    else:
        reply = f"[Console]: Unknown command '{cmd}'. Type /help for options."
    _extra_logs.append(reply)
    del _extra_logs[:-25]
    return reply


def _burn_cpu(seconds=20):
    global _stress_until
    _stress_until = max(_stress_until, time.time() + seconds)
    while time.time() < _stress_until:
        sum(i * i for i in range(4000))
        time.sleep(0.002)

MOTDS = {
    "minecraft": ["Creepers are friends, not food.", "Mining away, diamonds soon.", "Do not dig straight down."],
    "satisfactory": ["The factory must grow.", "Conveyor belts are love.", "Pioneers wanted, coffee included."],
    "valheim": ["Odin awaits in the Tenth World.", "The bees are happy.", "Sail into the mist."],
    "terraria": ["Dig, fight, explore, build!", "A goblin army is approaching!", "Watch out for boulders."],
}
THEMES = {
    "minecraft": {"bg": "#0b140d", "fg": "#ecfdf0", "accent": "#5ee062", "panel": "#121f15"},
    "satisfactory": {"bg": "#140f0a", "fg": "#fef3e2", "accent": "#f59e0b", "panel": "#1f1710"},
    "valheim": {"bg": "#09121a", "fg": "#e6f4fe", "accent": "#38bdf8", "panel": "#101e2b"},
    "terraria": {"bg": "#130c1a", "fg": "#f5ecfe", "accent": "#c084fc", "panel": "#1d1329"},
}
CONSOLE_LINES = {
    "minecraft": [
        "[Server thread/INFO]: Preparing spawn area: 100%",
        "[Server thread/INFO]: Done (2.14s)! For help, type \"help\"",
        "[User Authenticator/INFO]: UUID of player Alex is 8667ba71-b85a-4004-af54-457a9734eed7",
        "[Server thread/INFO]: Alex joined the game",
        "[Server thread/INFO]: Automatic world save completed in 18ms",
    ],
    "satisfactory": [
        "[LogGameState]: Dedicated server bound on 0.0.0.0:7777",
        "[LogFactory]: Conveyor Mk.5 throughput nominal (780 items/min)",
        "[LogNet]: Pioneer connected (SteamNetDriver)",
        "[LogSave]: Auto-saved session 'FICSIT_Alpha' in 42ms",
        "[LogPower]: Grid #1 steady at 4,200 MW",
    ],
    "valheim": [
        "[Valheim]: Loaded world seed 'Kubeheim' (Meadows)",
        "[ZSteamSocket]: Listening on port 2456",
        "[ZoneSystem]: Generated 128 active zones",
        "[Net]: Viking 'Erik' joined the longhouse",
        "[Save]: World saved in 31ms",
    ],
    "terraria": [
        "[tShock]: Terraria Server v1.4.4.9 initialized",
        "[World]: Settling liquids... 100%",
        "[Net]: Listening on port 7777",
        "[Player]: Guide has arrived!",
        "[Save]: World backup complete",
    ],
}


def _seed():
    return int(hashlib.md5(SERVER_NAME.encode()).hexdigest()[:6], 16) % 360


def fake_players():
    """Simulated player count using a sine wave seeded by server name and time."""
    wave = (math.sin(time.time() / 35 + _seed()) + 1) / 2
    return max(0, min(MAX_SLOTS, round(wave * MAX_SLOTS * 0.8)))


def status():
    players = fake_players()
    load_wave = (math.sin(time.time() / 12 + _seed()) + 1) / 2
    cpu_pct = round(8 + (players / max(1, MAX_SLOTS)) * 42 + load_wave * 11, 1)
    ram_used_mb = round(RAM_MB * (0.28 + (players / max(1, MAX_SLOTS)) * 0.45 + load_wave * 0.05))
    tps = round(20.0 - load_wave * 0.3, 1) if GAME == "minecraft" else round(60.0 - load_wave * 0.8, 1)
    motd = MOTD_ENV or MOTDS.get(GAME, MOTDS["minecraft"])[_seed() % 3]
    return {
        "name": SERVER_NAME,
        "game": GAME_LABEL,
        "version": GAME_VERSION,
        "port": GAME_PORT,
        "motd": motd,
        "players": players,
        "max_players": MAX_SLOTS,
        "ram_mb": RAM_MB,
        "ram_used_mb": ram_used_mb,
        "cpu_pct": cpu_pct,
        "tps": tps,
        "uptime_s": int(time.time() - STARTED),
        "pod": POD,
        "logs": CONSOLE_LINES.get(GAME, CONSOLE_LINES["minecraft"]) + _extra_logs,
    }


def page(host):
    s = status()
    t = THEMES.get(GAME, THEMES["minecraft"])
    esc = html.escape
    pct = s["players"] * 100 // max(1, s["max_players"])
    ram_pct = s["ram_used_mb"] * 100 // max(1, s["ram_mb"])
    addr = f"{host}:{GAME_PORT}"
    logs_html = "".join(f"<div>{esc(line)}</div>" for line in s["logs"])
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{esc(SERVER_NAME)} | {esc(GAME_LABEL)}</title>
<style>
:root{{--bg:{t['bg']};--fg:{t['fg']};--accent:{t['accent']};--panel:{t['panel']}}}
*{{box-sizing:border-box}}
body{{margin:0;font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;background:radial-gradient(circle at top, color-mix(in srgb, var(--accent) 14%, var(--bg)), var(--bg) 60%);color:var(--fg);min-height:100vh;display:grid;place-items:center;padding:24px}}
main{{width:min(680px,100%);background:var(--panel);border:1px solid color-mix(in srgb, var(--accent) 35%, #ffffff18);border-radius:16px;padding:28px;box-shadow:0 24px 60px #00000080}}
.top{{display:flex;justify-content:space-between;align-items:flex-start;gap:12px;flex-wrap:wrap;margin-bottom:20px}}
h1{{margin:0;font-size:1.65rem;display:flex;align-items:center;gap:10px}}
.dot{{width:11px;height:11px;background:var(--accent);border-radius:50%;box-shadow:0 0 12px var(--accent);animation:pulse 2s infinite}}
@keyframes pulse{{50%{{opacity:.45}}}}
.pill{{font-size:.75rem;font-weight:700;padding:4px 10px;border-radius:99px;background:color-mix(in srgb, var(--accent) 18%, transparent);color:var(--accent);border:1px solid color-mix(in srgb, var(--accent) 40%, transparent)}}
.sub{{opacity:.78;margin-top:4px;font-size:.95rem}}
.addr-box{{display:flex;justify-content:space-between;align-items:center;gap:10px;background:#00000040;border:1px solid #ffffff1a;border-radius:10px;padding:10px 14px;margin-bottom:18px;font-family:ui-monospace,Menlo,Consolas,monospace;font-size:.92rem}}
button{{cursor:pointer;font:inherit;font-size:.82rem;font-weight:600;background:var(--accent);color:#081008;border:0;border-radius:7px;padding:6px 12px}}
button:hover{{filter:brightness(1.08)}}
.stats{{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:12px;margin-bottom:18px}}
.stat{{background:#00000033;border:1px solid #ffffff14;border-radius:10px;padding:12px}}
.stat small{{display:block;opacity:.65;font-size:.76rem;text-transform:uppercase;letter-spacing:.04em}}
.stat b{{display:block;font-size:1.25rem;margin-top:4px}}
.bar{{height:7px;background:#ffffff14;border-radius:99px;overflow:hidden;margin-top:8px}}
.bar i{{display:block;height:100%;background:var(--accent);transition:width .4s ease}}
.console{{background:#07090c;border:1px solid #ffffff14;border-radius:10px;padding:12px 14px;font-family:ui-monospace,Menlo,Consolas,monospace;font-size:.8rem;line-height:1.55;color:#b8c4d0;max-height:150px;overflow:auto}}
.meta{{display:flex;justify-content:space-between;flex-wrap:wrap;gap:8px;margin-top:14px;font-size:.78rem;opacity:.65}}
</style></head><body><main>
<div class="top">
  <div>
    <h1><span class="dot"></span>{esc(SERVER_NAME)}</h1>
    <div class="sub">{esc(GAME_LABEL)} {esc(GAME_VERSION)} &middot; <span id="motd">{esc(s['motd'])}</span></div>
  </div>
  <span class="pill">SIMULATED POD &bull; ONLINE</span>
</div>

<div class="addr-box">
  <span id="addr">{esc(addr)}</span>
  <div style="display:flex;gap:8px">
    <button id="stress-btn" type="button" style="background:#ffffff1a;color:var(--fg)">Simulate CPU Load</button>
    <button id="copy-btn" type="button">Copy Address</button>
  </div>
</div>

<div class="stats">
  <div class="stat">
    <small>Players</small>
    <b id="players">{s['players']} / {s['max_players']}</b>
    <div class="bar"><i id="players-bar" style="width:{pct}%"></i></div>
  </div>
  <div class="stat">
    <small>Memory</small>
    <b id="ram">{s['ram_used_mb']} / {s['ram_mb']} MB</b>
    <div class="bar"><i id="ram-bar" style="width:{ram_pct}%"></i></div>
  </div>
  <div class="stat">
    <small>CPU / Tick</small>
    <b id="cpu">{s['cpu_pct']}% &middot; {s['tps']}</b>
  </div>
  <div class="stat">
    <small>Uptime</small>
    <b id="uptime">{s['uptime_s']}s</b>
  </div>
</div>

<div class="console" id="console">{logs_html}</div>
<form id="cmd-form" style="display:flex;gap:8px;margin-top:10px">
  <input id="cmd-input" placeholder="Type a command (e.g. /help, /say hello, /save-all)" style="flex:1;background:#07090c;border:1px solid #ffffff22;border-radius:8px;padding:8px 12px;color:var(--fg);font-family:ui-monospace,Menlo,Consolas,monospace;font-size:.82rem">
  <button type="submit">Send</button>
</form>

<div class="meta">
  <span>Pod: <code id="pod">{esc(POD)}</code></span>
  <span>Stateless FreeKube demo server &middot; auto-refreshing live</span>
</div>
</main>
<script>
const fmtUp = s => {{
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = s % 60;
  return h ? `${{h}}h ${{m}}m ${{sec}}s` : m ? `${{m}}m ${{sec}}s` : `${{sec}}s`;
}};
document.getElementById('stress-btn').onclick = async () => {{
  const btn = document.getElementById('stress-btn');
  btn.disabled = true;
  btn.textContent = 'Generating load (20s)...';
  try {{ await fetch('/stress', {{method: 'POST'}}); }} catch (e) {{}}
  setTimeout(() => {{ btn.disabled = false; btn.textContent = 'Simulate CPU Load'; }}, 20000);
}};
document.getElementById('copy-btn').onclick = async () => {{
  try {{ await navigator.clipboard.writeText(document.getElementById('addr').textContent); }} catch (e) {{}}
  const btn = document.getElementById('copy-btn');
  btn.textContent = 'Copied!';
  setTimeout(() => btn.textContent = 'Copy Address', 1500);
}};
document.getElementById('cmd-form').onsubmit = async e => {{
  e.preventDefault();
  const inp = document.getElementById('cmd-input');
  const cmd = inp.value.trim();
  if (!cmd) return;
  inp.value = '';
  try {{
    await fetch('/command', {{method: 'POST', headers: {{'Content-Type': 'application/json'}}, body: JSON.stringify({{command: cmd}})}});
    const r = await fetch('/status.json', {{cache: 'no-store'}});
    if (r.ok) {{
      const d = await r.json();
      const con = document.getElementById('console');
      con.innerHTML = d.logs.map(l => `<div>${{l.replace(/[<>&]/g, c => ({{'<':'&lt;','>':'&gt;','&':'&amp;'}}[c]))}}</div>`).join('');
      con.scrollTop = con.scrollHeight;
    }}
  }} catch (err) {{}}
}};
setInterval(async () => {{
  try {{
    const r = await fetch('/status.json', {{cache: 'no-store'}});
    if (!r.ok) return;
    const d = await r.json();
    document.getElementById('players').textContent = `${{d.players}} / ${{d.max_players}}`;
    document.getElementById('players-bar').style.width = `${{Math.round(d.players * 100 / Math.max(1, d.max_players))}}%`;
    document.getElementById('ram').textContent = `${{d.ram_used_mb}} / ${{d.ram_mb}} MB`;
    document.getElementById('ram-bar').style.width = `${{Math.round(d.ram_used_mb * 100 / Math.max(1, d.ram_mb))}}%`;
    document.getElementById('cpu').textContent = `${{d.cpu_pct}}% · ${{d.tps}}`;
    document.getElementById('uptime').textContent = fmtUp(d.uptime_s);
    document.getElementById('motd').textContent = d.motd;
    const con = document.getElementById('console');
    con.innerHTML = d.logs.map(l => `<div>${{l.replace(/[<>&]/g, c => ({{'<':'&lt;','>':'&gt;','&':'&amp;'}}[c]))}}</div>`).join('');
  }} catch (e) {{}}
}}, 3000);
</script>
</body></html>"""


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
        elif self.path == "/metrics":
            s = status()
            body = (
                f"freekube_gameserver_players {s['players']}\n"
                f"freekube_gameserver_max_players {s['max_players']}\n"
                f"freekube_gameserver_ram_used_mb {s['ram_used_mb']}\n"
                f"freekube_gameserver_uptime_seconds {s['uptime_s']}\n"
            )
            self._send(200, body, "text/plain; version=0.0.4; charset=utf-8")
        elif self.path in ("/", "/index.html"):
            host = (self.headers.get("Host") or "localhost").split(":")[0]
            self._send(200, page(host), "text/html; charset=utf-8")
        else:
            self._send(404, "not found", "text/plain")

    def do_POST(self):  # noqa: N802
        if self.path == "/stress":
            threading.Thread(target=_burn_cpu, args=(20,), daemon=True).start()
            self._send(202, json.dumps({"ok": True, "duration_s": 20}), "application/json")
        elif self.path == "/command":
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(min(length, 2048))
            try:
                cmd = json.loads(raw.decode()).get("command", "")
            except Exception:
                cmd = ""
            reply = _run_command(str(cmd)[:120])
            self._send(200, json.dumps({"reply": reply}), "application/json")
        else:
            self._send(404, "not found", "text/plain")

    def log_message(self, fmt, *args):  # avoid healthcheck log spam
        if not self.path.startswith("/healthz"):
            super().log_message(fmt, *args)


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", int(os.environ.get("PORT", "8080"))), Handler).serve_forever()
