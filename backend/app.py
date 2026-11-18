"""FreeKube API. Stateless: sessions, cache, and queue live in Redis; data lives in Postgres."""
import json
import re
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from functools import wraps

import psycopg2
import psycopg2.errors
import redis
from flask import Flask, g, jsonify, make_response, request
from werkzeug.security import check_password_hash, generate_password_hash

import common
from common import cached, db, enqueue, ensure_schema, invalidate, log, q, rds, record_event, serialize

app = Flask(__name__)

SESSION_TTL = 7 * 24 * 3600
EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,255}\.[^@\s]{2,}$")
NAME_RE = re.compile(r"^[A-Za-z0-9 _-]{3,32}$")
_schema_ready = False


# --------------------------------------------------------------- Helpers
def err(msg, code=400):
    return jsonify({"error": msg}), code


def client_ip():
    fwd = request.headers.get("X-Forwarded-For", "")
    return fwd.split(",")[0].strip() or request.remote_addr or "?"


def rate_limit(key, limit, window):
    """Fixed-window rate limit in Redis, shared across all API replicas."""
    try:
        n = rds().incr(key)
        if n == 1:
            rds().expire(key, window)
        return n <= limit
    except redis.RedisError:
        return True  # fail open so a Redis hiccup doesn't lock everyone out


def servers_key(uid):
    return f"servers:{uid}"


def start_session(user):
    token = secrets.token_urlsafe(32)
    rds().setex(f"sess:{token}", SESSION_TTL, json.dumps({"id": user["id"], "email": user["email"]}))
    resp = make_response(jsonify({"id": user["id"], "email": user["email"]}))
    resp.set_cookie(
        "sid",
        token,
        max_age=SESSION_TTL,
        httponly=True,
        samesite="Lax",
        secure=request.headers.get("X-Forwarded-Proto") == "https",
    )
    return resp


def login_required(fn):
    @wraps(fn)
    def wrapper(*a, **kw):
        token = request.cookies.get("sid")
        data = None
        if token:
            try:
                data = rds().get(f"sess:{token}")
            except redis.RedisError:
                return err("session store unavailable", 503)
        if not data:
            return err("not logged in", 401)
        g.user = json.loads(data)
        g.token = token
        return fn(*a, **kw)

    return wrapper


@app.before_request
def before():
    global _schema_ready
    if not _schema_ready and request.path.startswith("/api/"):
        ensure_schema()
        _schema_ready = True
    # Simple CSRF guard: cookie is SameSite=Lax, and mutating calls require this custom header
    if request.method in ("POST", "PUT", "PATCH", "DELETE") and request.headers.get("X-Requested-With") != "freekube":
        return err("missing X-Requested-With header", 403)


# ---------------------------------------------------------------- Health
@app.get("/healthz")
def healthz():
    return "ok"


@app.get("/readyz")
def readyz():
    try:
        q("SELECT 1")
        rds().ping()
    except Exception as e:  # noqa
        log.warning("not ready: %s", e)
        return "not ready", 503
    return "ready"


# ---------------------------------------------------------------- Public
@app.get("/api/catalog")
def catalog():
    return jsonify(cached("catalog", 300, common.public_catalog))


@app.get("/api/stats")
def stats():
    def compute():
        r = q(
            "SELECT (SELECT count(*) FROM users) AS users, "
            "(SELECT count(*) FROM servers WHERE status='running') AS running, "
            "(SELECT count(*) FROM servers) AS total_servers"
        )[0]
        by_game_rows = q("SELECT game, count(*) AS cnt FROM servers WHERE status='running' GROUP BY game")
        by_game = {row["game"]: row["cnt"] for row in by_game_rows}
        return {
            "users": r["users"],
            "running": r["running"],
            "total_servers": r["total_servers"],
            "by_game": by_game,
        }

    return jsonify(cached("stats", 15, compute))


# ------------------------------------------------------------------ Auth
@app.post("/api/register")
def register():
    body = request.get_json(silent=True) or {}
    email = str(body.get("email", "")).strip().lower()
    password = str(body.get("password", ""))
    if not EMAIL_RE.match(email):
        return err("invalid email")
    if len(password) < 8 or len(password) > 128:
        return err("password must be 8-128 characters")
    if not rate_limit(f"rl:register:{client_ip()}", 5, 3600):
        return err("too many registrations, try later", 429)
    try:
        rows = q(
            "INSERT INTO users(email, pw_hash) VALUES (%s, %s) RETURNING id, email",
            (email, generate_password_hash(password)),
        )
    except psycopg2.errors.UniqueViolation:
        return err("email already registered", 409)
    invalidate("stats")
    return start_session(rows[0])


