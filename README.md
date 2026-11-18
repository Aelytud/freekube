> **This is just an example project to use for OpenShift testing.**
> It is not a real game server hoster. The "game servers" are small web pages pretending to be Minecraft or Satisfactory servers.
> Do not use it in production: no email verification, no password reset, single Postgres, Redis without persistence.
> Everything here exists to try out Deployments, StatefulSets, CronJobs, Routes, NetworkPolicies, RBAC, HPA, Builds and GitOps on a test cluster.

# FreeKube: fake free game server hoster auf OpenShift

Shopseite + Backend, das User und "Gameserver" verwaltet. Die Gameserver sind kleine
Webseiten, die so tun als waeren sie Minecraft/Satisfactory Server. Das Backend legt sie
zur Laufzeit als echte Deployments + Services + Routes im Cluster an.

```
Browser -> Route (TLS) -> frontend (nginx, 2+ Pods, Cache fuer /api/catalog + /api/stats)
                              |
                              v /api
                           api (Flask, 2-6 Pods per HPA)  --enqueue-->  Redis (Sessions, Cache, Queue, Rate Limits)
                              |                                              |
                              v                                              v blpop
                          Postgres (StatefulSet+PVC)  <----------------  worker (2+ Pods, ServiceAccount mit RBAC)
                                                                             |  Kubernetes API
                                                    maintenance (CronJob)    v
                                                    Ablauf, Requeue,      gs-xxxx Deployment + Service + Route
                                                    Waisen aufraeumen     (pro User-Server, eigene Route)
```

## Stateless?
- api, worker, frontend, gameserver halten keinen lokalen State. Beliebig skalierbar, jederzeit killbar.
- Sessions, Cache, Job-Queue, Rate Limits: Redis (bewusst ohne Persistenz).
- Dauerhafte Daten (users, servers): Postgres. Das ist die einzige stateful Komponente.

## Deploy per Git
Das Repo ist die einzige Quelle. Builds ziehen den Code aus Git, die Manifeste liegen in `k8s/`.

```bash
# 1) Ordner als Repo pushen (public reicht)
git init -b main && git add . && git commit -m "init"
git remote add origin https://github.com/DEIN-USER/DEIN-REPO.git && git push -u origin main

# 2) Repo-URL in Builds und ArgoCD App eintragen, committen, pushen
grep -rl CHANGE-ME k8s gitops | xargs sed -i 's#github.com/CHANGE-ME/freekube#github.com/DEIN-USER/DEIN-REPO#g'
git commit -am "repo url" && git push

# 3a) Simpel: kubectl-style apply
oc login ...
./bootstrap.sh

# 3b) GitOps: ArgoCD uebernimmt (OpenShift GitOps Operator muss installiert sein)
./bootstrap.sh gitops
```
Das Projekt heisst fest `freekube`. Secrets (Passwoerter) erzeugt das Script im Cluster, die liegen nie im Git.

### Was nach einem `git push` passiert
- **Manifeste** (`k8s/`): mit ArgoCD automatisch (Polling alle ~3 Minuten, selfHeal und prune an).
  Ohne ArgoCD: `oc apply -k k8s`.
- **Images**: ein Build wird per `oc start-build freekube-api` angestossen (api, gameserver, frontend).
  Danach rollen die Image-Trigger die Deployments automatisch neu aus.
  Automatisch bei jedem Push geht es nur mit Webhook. Der Cluster muss dafuer von GitHub erreichbar sein,
  bei einem lokalen Cluster also nur ueber einen Tunnel (z.B. smee.io oder ngrok).
  URL: `oc describe bc/freekube-api | grep -A1 -i github`, im GitHub Repo unter Settings > Webhooks eintragen
  (Content type `application/json`).

## Ressourcen-Zoo
Deployment (api, worker, frontend, redis, gs-*), StatefulSet + PVC (postgres), CronJob (maintenance),
Services (ClusterIP, headless), Route (TLS edge, auch pro Gameserver), ServiceAccount + Role + RoleBinding,
Secret, HPA (api, frontend), PodDisruptionBudget, NetworkPolicies (default deny + gezielte Freigaben),
ResourceQuota, ImageStreams + BuildConfigs, Image Triggers (neuer Build = automatischer Rollout),
Init-Container, Liveness/Readiness Probes, Requests/Limits.

## Ausprobieren
```bash
oc get pods -w
# 1) Registrieren, Server anlegen, URL oeffnen
# 2) Skalieren
oc scale deploy/api --replicas=4
oc scale deploy/worker --replicas=3
# 3) Stateless beweisen: API-Pods killen, Login bleibt bestehen
oc delete pod -l app=api
# 4) Cache sehen (zweiter Aufruf = HIT)
curl -sI https://<route>/api/catalog | grep -i x-cache
# 5) Was hat der Worker gemacht?
oc logs -f deploy/worker
# 6) Gameserver im Cluster ansehen
oc get deploy,svc,route -l hoster-role=gameserver
# 7) NetworkPolicy testen: Frontend darf nicht an die DB
oc exec deploy/frontend -- sh -c 'nc -zv -w2 postgres 5432'   # sollte haengen/fehlschlagen
# 8) Redis killen: Sessions, Cache, Queue weg. Maintenance stellt Jobs nach <= 2 Min wieder her
oc delete pod -l app=redis
# 9) Ablauf testen: TTL kurz setzen, neuen Server anlegen
oc set env deploy/api SERVER_TTL_HOURS=0
```

## Code aendern und neu deployen
```bash
git commit -am "change" && git push
oc start-build freekube-api --follow        # rollt api + worker neu
oc start-build freekube-frontend --follow
```
Aufraeumen: `oc delete project freekube` (bei ArgoCD vorher die Application loeschen).

Neue Gameserver ziehen automatisch das neueste gameserver-Image (imagePullPolicy Always).

## Bekannte Grenzen (Absicht, ist ein Testprojekt)
- Rate Limit nutzt X-Forwarded-For, im Zweifel faelschbar.
- Keine E-Mail-Verifikation, kein Passwort-Reset.
- Redis ohne Persistenz, kein HA. Postgres single instance.
- Images kommen teils von docker.io (nginx-unprivileged). Bei Rate-Limits: Image per
  `oc import-image` oder `oc tag` in die interne Registry holen und im Dockerfile ersetzen.
- Gameserver sind HTTP-Dummys, keine echten Spiele-Ports.
