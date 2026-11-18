"""Maintenance (laeuft als CronJob):
1. abgelaufene Free-Server zum Loeschen markieren
2. haengende Jobs (Queue ist nur in-memory) neu einreihen
3. verwaiste Gameserver im Cluster (kein DB-Eintrag) wegraeumen
"""
import k8s
from common import enqueue, ensure_schema, invalidate, log, q

STUCK_AFTER = "2 minutes"


def expire():
    rows = q("UPDATE servers SET status='deleting', updated_at=now() "
             "WHERE expires_at < now() AND status <> 'deleting' RETURNING id, user_id")
    for r in rows:
        enqueue("delete", r["id"])
        invalidate(f"servers:{r['user_id']}", "stats")
    log.info("expired: %d", len(rows))


def requeue_stuck():
    rows = q("UPDATE servers SET updated_at=now() "
             f"WHERE status IN ('provisioning','deleting') AND updated_at < now() - interval '{STUCK_AFTER}' "
             "RETURNING id, status")
    for r in rows:
        enqueue("create" if r["status"] == "provisioning" else "delete", r["id"])
    log.info("requeued: %d", len(rows))


def remove_orphans():
    known = {r["k8s_name"] for r in q("SELECT k8s_name FROM servers")}
    removed = 0
    for d in k8s.list_items("deployments"):
        name = d["metadata"]["name"]
        if name not in known:
            for kind in ("routes", "services", "deployments"):
                k8s.delete(kind, name)
            removed += 1
    log.info("orphans removed: %d", removed)


if __name__ == "__main__":
    ensure_schema()
    expire()
    requeue_stuck()
    remove_orphans()