@app.post("/api/login")
def login():
    body = request.get_json(silent=True) or {}
    email = str(body.get("email", "")).strip().lower()
    password = str(body.get("password", ""))
    if not rate_limit(f"rl:login:{client_ip()}:{email}", 10, 60):
        return err("too many attempts, wait a minute", 429)
    rows = q("SELECT id, email, pw_hash FROM users WHERE email=%s", (email,))
    if not rows or not check_password_hash(rows[0]["pw_hash"], password):
        return err("wrong email or password", 401)
    return start_session(rows[0])


@app.post("/api/logout")
@login_required
def logout():
    rds().delete(f"sess:{g.token}")
    resp = make_response(jsonify({"ok": True}))
    resp.delete_cookie("sid")
    return resp


@app.get("/api/me")
@login_required
def me():
    return jsonify({**g.user, "limits": common.public_catalog()["limits"]})


# --------------------------------------------------------------- Servers
@app.get("/api/servers")
@login_required
def list_servers():
    uid = g.user["id"]

    def compute():
        rows = q(
            "SELECT id, name, game, plan, status, k8s_name, url, motd, autoscale, min_replicas, max_replicas, "
            "error, created_at, expires_at FROM servers WHERE user_id=%s ORDER BY created_at DESC",
            (uid,),
        )
        return [serialize(r) for r in rows]

    # Short TTL + explicit invalidation by API and worker on every change
    return jsonify(cached(servers_key(uid), 10, compute))


@app.post("/api/servers")
@login_required
def create_server():
    uid = g.user["id"]
    body = request.get_json(silent=True) or {}
    name = str(body.get("name", "")).strip()
    game = body.get("game")
    plan = body.get("plan")
    motd = str(body.get("motd", "")).strip()[:120]
    autoscale = bool(body.get("autoscale", False))
    try:
        min_rep = max(1, min(2, int(body.get("min_replicas", 1))))
        max_rep = max(min_rep, min(5, int(body.get("max_replicas", 3))))
    except (TypeError, ValueError):
        return err("invalid replica bounds")
    if not NAME_RE.match(name):
        return err("name: 3-32 chars, letters, digits, space, _ or -")
    if game not in common.GAMES:
        return err("unknown game")
    if plan not in common.PLANS:
        return err("unknown plan")
    if not rate_limit(f"rl:create:{uid}", 10, 3600):
        return err("slow down, max 10 creates per hour", 429)

    if not motd:
        motd = common.GAMES[game]["default_motd"]

    sid = uuid.uuid4()
    k8s_name = "gs-" + sid.hex[:12]
    expires = datetime.now(timezone.utc) + timedelta(hours=common.SERVER_TTL_HOURS)
    with db() as cur:
        # Lock user row so two concurrent requests cannot bypass the server limit
        cur.execute("SELECT id FROM users WHERE id=%s FOR UPDATE", (uid,))
        if not cur.fetchone():
            return err("user vanished", 401)
        cur.execute("SELECT count(*) AS n FROM servers WHERE user_id=%s", (uid,))
        if cur.fetchone()["n"] >= common.MAX_SERVERS_PER_USER:
            return err(f"limit reached: max {common.MAX_SERVERS_PER_USER} servers on the free tier", 409)
        cur.execute(
            "INSERT INTO servers(id, user_id, name, game, plan, status, k8s_name, motd, autoscale, min_replicas, max_replicas, expires_at) "
            "VALUES (%s,%s,%s,%s,%s,'provisioning',%s,%s,%s,%s,%s,%s)",
            (str(sid), uid, name, game, plan, k8s_name, motd, autoscale, min_rep, max_rep, expires),
        )
    scale_note = f" with HPA ({min_rep}–{max_rep} pods)" if autoscale else ""
    record_event(sid, "created", f"Queued {common.GAMES[game]['label']} ({common.PLANS[plan]['label']}) server '{name}'{scale_note}.")
    invalidate(servers_key(uid), "stats")
    enqueue("create", sid)
    return jsonify({"id": str(sid), "status": "provisioning"}), 202


