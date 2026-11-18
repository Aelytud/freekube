"""Worker: consumes jobs from the Redis queue and provisions/updates/deletes game servers.

Stateless and horizontally scalable (oc scale deploy/worker --replicas=3).
All operations are idempotent so duplicate execution is safe.
"""
import json
import time
from datetime import datetime, timezone

import common
import k8s
from common import invalidate, log, q, rds, record_event

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
        if s.get("autoscale"):
            k8s.sync_hpa(s)
        record_event(server_id, "provisioned", f"Cluster resources created ({s['k8s_name']}).")
        log.info("created resources for %s (%s)", s["k8s_name"], server_id)
    except Exception as e:  # noqa
        log.exception("create failed")
        q("UPDATE servers SET status='failed', error=%s, updated_at=now() WHERE id=%s", (str(e)[:300], server_id))
        record_event(server_id, "error", f"Provisioning failed: {str(e)[:180]}")
        touch_cache(s["user_id"])


def handle_restart(server_id):
    s = get_server(server_id)
    if not s or s["status"] in ("deleting", "failed"):
        return
    game, plan = common.GAMES[s["game"]], common.PLANS[s["plan"]]
    ts = datetime.now(timezone.utc).isoformat()
    k8s.patch_deployment(
        s["k8s_name"],
        {
            "spec": {
                "template": {
                    "metadata": {"annotations": {"freekube/restarted-at": ts}},
                    "spec": {
                        "containers": [
                            {
                                "name": "gameserver",
                                "env": k8s._server_env(s, game, plan),
                                "resources": {"requests": plan["k8s"]["req"], "limits": plan["k8s"]["lim"]},
                            }
                        ]
                    },
                }
            }
        },
    )
    record_event(server_id, "restarted", f"Rolling pod restart triggered for {s['k8s_name']}.")
    log.info("restarted %s (%s)", s["k8s_name"], server_id)


def handle_autoscale(server_id):
    s = get_server(server_id)
    if not s or s["status"] in ("deleting", "failed"):
        return
    k8s.sync_hpa(s)
    msg = (
        f"HPA enabled ({s['min_replicas']}–{s['max_replicas']} pods)."
        if s.get("autoscale")
        else "HPA disabled (fixed 1 pod)."
    )
    record_event(server_id, "autoscale", msg)
    log.info("synced HPA for %s (%s): %s", s["k8s_name"], server_id, msg)


def handle_stop(server_id):
    s = get_server(server_id)
    if not s or s["status"] == "deleting":
        return
    k8s.delete("hpas", s["k8s_name"])
    k8s.patch_deployment(s["k8s_name"], {"spec": {"replicas": 0}})
    q(
        "UPDATE servers SET status='stopped', ready_replicas=0, desired_replicas=0, updated_at=now() WHERE id=%s",
        (server_id,),
    )
    record_event(server_id, "stopped", f"Scaled {s['k8s_name']} to 0 pods.")
    touch_cache(s["user_id"])


def handle_start(server_id):
    s = get_server(server_id)
    if not s or s["status"] == "deleting":
        return
    desired = int(s.get("min_replicas") or 1) if s.get("autoscale") else 1
    k8s.patch_deployment(s["k8s_name"], {"spec": {"replicas": desired}})
    if s.get("autoscale"):
        k8s.sync_hpa(s)
    q(
        "UPDATE servers SET status='provisioning', desired_replicas=%s, updated_at=now() WHERE id=%s",
        (desired, server_id),
    )
    record_event(server_id, "starting", f"Scaling {s['k8s_name']} back up to {desired} pod(s).")
    touch_cache(s["user_id"])


def handle_logs(server_id):
    s = get_server(server_id)
    if not s:
        return
    lines = k8s.pod_logs(s["k8s_name"], tail=50)
    rds().setex(f"logs:{server_id}", 60, json.dumps({"lines": lines, "ts": datetime.now(timezone.utc).isoformat()}))


def handle_delete(server_id):
    s = get_server(server_id)
    if not s:
        return
    for kind in ("hpas", "routes", "services", "deployments"):
        k8s.delete(kind, s["k8s_name"])
    q("DELETE FROM servers WHERE id=%s", (server_id,))
    touch_cache(s["user_id"])
    log.info("deleted %s (%s)", s["k8s_name"], server_id)


def poll_provisioning():
    """Check whether provisioning servers have become ready in the cluster."""
    for s in q(
        "SELECT id, user_id, k8s_name, now() - created_at AS age "
        "FROM servers WHERE status='provisioning'"
    ):
        try:
            if k8s.deployment_ready(s["k8s_name"]):
                host = k8s.route_host(s["k8s_name"])
                url = f"https://{host}" if host else None
                q(
                    "UPDATE servers SET status='running', url=%s, ready_replicas=1, desired_replicas=1, updated_at=now() "
                    "WHERE id=%s AND status='provisioning'",
                    (url, str(s["id"])),
                )
                record_event(s["id"], "running", f"Server is online at {url or s['k8s_name']}.")
                touch_cache(s["user_id"])
            elif s["age"].total_seconds() > PROVISION_TIMEOUT_S:
                q(
                    "UPDATE servers SET status='failed', error='provisioning timeout', updated_at=now() "
                    "WHERE id=%s AND status='provisioning'",
                    (str(s["id"]),),
                )
                record_event(s["id"], "error", "Provisioning timed out after 300s.")
                touch_cache(s["user_id"])
        except Exception:  # noqa
            log.exception("poll failed for %s", s["k8s_name"])


def sync_running_replicas():
    """Sync live ready/desired replica counts for running servers (for HPA visibility)."""
    for s in q("SELECT id, user_id, k8s_name, ready_replicas, desired_replicas FROM servers WHERE status='running'"):
        try:
            ready, desired = k8s.deployment_replicas(s["k8s_name"])
            if ready != s["ready_replicas"] or desired != s["desired_replicas"]:
                q(
                    "UPDATE servers SET ready_replicas=%s, desired_replicas=%s WHERE id=%s",
                    (ready, desired, str(s["id"])),
                )
                touch_cache(s["user_id"])
        except Exception:  # noqa
            log.debug("replica sync skipped for %s", s["k8s_name"])


HANDLERS = {
    "create": handle_create,
    "restart": handle_restart,
    "autoscale": handle_autoscale,
    "stop": handle_stop,
    "start": handle_start,
    "logs": handle_logs,
    "delete": handle_delete,
}


def main():
    common.ensure_schema()
    log.info("worker started, namespace=%s", k8s.namespace())
    last_sync = 0.0
    while True:
        try:
            rds().setex("worker:alive", 25, datetime.now(timezone.utc).isoformat())
            item = rds().blpop(common.QUEUE, timeout=5)
            if item:
                job = json.loads(item[1])
                log.info("job %s", job)
                try:
                    HANDLERS[job["op"]](job["id"])
                except Exception:  # noqa
                    # Avoid hot-loop retries; the maintenance CronJob requeues stuck jobs
                    log.exception("job failed, maintenance will requeue")
            poll_provisioning()
            if time.time() - last_sync > 10:
                sync_running_replicas()
                last_sync = time.time()
        except Exception:  # noqa
            log.exception("loop error")
            time.sleep(3)


if __name__ == "__main__":
    main()
