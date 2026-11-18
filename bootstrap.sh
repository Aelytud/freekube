#!/usr/bin/env bash
# Einmaliges Setup. Danach laeuft alles ueber Git.
#   ./bootstrap.sh          -> Manifeste per "oc apply -k" (simpel)
#   ./bootstrap.sh gitops   -> ArgoCD (OpenShift GitOps Operator) uebernimmt die Manifeste
set -euo pipefail
cd "$(dirname "$0")"
MODE=${1:-plain}
NS=freekube

if grep -rq "CHANGE-ME" k8s gitops; then
  echo "Erst die Repo-URL eintragen und pushen:"
  echo "  grep -rl CHANGE-ME k8s gitops | xargs sed -i 's#github.com/CHANGE-ME/freekube#github.com/DEIN-USER/DEIN-REPO#g'"
  exit 1
fi

oc get project "$NS" >/dev/null 2>&1 || oc new-project "$NS"
oc project "$NS" >/dev/null

# Secrets (gehoeren NICHT ins Git)
if ! oc get secret freekube-secrets >/dev/null 2>&1; then
  PGPW=$(openssl rand -hex 16); RPW=$(openssl rand -hex 16)
  oc create secret generic freekube-secrets \
    --from-literal=POSTGRESQL_USER=freekube \
    --from-literal=POSTGRESQL_PASSWORD="$PGPW" \
    --from-literal=POSTGRESQL_DATABASE=freekube \
    --from-literal=DATABASE_URL="postgresql://freekube:${PGPW}@postgres:5432/freekube" \
    --from-literal=REDIS_PASSWORD="$RPW" \
    --from-literal=REDIS_URL="redis://:${RPW}@redis:6379/0"
fi
oc get secret freekube-webhook >/dev/null 2>&1 || \
  oc create secret generic freekube-webhook --from-literal=WebHookSecretKey="$(openssl rand -hex 12)"

if [ "$MODE" = "gitops" ]; then
  oc label namespace "$NS" argocd.argoproj.io/managed-by=openshift-gitops --overwrite
  oc apply -f gitops/argocd-app.yaml
else
  oc apply -k k8s
fi

# Warten bis die BuildConfigs existieren (bei ArgoCD dauert das kurz), dann erste Builds anstossen
for bc in freekube-api freekube-gameserver freekube-frontend; do
  for i in $(seq 1 60); do oc get bc/"$bc" >/dev/null 2>&1 && break; sleep 3; done
  oc start-build "$bc"
done

echo
echo "Builds beobachten: oc get builds -w"
echo "URL danach:        oc get route freekube -o jsonpath='{.spec.host}'"
