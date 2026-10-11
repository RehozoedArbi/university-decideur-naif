"""
decideur.py — Naive Healer
==========================
Surveille Prometheus toutes les 10 secondes et propose des actions
de remédiation basées sur des règles simples (if/else sur métriques).

Il est intentionnellement naïf :
- Il ne vérifie pas si son action est sûre
- Il ne sait pas que la DB a changé de schéma
- Il ne sait pas si un restart va casser le quorum
- Il ne sait pas si le HPA est déjà au maximum

C'est le rôle du moteur de vérification de bloquer ses mauvaises
décisions — le décideur existe pour générer des propositions, pas
pour les valider.

Chaque proposition est un objet JSON structuré envoyé au moteur
(via stdout pour l'instant — le moteur le lira depuis là).
"""

import os
import time
import json
import logging
from datetime import datetime, timezone

import requests

# ============================================================
# Configuration
# ============================================================
PROMETHEUS_URL = os.environ.get(
    "PROMETHEUS_URL",
    "http://prometheus.monitoring.svc.cluster.local:9090"
)
POLL_INTERVAL_SECONDS = int(os.environ.get("POLL_INTERVAL_SECONDS", "10"))
APP_NAMESPACE         = os.environ.get("APP_NAMESPACE", "university-app")
LOG_LEVEL             = os.environ.get("LOG_LEVEL", "INFO")

# Seuils calibrés sur les observations réelles des 8 scénarios de chaos
THRESHOLDS = {
    # Latence P95 en secondes (calibré sur scénario 3 : 400ms, scénario 7 : 8-10s)
    "latency_p95_seconds": float(os.environ.get("THRESHOLD_LATENCY_P95", "0.5")),
    # Taux d'erreur 5xx (calibré sur scénario 2 : HTTPChaos injecte des 500)
    "error_rate_5xx":      float(os.environ.get("THRESHOLD_ERROR_RATE", "0.10")),
    # Durée minimale d'anomalie avant proposition (évite les faux positifs)
    "anomaly_duration_s":  int(os.environ.get("THRESHOLD_ANOMALY_DURATION", "60")),
    # Nombre de pods ready minimum (quorum)
    "min_replicas_ready":  int(os.environ.get("THRESHOLD_MIN_REPLICAS", "1")),
    # Seuil pour détecter une dégradation simultanée (scénario 7 : cause racine DB)
    "cascade_services_threshold": int(os.environ.get("THRESHOLD_CASCADE", "2")),
}

SERVICES = ["student-service", "enrollment-service", "teacher-admin-service"]

logging.basicConfig(
    level=LOG_LEVEL,
    format='{"timestamp": "%(asctime)s", "level": "%(levelname)s", '
           '"service": "decideur", "message": %(message)s}',
    datefmt="%Y-%m-%dT%H:%M:%S+00:00"
)
logger = logging.getLogger("decideur")

# ============================================================
# Historique des anomalies (pour respecter anomaly_duration_s)
# ============================================================
anomaly_start: dict = {}   # service → datetime de début d'anomalie
last_action:   dict = {}   # service → dernière action proposée + timestamp


# ============================================================
# Requêtes Prometheus
# ============================================================
def query_prometheus(promql: str) -> list:
    """Exécute une requête PromQL et retourne les résultats."""
    try:
        r = requests.get(
            f"{PROMETHEUS_URL}/api/v1/query",
            params={"query": promql},
            timeout=5
        )
        r.raise_for_status()
        return r.json().get("data", {}).get("result", [])
    except Exception as e:
        logger.warning(json.dumps(f"Prometheus inaccessible : {e}"))
        return []


def get_latency_p95(service: str) -> float | None:
    """Latence P95 en secondes pour un service (dernière minute)."""
    results = query_prometheus(
        f'histogram_quantile(0.95, '
        f'sum(rate(flask_http_request_duration_seconds_bucket'
        f'{{service_name="{service}"}}[1m])) by (le))'
    )
    if not results:
        # Fallback Spring Boot (teacher-admin-service)
        results = query_prometheus(
            f'histogram_quantile(0.95, '
            f'sum(rate(http_server_requests_seconds_bucket'
            f'{{job="{service}"}}[1m])) by (le))'
        )
    if results and results[0].get("value"):
        try:
            return float(results[0]["value"][1])
        except (ValueError, IndexError):
            return None
    return None


