"""Minimaler Kubernetes/OpenShift Client, nur requests + ServiceAccount Token.

Der Worker nutzt das, um Gameserver (Deployment + Service + Route) anzulegen.
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
}


def _url(kind, name=""):
    base = API + PATHS[kind].format(ns=namespace())
    return f"{base}/{name}" if name else base


def create(kind, body):
    r = _session().post(_url(kind), json=body, timeout=10)
    if r.status_code not in (200, 201, 409):  # 409 = gibts schon, ist ok (idempotent)
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


def manifests(server, game, plan):
    """Baut Deployment, Service, Route fuer einen (Fake-)Gameserver."""
    name = server["k8s_name"]
    labels = {"app": name, "hoster-role": "gameserver", "hoster-server-id": str(server["id"]),
              "app.kubernetes.io/part-of": "freekube"}
    deployment = {
        "apiVersion": "apps/v1", "kind": "Deployment",
        "metadata": {"name": name, "labels": labels},
        "spec": {
            "replicas": 1,
            "selector": {"matchLabels": {"app": name}},
            "template": {
                "metadata": {"labels": labels},
                "spec": {
                    "securityContext": {"seccompProfile": {"type": "RuntimeDefault"}},
                    "containers": [{
                        "name": "gameserver",
                        "image": GS_IMAGE,
                        "imagePullPolicy": "Always",
                        "ports": [{"containerPort": 8080}],
                        "env": [
                            {"name": "GAME", "value": server["game"]},
                            {"name": "GAME_LABEL", "value": game["label"]},
                            {"name": "GAME_VERSION", "value": game["version"]},
                            {"name": "GAME_PORT", "value": str(game["port"])},
                            {"name": "SERVER_NAME", "value": server["name"]},
                            {"name": "MAX_SLOTS", "value": str(plan["slots"])},
                            {"name": "RAM_MB", "value": str(plan["ram_mb"])},
                        ],
                        "readinessProbe": {"httpGet": {"path": "/healthz", "port": 8080}, "periodSeconds": 3},
                        "livenessProbe": {"httpGet": {"path": "/healthz", "port": 8080}, "initialDelaySeconds": 10},
                        "resources": {"requests": plan["k8s"]["req"], "limits": plan["k8s"]["lim"]},
                        "securityContext": {"allowPrivilegeEscalation": False, "capabilities": {"drop": ["ALL"]}},
                    }],
                },
            },
        },
    }
    service = {
        "apiVersion": "v1", "kind": "Service",
        "metadata": {"name": name, "labels": labels},
        "spec": {"selector": {"app": name}, "ports": [{"port": 8080, "targetPort": 8080}]},
    }
    route = {
        "apiVersion": "route.openshift.io/v1", "kind": "Route",
        "metadata": {"name": name, "labels": labels},
        "spec": {"to": {"kind": "Service", "name": name}, "port": {"targetPort": 8080},
                 "tls": {"termination": "edge", "insecureEdgeTerminationPolicy": "Redirect"}},
    }
    return deployment, service, route
