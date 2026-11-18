"""Worker: holt Jobs aus der Redis-Queue und baut/loescht Gameserver im Cluster.

Stateless und beliebig skalierbar (oc scale deploy/worker --replicas=3).
Alle Operationen sind idempotent, doppelte Ausfuehrung schadet nicht.
"""
import json
import time

import k8s
import common
from common import invalidate, log, q, rds

PROVISION_TIMEOUT_S = 300


def touch_cache(uid):
    invalidate(f"servers:{uid}", "stats")


def get_server(server_id):
    rows = q("SELECT * FROM servers WHERE id=%s", (server_id,))
    return rows[0] if rows else None


def handle_create(server_id):
    s = get_server(server_id)
    if not s or s["status"] != "provisioning":
        return
    game, plan = common.GAMES[s["game"]], common.PLANS[s["plan"]]
    try:
        for kind, body in zip(("deployments", "services", "routes"), k8s.manifests(s, game, plan)):
            k8s.create(kind, body)
        log.info("created resources for %s (%s)", s["k8s_name"], server_id)
    except Exception as e:  # noqa
        log.exception("create failed")
        q("UPDATE servers SET status='failed', error=%s, updated_at=now() WHERE id=%s", (str(e)[:300], server_id))
        touch_cache(s["user_id"])


def handle_delete(server_id):
    s = get_server(server_id)
    if not s:
        return
    for kind in ("routes", "services", "deployments"):
        k8s.delete(kind, s["k8s_name"])
    q("DELETE FROM servers WHERE id=%s", (server_id,))
    touch_cache(s["user_id"])
    log.info("deleted %s (%s)", s["k8s_name"], server_id)


def poll_provisioning():
    """Schaut nach, ob provisionierende Server inzwischen laufen."""
    for s in q("SELECT id, user_id, k8s_name, now() - created_at AS age "
               "FROM servers WHERE status='provisioning'"):
        try:
            if k8s.deployment_ready(s["k8s_name"]):
                host = k8s.route_host(s["k8s_name"])
                url = f"https://{host}" if host else None
                q("UPDATE servers SET status='running', url=%s, updated_at=now() "
                  "WHERE id=%s AND status='provisioning'", (url, str(s["id"])))
                touch_cache(s["user_id"])
            elif s["age"].total_seconds() > PROVISION_TIMEOUT_S:
                q("UPDATE servers SET status='failed', error='provisioning timeout', updated_at=now() "
                  "WHERE id=%s AND status='provisioning'", (str(s["id"]),))
                touch_cache(s["user_id"])
        except Exception:  # noqa
            log.exception("poll failed for %s", s["k8s_name"])


HANDLERS = {"create": handle_create, "delete": handle_delete}


def main():
    common.ensure_schema()
    log.info("worker started, namespace=%s", k8s.namespace())
    while True:
        try:
            item = rds().blpop(common.QUEUE, timeout=5)
            if item:
                job = json.loads(item[1])
                log.info("job %s", job)
                try:
                    HANDLERS[job["op"]](job["id"])
                except Exception:  # noqa
                    # kein Sofort-Retry (Hot-Loop), der Maintenance-CronJob stellt haengende Jobs wieder her
                    log.exception("job failed, maintenance will requeue")
            poll_provisioning()
        except Exception:  # noqa
            log.exception("loop error")
            time.sleep(3)


if __name__ == "__main__":
    main()
