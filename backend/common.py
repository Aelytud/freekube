"""Gemeinsamer Kram fuer API, Worker und Maintenance.

Die Prozesse halten selbst KEINEN State. Alles liegt in Postgres (dauerhaft)
oder Redis (Sessions, Cache, Job-Queue).
"""
import json
import os
import time
import logging
from contextlib import contextmanager

import psycopg2
import psycopg2.extras
import psycopg2.pool
import redis

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("freekube")

DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://freekube:freekube@localhost:5432/freekube")
REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")

MAX_SERVERS_PER_USER = int(os.environ.get("MAX_SERVERS_PER_USER", "2"))
SERVER_TTL_HOURS = int(os.environ.get("SERVER_TTL_HOURS", "24"))
QUEUE = "jobs"

# ---------------------------------------------------------------- Katalog
GAMES = {
    "minecraft": {"label": "Minecraft (Java)", "port": 25565, "version": "1.21.4"},
    "satisfactory": {"label": "Satisfactory", "port": 7777, "version": "1.0"},
}
PLANS = {
    "starter": {"label": "Starter", "ram_mb": 2048, "slots": 10,
                "k8s": {"req": {"cpu": "10m", "memory": "32Mi"}, "lim": {"cpu": "100m", "memory": "96Mi"}}},
    "standard": {"label": "Standard", "ram_mb": 4096, "slots": 20,
                 "k8s": {"req": {"cpu": "20m", "memory": "48Mi"}, "lim": {"cpu": "200m", "memory": "128Mi"}}},
}


def public_catalog():
    return {
        "games": [{"id": k, "label": v["label"], "version": v["version"]} for k, v in GAMES.items()],
        "plans": [{"id": k, "label": v["label"], "ram_mb": v["ram_mb"], "slots": v["slots"]} for k, v in PLANS.items()],
        "limits": {"max_servers": MAX_SERVERS_PER_USER, "ttl_hours": SERVER_TTL_HOURS},
    }


# ------------------------------------------------------------------ Redis
_redis = None


def rds():
    global _redis
    if _redis is None:
        _redis = redis.Redis.from_url(REDIS_URL, decode_responses=True, socket_timeout=6, socket_connect_timeout=3)
    return _redis


def cached(key, ttl, fn):
    """Read-through Cache. Wenn Redis zickt, einfach ohne Cache weiter."""
    try:
        hit = rds().get(key)
        if hit is not None:
            return json.loads(hit)
    except redis.RedisError:
        log.warning("cache read failed for %s", key)
    value = fn()
    try:
        rds().setex(key, ttl, json.dumps(value))
    except redis.RedisError:
        log.warning("cache write failed for %s", key)
    return value


def invalidate(*keys):
    try:
        rds().delete(*keys)
    except redis.RedisError:
        log.warning("cache invalidate failed for %s", keys)


def enqueue(op, server_id):
    rds().rpush(QUEUE, json.dumps({"op": op, "id": str(server_id)}))


# --------------------------------------------------------------- Postgres
_pool = None


def _get_pool():
    global _pool
    if _pool is None:  # lazy, damit es nach dem gunicorn-fork pro Prozess passiert
        _pool = psycopg2.pool.ThreadedConnectionPool(1, 8, DATABASE_URL)
    return _pool


@contextmanager
def db():
    pool = _get_pool()
    conn = pool.getconn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            yield cur
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        pool.putconn(conn)


def q(sql, args=()):
    """Kurzform: fuehrt aus, liefert Zeilen bei SELECT/RETURNING."""
    with db() as cur:
        cur.execute(sql, args)
        return cur.fetchall() if cur.description else None


SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
  id serial PRIMARY KEY,
  email text UNIQUE NOT NULL,
  pw_hash text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS servers (
  id uuid PRIMARY KEY,
  user_id int NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  name text NOT NULL,
  game text NOT NULL,
  plan text NOT NULL,
  status text NOT NULL,
  k8s_name text NOT NULL,
  url text,
  error text,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  expires_at timestamptz NOT NULL
);
CREATE INDEX IF NOT EXISTS servers_user_idx ON servers(user_id);
CREATE INDEX IF NOT EXISTS servers_status_idx ON servers(status);
"""


def ensure_schema(retries=30):
    """Idempotent, mit Advisory Lock, damit mehrere Replicas sich nicht ins Gehege kommen."""
    for attempt in range(retries):
        try:
            with db() as cur:
                cur.execute("SELECT pg_advisory_xact_lock(424242)")
                cur.execute(SCHEMA)
            return
        except psycopg2.OperationalError:
            log.warning("db not ready (%s/%s)", attempt + 1, retries)
            time.sleep(2)
    raise RuntimeError("database never became ready")


def serialize(row):
    """Row -> JSON-faehiges dict."""
    out = {}
    for k, v in row.items():
        if hasattr(v, "isoformat"):
            v = v.isoformat()
        elif k == "id" and v is not None:
            v = str(v)
        out[k] = v
    return out
