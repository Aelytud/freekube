"""Shared utilities for API, Worker, and Maintenance.

All processes are stateless. Persistent data lives in Postgres and ephemeral
state (sessions, cache, job queue, rate limits) lives in Redis.
"""
import json
import logging
import os
import time
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

# ----------------------------------------------------------------- Catalog
GAMES = {
    "minecraft": {
        "label": "Minecraft (Java)",
        "port": 25565,
        "version": "1.21.4",
        "accent": "#5ee062",
        "description": "High-tickrate PaperMC & vanilla worlds with instant TLS status console.",
        "tags": ["PaperMC", "Mod-ready", "Crossplay"],
        "default_motd": "Creepers are friends, not food.",
    },
    "satisfactory": {
        "label": "Satisfactory",
        "port": 7777,
        "version": "1.0",
        "accent": "#f59e0b",
        "description": "Dedicated FICSIT pioneer servers built for massive belt & train megafactories.",
        "tags": ["Dedicated", "Auto-Save", "Co-op"],
        "default_motd": "The factory must grow.",
    },
    "valheim": {
        "label": "Valheim",
        "port": 2456,
        "version": "0.219.16",
        "accent": "#38bdf8",
        "description": "Persistent Viking realms with low-latency exploration and boss progression.",
        "tags": ["Vikings", "Always-On", "Co-op"],
        "default_motd": "Odin awaits in the Tenth World.",
    },
    "terraria": {
        "label": "Terraria",
        "port": 7777,
        "version": "1.4.4.9",
        "accent": "#c084fc",
        "description": "Classic 2D sandbox worlds with tShock-ready configuration and fast restarts.",
        "tags": ["tShock", "2D Sandbox", "Classic"],
        "default_motd": "Dig, fight, explore, build!",
    },
}

PLANS = {
    "starter": {
        "label": "Starter",
        "ram_mb": 2048,
        "slots": 10,
        "vcpu": 1,
        "description": "Great for small friend groups and private survival worlds.",
        "k8s": {"req": {"cpu": "10m", "memory": "32Mi"}, "lim": {"cpu": "100m", "memory": "96Mi"}},
    },
    "standard": {
        "label": "Standard",
        "ram_mb": 4096,
        "slots": 20,
        "vcpu": 2,
        "description": "Balanced headroom for modded packs and busy co-op sessions.",
        "k8s": {"req": {"cpu": "20m", "memory": "48Mi"}, "lim": {"cpu": "200m", "memory": "128Mi"}},
    },
    "plus": {
        "label": "Plus",
        "ram_mb": 6144,
        "slots": 32,
        "vcpu": 2,
        "description": "Maximum free-tier memory for large worlds and heavy factory builds.",
        "k8s": {"req": {"cpu": "25m", "memory": "64Mi"}, "lim": {"cpu": "250m", "memory": "160Mi"}},
    },
}


PRESETS = [
    {
        "id": "vanilla",
        "label": "Vanilla Survival",
        "game": "minecraft",
        "plan": "starter",
        "name": "Survival Realm",
        "motd": "Pure vanilla 1.21.4 survival — grief-free!",
        "autoscale": False,
        "min_replicas": 1,
        "max_replicas": 3,
    },
    {
        "id": "factory",
        "label": "Factory Megabase",
        "game": "satisfactory",
        "plan": "standard",
        "name": "FICSIT Alpha",
        "motd": "The factory must grow — Tier 9 co-op.",
        "autoscale": False,
        "min_replicas": 1,
        "max_replicas": 3,
    },
    {
        "id": "vikings",
        "label": "Viking Longhouse",
        "game": "valheim",
        "plan": "standard",
        "name": "Kubeheim",
        "motd": "Sail into the mist — bosses & building.",
        "autoscale": False,
        "min_replicas": 1,
        "max_replicas": 3,
    },
    {
        "id": "hpa-test",
        "label": "HPA Load Test",
        "game": "minecraft",
        "plan": "plus",
        "name": "Autoscale Arena",
        "motd": "HPA enabled (1-4 pods) — try Simulate CPU Load!",
        "autoscale": True,
        "min_replicas": 1,
        "max_replicas": 4,
    },
]


