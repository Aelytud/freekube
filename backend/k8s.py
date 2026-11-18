"""Minimal Kubernetes/OpenShift client using requests and the ServiceAccount token.

Used by the worker and maintenance job to manage game server resources
(Deployment + Service + Route).
"""
import os
import requests

SA = "/var/run/secrets/kubernetes.io/serviceaccount"
API = os.environ.get("K8S_API", "https://kubernetes.default.svc")
GS_IMAGE = os.environ.get("GAMESERVER_IMAGE", "freekube-gameserver:latest")
LABEL = "hoster-role=gameserver"


def namespace():
    if os.environ.get("POD_NAMESPACE"):
        return os.environ["POD_NAMESPACE"]
    with open(f"{SA}/namespace") as f:
        return f.read().strip()


def _session():
    s = requests.Session()
    with open(f"{SA}/token") as f:
        s.headers["Authorization"] = "Bearer " + f.read().strip()
    s.verify = f"{SA}/ca.crt"
    return s


PATHS = {
    "deployments": "/apis/apps/v1/namespaces/{ns}/deployments",
    "services": "/api/v1/namespaces/{ns}/services",
    "routes": "/apis/route.openshift.io/v1/namespaces/{ns}/routes",
    "hpas": "/apis/autoscaling/v2/namespaces/{ns}/horizontalpodautoscalers",
    "pods": "/api/v1/namespaces/{ns}/pods",
}


def _url(kind, name=""):
    base = API + PATHS[kind].format(ns=namespace())
    return f"{base}/{name}" if name else base


def create(kind, body):
    r = _session().post(_url(kind), json=body, timeout=10)
    if r.status_code not in (200, 201, 409):  # 409 = already exists (idempotent)
        r.raise_for_status()


def patch_deployment(name, body):
    r = _session().patch(
        _url("deployments", name),
        json=body,
        headers={"Content-Type": "application/strategic-merge-patch+json"},
        timeout=10,
    )
    if r.status_code != 404:
        r.raise_for_status()


def patch(kind, name, body):
    r = _session().patch(
        _url(kind, name),
        json=body,
        headers={"Content-Type": "application/strategic-merge-patch+json"},
        timeout=10,
    )
    if r.status_code != 404:
        r.raise_for_status()


def get(kind, name):
    r = _session().get(_url(kind, name), timeout=10)
    if r.status_code == 404:
        return None
    r.raise_for_status()
    return r.json()


def delete(kind, name):
    r = _session().delete(_url(kind, name), json={"propagationPolicy": "Background"}, timeout=10)
    if r.status_code not in (200, 202, 404):
        r.raise_for_status()


def list_items(kind, selector=LABEL):
    r = _session().get(_url(kind), params={"labelSelector": selector}, timeout=10)
    r.raise_for_status()
    return r.json().get("items", [])


def route_host(name):
    route = get("routes", name)
    return route["spec"].get("host") if route else None


def deployment_ready(name):
    d = get("deployments", name)
    return bool(d and (d.get("status", {}).get("availableReplicas") or 0) >= 1)


def deployment_replicas(name):
    d = get("deployments", name)
    if not d:
        return 0, 0
    ready = int(d.get("status", {}).get("readyReplicas") or 0)
    desired = int(d.get("spec", {}).get("replicas") or 0)
    return ready, desired


def pod_logs(name, tail=50):
    pods = list_items("pods", selector=f"app={name}")
    if not pods:
        return ["No active pods found for this deployment."]
    pod_name = pods[0]["metadata"]["name"]
    r = _session().get(_url("pods", f"{pod_name}/log"), params={"tailLines": tail}, timeout=10)
    if r.status_code != 200:
        return [f"Could not fetch logs from pod {pod_name} (HTTP {r.status_code})."]
    lines = [line for line in r.text.splitlines() if line.strip()]
    return lines or [f"Pod {pod_name} has no log output yet."]


