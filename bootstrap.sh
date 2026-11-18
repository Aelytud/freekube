#!/usr/bin/env bash
# One-time setup. Everything afterwards runs through Git.
#   ./bootstrap.sh          -> apply manifests via "oc apply -k" (simple)
#   ./bootstrap.sh gitops   -> ArgoCD (OpenShift GitOps Operator) manages the manifests
set -euo pipefail
cd "$(dirname "$0")"
MODE=${1:-plain}
NS=freekube

if grep -rq "CHANGE-ME" k8s gitops; then
  echo "First set your repo URL and push:"
  echo "  grep -rl CHANGE-ME k8s gitops | xargs sed -i 's#github.com/CHANGE-ME/freekube#github.com/YOUR-USER/YOUR-REPO#g'"
  exit 1
fi

oc get project "$NS" >/dev/null 2>&1 || oc new-project "$NS"
oc project "$NS" >/dev/null

# Secrets (do NOT belong in Git)
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

# Wait until BuildConfigs exist (takes a moment with ArgoCD), then trigger initial builds
for bc in freekube-api freekube-gameserver freekube-frontend; do
  for i in $(seq 1 60); do oc get bc/"$bc" >/dev/null 2>&1 && break; sleep 3; done
  oc start-build "$bc"
done

echo
echo "Watch builds: oc get builds -w"
echo "Route URL:    oc get route freekube -o jsonpath='{.spec.host}'"