def get_error_rate_5xx(service: str) -> float | None:
    """Taux de requêtes 5xx sur le dernier minute."""
    total = query_prometheus(
        f'sum(rate(flask_http_request_total{{service_name="{service}"}}[1m]))'
    )
    errors = query_prometheus(
        f'sum(rate(flask_http_request_total'
        f'{{service_name="{service}", status=~"5.."}}[1m]))'
    )
    try:
        t = float(total[0]["value"][1]) if total else 0
        e = float(errors[0]["value"][1]) if errors else 0
        return e / t if t > 0 else 0.0
    except (ValueError, IndexError, ZeroDivisionError):
        return None


def get_replicas_ready(service: str) -> int | None:
    """Nombre de réplicas prêts pour un déploiement."""
    results = query_prometheus(
        f'kube_deployment_status_replicas_ready'
        f'{{namespace="{APP_NAMESPACE}", deployment="{service}"}}'
    )
    if results and results[0].get("value"):
        try:
            return int(float(results[0]["value"][1]))
        except (ValueError, IndexError):
            return None
    return None


def get_hpa_max_replicas(service: str) -> int | None:
    """MaxReplicas configuré dans le HPA."""
    results = query_prometheus(
        f'kube_horizontalpodautoscaler_spec_max_replicas'
        f'{{namespace="{APP_NAMESPACE}", horizontalpodautoscaler="{service}"}}'
    )
    if results and results[0].get("value"):
        try:
            return int(float(results[0]["value"][1]))
        except (ValueError, IndexError):
            return None
    return None


def get_current_replicas(service: str) -> int | None:
    """Nombre de réplicas actuellement déployés."""
    results = query_prometheus(
        f'kube_deployment_spec_replicas'
        f'{{namespace="{APP_NAMESPACE}", deployment="{service}"}}'
    )
    if results and results[0].get("value"):
        try:
            return int(float(results[0]["value"][1]))
        except (ValueError, IndexError):
            return None
    return None


# ============================================================
# Logique de détection et proposition d'actions
# ============================================================
def check_anomaly_duration(service: str, is_anomaly: bool) -> bool:
    """
    Retourne True si l'anomalie dure depuis assez longtemps
    pour justifier une proposition d'action.
    """
    now = datetime.now(timezone.utc)
    if is_anomaly:
        if service not in anomaly_start:
            anomaly_start[service] = now
            return False
        elapsed = (now - anomaly_start[service]).total_seconds()
        return elapsed >= THRESHOLDS["anomaly_duration_s"]
    else:
        anomaly_start.pop(service, None)
        return False


def propose_action(action: str, target: str, reason: str,
                   details: dict | None = None) -> dict:
    """Construit et logue une proposition d'action structurée."""
    proposal = {
        "timestamp":  datetime.now(timezone.utc).isoformat(),
        "source":     "decideur-naif",
        "action":     action,
        "target":     target,
        "namespace":  APP_NAMESPACE,
        "reason":     reason,
        "details":    details or {},
    }
    # Log structuré — le moteur lira ce JSON depuis stdout/logs du pod
    print(f"ACTION_PROPOSAL:{json.dumps(proposal)}", flush=True)
    logger.info(json.dumps(
        f"Proposition → {action}({target}) : {reason}"
    ))
    last_action[target] = {"action": action, "timestamp": datetime.now(timezone.utc)}
    return proposal


def detect_cascade_db(metrics: dict) -> bool:
    """
    Détecte si plusieurs services sont dégradés simultanément
    (signe probable d'une cause racine commune comme Postgres).
    """
    degraded = sum(
        1 for svc, m in metrics.items()
        if m.get("latency_p95") and m["latency_p95"] > THRESHOLDS["latency_p95_seconds"]
    )
    return degraded >= THRESHOLDS["cascade_services_threshold"]


