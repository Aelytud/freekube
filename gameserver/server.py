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
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Anton&family=Geist+Mono:wght@400;500&family=Geist:wght@400;500;600&display=swap">
<style>
:root{{--bg:#0a0a0a;--surface:#111111;--over:#1f1f1f;--line:#262626;--fg:#f4efe8;--muted:#a1a1aa;--accent:#3ddc84}}
*{{box-sizing:border-box}}
body{{margin:0;font-family:"Geist",system-ui,sans-serif;background:var(--bg);color:var(--fg);min-height:100vh;padding:24px;-webkit-font-smoothing:antialiased}}
main{{width:min(780px,100%);margin:0 auto;background:var(--surface);padding:16px;display:flex;flex-direction:column;gap:2px}}
.top{{display:flex;justify-content:space-between;align-items:center;gap:12px;flex-wrap:wrap;padding:18px 20px;background:var(--bg)}}
h1{{margin:0;font:400 clamp(26px,3.5vw,38px)/1 "Anton",system-ui,sans-serif;letter-spacing:.02em;text-transform:uppercase}}
.state{{display:inline-flex;align-items:center;gap:8px;font:500 12px/1 "Geist Mono",ui-monospace,monospace;color:var(--fg)}}
.state i{{width:8px;height:8px;border-radius:999px;background:var(--accent)}}
.sub{{color:var(--muted);margin-top:6px;font:400 13px/18px "Geist Mono",ui-monospace,monospace}}
.addr-box{{display:flex;justify-content:space-between;align-items:center;gap:10px;flex-wrap:wrap;background:var(--bg);padding:12px 20px;font:400 13px/1 "Geist Mono",ui-monospace,monospace}}
button{{cursor:pointer;font:500 12px/1 "Geist",system-ui,sans-serif;background:var(--surface);color:var(--fg);border:1px solid var(--line);border-radius:0;height:32px;padding:0 14px;transition:background-color .15s,border-color .15s}}
button:hover:not(:disabled){{background:var(--over);border-color:#6a6a72}}
button.primary{{background:var(--fg);color:#0a0a0b;border-color:var(--fg)}}
button.primary:hover:not(:disabled){{background:#e4ded5;border-color:#e4ded5}}
button:disabled{{opacity:.45;cursor:not-allowed}}
.stats{{display:grid;grid-template-columns:repeat(4,1fr);gap:2px}}
.stat{{padding:14px 18px;background:var(--bg)}}
.stat small{{display:block;color:var(--muted);font:500 11px/16px "Geist Mono",ui-monospace,monospace;text-transform:uppercase;letter-spacing:.06em}}
.stat b{{display:block;font:500 20px/26px "Geist Mono",ui-monospace,monospace;margin-top:4px;font-variant-numeric:tabular-nums}}
.bar{{height:4px;background:var(--over);margin-top:10px}}
.bar i{{display:block;height:100%;background:var(--fg);transition:width .3s ease}}
.console-wrap{{background:var(--bg);padding:16px 20px}}
.console{{font:400 12px/21px "Geist Mono",ui-monospace,monospace;color:var(--muted);height:190px;overflow:auto}}
.console div:last-child{{color:var(--fg)}}
.meta{{display:flex;justify-content:space-between;flex-wrap:wrap;gap:8px;padding:12px 20px;font:400 12px/16px "Geist Mono",ui-monospace,monospace;color:var(--muted);background:var(--bg)}}
@media(max-width:640px){{.stats{{grid-template-columns:repeat(2,1fr)}}}}
</style></head><body><main>
<div class="top">
  <div>
    <h1>{esc(SERVER_NAME)}</h1>
    <div class="sub">{esc(GAME_LABEL)} {esc(GAME_VERSION)} &middot; <span id="motd">{esc(s['motd'])}</span></div>
  </div>
  <span class="state"><i></i>running</span>
</div>

<div class="addr-box">
  <span id="addr">{esc(addr)}</span>
  <div style="display:flex;gap:6px">
    <button id="stress-btn" type="button">Simulate CPU Load</button>
    <button id="copy-btn" class="primary" type="button">Copy Address</button>
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

<div class="console-wrap">
  <div class="console" id="console">{logs_html}</div>
  <form id="cmd-form" style="display:flex;gap:6px;margin-top:10px;padding-top:10px;border-top:1px solid var(--line)">
    <input id="cmd-input" placeholder="/help, /say hello, /save-all" style="flex:1;background:var(--surface);border:1px solid var(--line);border-radius:0;padding:0 10px;height:32px;color:var(--fg);font:400 12px/1 'Geist Mono',ui-monospace,monospace">
    <button type="submit">Run</button>
  </form>
</div>

<div class="meta">
  <span>pod: <code id="pod">{esc(POD)}</code></span>
  <span>freekube instance &middot; 3s poll</span>
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
