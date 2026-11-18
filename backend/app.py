"""FreeKube API. Stateless: Sessions, Cache und Queue liegen in Redis, Daten in Postgres."""
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
from common import cached, db, enqueue, ensure_schema, invalidate, log, q, rds, serialize

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
    """Fixed-window Rate Limit in Redis. Zaehler gilt fuer alle API-Replicas gemeinsam."""
    try:
        n = rds().incr(key)
        if n == 1:
            rds().expire(key, window)
        return n <= limit
    except redis.RedisError:
        return True  # lieber durchlassen als alle aussperren


def servers_key(uid):
    return f"servers:{uid}"


def start_session(user):
    token = secrets.token_urlsafe(32)
    rds().setex(f"sess:{token}", SESSION_TTL, json.dumps({"id": user["id"], "email": user["email"]}))
    resp = make_response(jsonify({"id": user["id"], "email": user["email"]}))
    resp.set_cookie("sid", token, max_age=SESSION_TTL, httponly=True, samesite="Lax",
                    secure=request.headers.get("X-Forwarded-Proto") == "https")
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
    # simpler CSRF-Schutz: Cookie ist SameSite=Lax, schreibende Calls brauchen zusaetzlich den Header
    if request.method in ("POST", "PUT", "DELETE") and request.headers.get("X-Requested-With") != "freekube":
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
        r = q("SELECT (SELECT count(*) FROM users) AS users, "
              "(SELECT count(*) FROM servers WHERE status='running') AS running")[0]
        return {"users": r["users"], "running": r["running"]}
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
        rows = q("INSERT INTO users(email, pw_hash) VALUES (%s, %s) RETURNING id, email",
                 (email, generate_password_hash(password)))
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
        rows = q("SELECT id, name, game, plan, status, url, error, created_at, expires_at "
                 "FROM servers WHERE user_id=%s ORDER BY created_at DESC", (uid,))
        return [serialize(r) for r in rows]
    # kurzer TTL + Invalidierung durch API und Worker bei jeder Aenderung
    return jsonify(cached(servers_key(uid), 10, compute))


@app.post("/api/servers")
@login_required
def create_server():
    uid = g.user["id"]
    body = request.get_json(silent=True) or {}
    name = str(body.get("name", "")).strip()
    game = body.get("game")
    plan = body.get("plan")
    if not NAME_RE.match(name):
        return err("name: 3-32 chars, letters, digits, space, _ or -")
    if game not in common.GAMES:
        return err("unknown game")
    if plan not in common.PLANS:
        return err("unknown plan")
    if not rate_limit(f"rl:create:{uid}", 10, 3600):
        return err("slow down, max 10 creates per hour", 429)

    sid = uuid.uuid4()
    k8s_name = "gs-" + sid.hex[:12]
    expires = datetime.now(timezone.utc) + timedelta(hours=common.SERVER_TTL_HOURS)
    with db() as cur:
        # User-Row sperren, damit zwei parallele Requests das Limit nicht umgehen
        cur.execute("SELECT id FROM users WHERE id=%s FOR UPDATE", (uid,))
        if not cur.fetchone():
            return err("user vanished", 401)
        cur.execute("SELECT count(*) AS n FROM servers WHERE user_id=%s", (uid,))
        if cur.fetchone()["n"] >= common.MAX_SERVERS_PER_USER:
            return err(f"limit reached: max {common.MAX_SERVERS_PER_USER} servers on the free tier", 409)
        cur.execute("INSERT INTO servers(id, user_id, name, game, plan, status, k8s_name, expires_at) "
                    "VALUES (%s,%s,%s,%s,%s,'provisioning',%s,%s)",
                    (str(sid), uid, name, game, plan, k8s_name, expires))
    invalidate(servers_key(uid), "stats")
    enqueue("create", sid)
    return jsonify({"id": str(sid), "status": "provisioning"}), 202


def _owned(server_id):
    try:
        uuid.UUID(server_id)
    except ValueError:
        return None
    rows = q("SELECT id, status FROM servers WHERE id=%s AND user_id=%s", (server_id, g.user["id"]))
    return rows[0] if rows else None


@app.delete("/api/servers/<server_id>")
@login_required
def delete_server(server_id):
    if not _owned(server_id):
        return err("not found", 404)
    q("UPDATE servers SET status='deleting', updated_at=now() WHERE id=%s", (server_id,))
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
    q("UPDATE servers SET expires_at = now() + make_interval(hours => %s) WHERE id=%s",
      (common.SERVER_TTL_HOURS, server_id))
    invalidate(servers_key(g.user["id"]))
    return jsonify({"ok": True})