def _server_env(server, game, plan):
    return [
        {"name": "GAME", "value": server["game"]},
        {"name": "GAME_LABEL", "value": game["label"]},
        {"name": "GAME_VERSION", "value": game["version"]},
        {"name": "GAME_PORT", "value": str(game["port"])},
        {"name": "SERVER_NAME", "value": server["name"]},
        {"name": "MOTD", "value": server.get("motd") or game["default_motd"]},
        {"name": "MAX_SLOTS", "value": str(plan["slots"])},
        {"name": "RAM_MB", "value": str(plan["ram_mb"])},
    ]


def manifests(server, game, plan):
    """Build Deployment, Service, and Route manifests for a (fake) game server."""
    name = server["k8s_name"]
    labels = {
        "app": name,
        "hoster-role": "gameserver",
        "hoster-server-id": str(server["id"]),
        "app.kubernetes.io/part-of": "freekube",
    }
    deployment = {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {"name": name, "labels": labels},
        "spec": {
            "replicas": 1,
            "selector": {"matchLabels": {"app": name}},
            "template": {
                "metadata": {"labels": labels},
                "spec": {
                    "securityContext": {"seccompProfile": {"type": "RuntimeDefault"}},
                    "containers": [
                        {
                            "name": "gameserver",
                            "image": GS_IMAGE,
                            "imagePullPolicy": "Always",
                            "ports": [{"containerPort": 8080}],
                            "env": _server_env(server, game, plan),
                            "readinessProbe": {"httpGet": {"path": "/healthz", "port": 8080}, "periodSeconds": 3},
                            "livenessProbe": {
                                "httpGet": {"path": "/healthz", "port": 8080},
                                "initialDelaySeconds": 10,
                            },
                            "resources": {"requests": plan["k8s"]["req"], "limits": plan["k8s"]["lim"]},
                            "securityContext": {
                                "allowPrivilegeEscalation": False,
                                "capabilities": {"drop": ["ALL"]},
                            },
                        }
                    ],
                },
            },
        },
    }
    service = {
        "apiVersion": "v1",
        "kind": "Service",
        "metadata": {"name": name, "labels": labels},
        "spec": {"selector": {"app": name}, "ports": [{"port": 8080, "targetPort": 8080}]},
    }
    route = {
        "apiVersion": "route.openshift.io/v1",
        "kind": "Route",
        "metadata": {"name": name, "labels": labels},
        "spec": {
            "to": {"kind": "Service", "name": name},
            "port": {"targetPort": 8080},
            "tls": {"termination": "edge", "insecureEdgeTerminationPolicy": "Redirect"},
        },
    }
    return deployment, service, route


def hpa_manifest(server):
    """Build an autoscaling/v2 HorizontalPodAutoscaler for a game server Deployment."""
    name = server["k8s_name"]
    labels = {
        "app": name,
        "hoster-role": "gameserver",
        "hoster-server-id": str(server["id"]),
        "app.kubernetes.io/part-of": "freekube",
    }
    return {
        "apiVersion": "autoscaling/v2",
        "kind": "HorizontalPodAutoscaler",
        "metadata": {"name": name, "labels": labels},
        "spec": {
            "scaleTargetRef": {"apiVersion": "apps/v1", "kind": "Deployment", "name": name},
            "minReplicas": int(server.get("min_replicas") or 1),
            "maxReplicas": int(server.get("max_replicas") or 3),
            "metrics": [
                {
                    "type": "Resource",
                    "resource": {"name": "cpu", "target": {"type": "Utilization", "averageUtilization": 60}},
                }
            ],
            "behavior": {
                "scaleDown": {"stabilizationWindowSeconds": 60},
            },
        },
    }


def sync_hpa(server):
    """Create, update, or remove the HorizontalPodAutoscaler for a game server."""
    name = server["k8s_name"]
    if server.get("autoscale"):
        body = hpa_manifest(server)
        if get("hpas", name):
            patch("hpas", name, {"spec": body["spec"]})
        else:
            create("hpas", body)
    else:
        delete("hpas", name)
        patch_deployment(name, {"spec": {"replicas": 1}})