def public_catalog():
    return {
        "games": [
            {
                "id": k,
                "label": v["label"],
                "version": v["version"],
                "port": v["port"],
                "accent": v["accent"],
                "description": v["description"],
                "tags": v["tags"],
                "default_motd": v["default_motd"],
            }
            for k, v in GAMES.items()
        ],
        "plans": [
            {
                "id": k,
                "label": v["label"],
                "ram_mb": v["ram_mb"],
                "slots": v["slots"],
                "vcpu": v["vcpu"],
                "description": v["description"],
            }
            for k, v in PLANS.items()
        ],
        "presets": PRESETS,
        "limits": {"max_servers": MAX_SERVERS_PER_USER, "ttl_hours": SERVER_TTL_HOURS},
    }


# ------------------------------------------------------------------- Redis
_redis = None


def rds():
    global _redis
    if _redis is None:
        _redis = redis.Redis.from_url(REDIS_URL, decode_responses=True, socket_timeout=6, socket_connect_timeout=3)
    return _redis


def cached(key, ttl, fn):
    """Read-through cache. Falls back to direct execution if Redis is unavailable."""
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


# ---------------------------------------------------------------- Postgres
_pool = None


def _get_pool():
    global _pool
    if _pool is None:  # lazy init so each gunicorn worker creates its own pool after fork
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
    """Execute a query and return rows for SELECT/RETURNING statements."""
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
  motd text,
  autoscale boolean NOT NULL DEFAULT false,
  min_replicas int NOT NULL DEFAULT 1,
  max_replicas int NOT NULL DEFAULT 3,
  ready_replicas int NOT NULL DEFAULT 0,
  desired_replicas int NOT NULL DEFAULT 1,
  error text,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  expires_at timestamptz NOT NULL
);
ALTER TABLE servers ADD COLUMN IF NOT EXISTS motd text;
ALTER TABLE servers ADD COLUMN IF NOT EXISTS autoscale boolean NOT NULL DEFAULT false;
ALTER TABLE servers ADD COLUMN IF NOT EXISTS min_replicas int NOT NULL DEFAULT 1;
ALTER TABLE servers ADD COLUMN IF NOT EXISTS max_replicas int NOT NULL DEFAULT 3;
ALTER TABLE servers ADD COLUMN IF NOT EXISTS ready_replicas int NOT NULL DEFAULT 0;
ALTER TABLE servers ADD COLUMN IF NOT EXISTS desired_replicas int NOT NULL DEFAULT 1;
CREATE INDEX IF NOT EXISTS servers_user_idx ON servers(user_id);
CREATE INDEX IF NOT EXISTS servers_status_idx ON servers(status);
CREATE TABLE IF NOT EXISTS server_events (
  id serial PRIMARY KEY,
  server_id uuid NOT NULL REFERENCES servers(id) ON DELETE CASCADE,
  event_type text NOT NULL,
  message text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS server_events_sid_idx ON server_events(server_id, created_at DESC);
"""


def ensure_schema(retries=30):
    """Idempotent schema setup guarded by an advisory lock for multi-replica safety."""
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


def record_event(server_id, event_type, message):
    """Append an audit/activity event for a server."""
    try:
        q(
            "INSERT INTO server_events(server_id, event_type, message) VALUES (%s, %s, %s)",
            (str(server_id), event_type, message[:300]),
        )
    except Exception:  # noqa
        log.warning("failed to record event for %s", server_id)


def serialize(row):
    """Convert a database row into a JSON-serializable dict."""
    out = {}
    for k, v in row.items():
        if hasattr(v, "isoformat"):
            v = v.isoformat()
        elif k in ("id", "server_id") and v is not None:
            v = str(v)
        out[k] = v
    return out