def _owned(server_id):
    try:
        uuid.UUID(server_id)
    except ValueError:
        return None
    rows = q(
        "SELECT id, name, motd, autoscale, min_replicas, max_replicas, status FROM servers WHERE id=%s AND user_id=%s",
        (server_id, g.user["id"]),
    )
    return rows[0] if rows else None


@app.patch("/api/servers/<server_id>")
@login_required
def update_server(server_id):
    s = _owned(server_id)
    if not s:
        return err("not found", 404)
    if s["status"] == "deleting":
        return err("server is being deleted", 409)

    body = request.get_json(silent=True) or {}
    name = str(body.get("name", s["name"])).strip()
    motd = str(body.get("motd", s.get("motd") or "")).strip()[:120]
    autoscale = bool(body.get("autoscale", s.get("autoscale", False)))
    try:
        min_rep = max(1, min(2, int(body.get("min_replicas", s.get("min_replicas", 1)))))
        max_rep = max(min_rep, min(5, int(body.get("max_replicas", s.get("max_replicas", 3)))))
    except (TypeError, ValueError):
        return err("invalid replica bounds")
    if not NAME_RE.match(name):
        return err("name: 3-32 chars, letters, digits, space, _ or -")

    text_changed = (name != s["name"]) or (motd != (s.get("motd") or ""))
    hpa_changed = (
        autoscale != bool(s.get("autoscale", False))
        or min_rep != int(s.get("min_replicas", 1))
        or max_rep != int(s.get("max_replicas", 3))
    )

    q(
        "UPDATE servers SET name=%s, motd=%s, autoscale=%s, min_replicas=%s, max_replicas=%s, updated_at=now() WHERE id=%s",
        (name, motd, autoscale, min_rep, max_rep, server_id),
    )
    record_event(server_id, "updated", f"Updated settings (name='{name}', autoscale={autoscale}).")
    invalidate(servers_key(g.user["id"]))
    if s["status"] == "running":
        if text_changed:
            enqueue("restart", server_id)
        if hpa_changed:
            enqueue("autoscale", server_id)
    return jsonify({"ok": True, "name": name, "motd": motd, "autoscale": autoscale, "min_replicas": min_rep, "max_replicas": max_rep})


@app.post("/api/servers/<server_id>/restart")
@login_required
def restart_server(server_id):
    s = _owned(server_id)
    if not s:
        return err("not found", 404)
    if s["status"] != "running":
        return err("only running servers can be restarted", 409)
    if not rate_limit(f"rl:restart:{g.user['id']}", 12, 300):
        return err("too many restarts, wait a moment", 429)
    enqueue("restart", server_id)
    record_event(server_id, "restart_requested", "Rolling pod restart requested.")
    return jsonify({"ok": True}), 202


@app.get("/api/servers/<server_id>/events")
@login_required
def server_events(server_id):
    if not _owned(server_id):
        return err("not found", 404)
    rows = q(
        "SELECT id, event_type, message, created_at FROM server_events "
        "WHERE server_id=%s ORDER BY created_at DESC LIMIT 20",
        (server_id,),
    )
    return jsonify([serialize(r) for r in rows])


@app.delete("/api/servers/<server_id>")
@login_required
def delete_server(server_id):
    if not _owned(server_id):
        return err("not found", 404)
    q("UPDATE servers SET status='deleting', updated_at=now() WHERE id=%s", (server_id,))
    record_event(server_id, "deleting", "Deletion requested.")
    invalidate(servers_key(g.user["id"]), "stats")
    enqueue("delete", server_id)
    return jsonify({"status": "deleting"}), 202


@app.post("/api/servers/<server_id>/renew")
@login_required
def renew_server(server_id):
    s = _owned(server_id)
    if not s:
        return err("not found", 404)
    if s["status"] == "deleting":
        return err("server is being deleted", 409)
    q(
        "UPDATE servers SET expires_at = now() + make_interval(hours => %s), updated_at=now() WHERE id=%s",
        (common.SERVER_TTL_HOURS, server_id),
    )
    record_event(server_id, "renewed", f"Extended lifetime by {common.SERVER_TTL_HOURS}h.")
    invalidate(servers_key(g.user["id"]))
    return jsonify({"ok": True})
