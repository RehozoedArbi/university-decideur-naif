#!/bin/bash
set -euo pipefail

# ============================================================
# setup-decideur.sh — Build, push et déploie le décideur naïf
# ============================================================

DOCKERHUB_USER="darbi"
IMAGE_NAME="university-decideur"
IMAGE_TAG="latest"
CHART_PATH="$(cd "$(dirname "$0")/.." && pwd)/university-decideur"
APP_DIR="$(cd "$(dirname "$0")/.." && pwd)/app"

log() { echo -e "\n\033[1;34m[decideur]\033[0m $1"; }
ok()  { echo -e "\033[1;32m  ✓ $1\033[0m"; }
err() { echo -e "\033[1;31m  ✗ $1\033[0m"; exit 1; }

log "1. Vérification des prérequis"
for cmd in docker helm kubectl; do
  command -v "$cmd" &>/dev/null || err "$cmd non trouvé"
done
ok "docker, helm, kubectl présents"

log "2. Build de l'image Docker"
docker build -t "${DOCKERHUB_USER}/${IMAGE_NAME}:${IMAGE_TAG}" "${APP_DIR}"
ok "Image buildée : ${DOCKERHUB_USER}/${IMAGE_NAME}:${IMAGE_TAG}"

log "3. Push sur Docker Hub"
docker push "${DOCKERHUB_USER}/${IMAGE_NAME}:${IMAGE_TAG}"
ok "Image pushée sur Docker Hub"

log "4. Déploiement du chart Helm"
helm upgrade --install university-decideur "${CHART_PATH}" \
  --namespace university-decideur \
  --create-namespace \
  --wait
ok "Chart university-decideur déployé"

log "5. Vérification du pod"
kubectl wait --for=condition=Ready pods \
  -l app.kubernetes.io/name=decideur \
  -n university-decideur \
  --timeout=60s
ok "Pod décideur Ready"

log "6. Premiers logs (5 secondes)"
sleep 5
kubectl logs -n university-decideur \
  -l app.kubernetes.io/name=decideur \
  --tail=10

echo ""
ok "Décideur déployé et opérationnel"
echo ""
echo "  Suivre les propositions en temps réel :"
echo "    kubectl logs -n university-decideur -l app.kubernetes.io/name=decideur -f"
echo ""
echo "  Filtrer uniquement les propositions d'action :"
echo "    kubectl logs -n university-decideur -l app.kubernetes.io/name=decideur -f \\"
echo "    | grep ACTION_PROPOSAL"
echo ""