def analyze_service(service: str, metrics: dict) -> dict | None:
    """
    Analyse les métriques d'un service et retourne une proposition
    d'action si une anomalie est détectée depuis assez longtemps.
    """
    m = metrics[service]
    latency   = m.get("latency_p95")
    error_rate = m.get("error_rate_5xx")
    ready     = m.get("replicas_ready")
    current   = m.get("current_replicas")
    hpa_max   = m.get("hpa_max_replicas")

    # ────────────────────────────────────────────────────────
    # Règle 1 : Réplicas_Ready = 0 → Restart
    # ────────────────────────────────────────────────────────
    if ready is not None and ready == 0:
        if check_anomaly_duration(f"{service}_quorum", True):
            return propose_action(
                action="Restart",
                target=service,
                reason=f"Aucun réplica disponible (ready=0)",
                details={"replicas_ready": ready, "current_replicas": current}
            )
    else:
        check_anomaly_duration(f"{service}_quorum", False)

    # ────────────────────────────────────────────────────────
    # Règle 2 : Taux d'erreur 5xx > seuil → Rollback
    # (signe d'une régression après déploiement)
    # ────────────────────────────────────────────────────────
    if error_rate is not None and error_rate > THRESHOLDS["error_rate_5xx"]:
        if check_anomaly_duration(f"{service}_errors", True):
            return propose_action(
                action="Rollback",
                target=service,
                reason=f"Taux d'erreur 5xx élevé ({error_rate:.1%})",
                details={"error_rate_5xx": round(error_rate, 4)}
            )
    else:
        check_anomaly_duration(f"{service}_errors", False)

    # ────────────────────────────────────────────────────────
    # Règle 3 : Latence P95 > seuil → Restart
    # ────────────────────────────────────────────────────────
    if latency is not None and latency > THRESHOLDS["latency_p95_seconds"]:
        if check_anomaly_duration(f"{service}_latency", True):
            return propose_action(
                action="Restart",
                target=service,
                reason=f"Latence P95 élevée ({latency*1000:.0f}ms > "
                       f"{THRESHOLDS['latency_p95_seconds']*1000:.0f}ms)",
                details={"latency_p95_ms": round(latency * 1000, 1)}
            )
    else:
        check_anomaly_duration(f"{service}_latency", False)

    # ────────────────────────────────────────────────────────
    # Règle 4 : CPU saturé + HPA au max → ScaleUp (inutile mais naïf)
    # ────────────────────────────────────────────────────────
    if (current is not None and hpa_max is not None
            and current >= hpa_max
            and latency is not None
            and latency > THRESHOLDS["latency_p95_seconds"]):
        if check_anomaly_duration(f"{service}_cpu", True):
            return propose_action(
                action="ScaleUp",
                target=service,
                reason=f"Service saturé et HPA au maximum ({current}/{hpa_max} réplicas)",
                details={"current_replicas": current, "hpa_max": hpa_max}
            )
    else:
        check_anomaly_duration(f"{service}_cpu", False)

    # ────────────────────────────────────────────────────────
    # Règle 5 : Latence modérée → ScaleDown (logique naïve dangereuse)
    # ────────────────────────────────────────────────────────
    if (latency is not None
            and 0.1 < latency <= THRESHOLDS["latency_p95_seconds"]
            and current is not None and current > 1):
        if check_anomaly_duration(f"{service}_scaledown", True):
            return propose_action(
                action="ScaleDown",
                target=service,
                reason=f"Latence légèrement élevée ({latency*1000:.0f}ms), "
                       f"tentative de réduction de charge",
                details={"latency_p95_ms": round(latency * 1000, 1),
                         "current_replicas": current}
            )
    else:
        check_anomaly_duration(f"{service}_scaledown", False)

    return None


# ============================================================
# Boucle principale
# ============================================================
def main():
    logger.info(json.dumps(
        f"Démarrage du décideur naïf — polling toutes les "
        f"{POLL_INTERVAL_SECONDS}s, seuils: {THRESHOLDS}"
    ))

    while True:
        try:
            # Collecte des métriques pour tous les services
            metrics = {}
            for svc in SERVICES:
                metrics[svc] = {
                    "latency_p95":      get_latency_p95(svc),
                    "error_rate_5xx":   get_error_rate_5xx(svc),
                    "replicas_ready":   get_replicas_ready(svc),
                    "current_replicas": get_current_replicas(svc),
                    "hpa_max_replicas": get_hpa_max_replicas(svc),
                }

            logger.debug(json.dumps(f"Métriques collectées : {metrics}"))

            # Détection de cascade (scénario 7 : cause racine Postgres)
            if detect_cascade_db(metrics):
                degraded = [
                    s for s, m in metrics.items()
                    if m.get("latency_p95") and
                    m["latency_p95"] > THRESHOLDS["latency_p95_seconds"]
                ]
                propose_action(
                    action="InvestigateDB",
                    target="postgres",
                    reason=f"{len(degraded)} services dégradés simultanément "
                           f"({', '.join(degraded)}) — cause racine probable : Postgres",
                    details={"degraded_services": degraded}
                )
            else:
                # Analyse individuelle de chaque service
                for svc in SERVICES:
                    analyze_service(svc, metrics)

        except Exception as e:
            logger.error(json.dumps(f"Erreur inattendue dans la boucle : {e}"))

        time.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
