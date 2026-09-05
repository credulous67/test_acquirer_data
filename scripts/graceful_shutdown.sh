#!/usr/bin/env bash
# Stops the simulation in an order that leaves it cleanly resumable:
#   1. Stop merchant-simulator first, so no new authorizations start.
#      (It also drains its own already-in-flight sends before exiting --
#      see services/merchant_simulator/app.py.)
#   2. Wait for the gateway/issuer to finish everything already in flight
#      (poll the dashboard's "outstanding" count via GET /api/stats).
#   3. Only then stop gateway, issuer-simulator, dashboard and postgres --
#      each was already given a chance to finish its own work via
#      services/common/util.serve_until_signal.
#
# Named volumes (seed-data, pgdata) are untouched, so `podman-compose up`
# or `podman-compose start` afterwards resumes with the same seed data,
# the same database contents, and no authorizations stuck mid-flight.
#
# Usage: scripts/graceful_shutdown.sh [--down]
#   --down   remove the containers (podman-compose down) instead of just
#            stopping them (podman-compose stop). Volumes are kept either
#            way; only pass this if you also intend to rebuild/replace
#            containers before the next `up`.
set -euo pipefail

COMPOSE=${COMPOSE_CMD:-podman-compose}
DASHBOARD_URL=${DASHBOARD_URL:-http://localhost:8080}
DRAIN_TIMEOUT=${DRAIN_TIMEOUT:-120}
FINAL_ACTION="stop"
[ "${1:-}" = "--down" ] && FINAL_ACTION="down"

echo "==> Stopping merchant-simulator (no new authorizations will be originated)"
$COMPOSE stop merchant-simulator

echo "==> Draining authorizations already in flight (gateway/issuer/dashboard stay up)"
waited=0
while true; do
    outstanding=$(curl -s --max-time 3 "$DASHBOARD_URL/api/stats" \
        | python3 -c 'import sys, json; print(json.load(sys.stdin).get("outstanding", "?"))' 2>/dev/null || echo "?")
    echo "    outstanding: ${outstanding}"

    if [ "$outstanding" = "0" ]; then
        echo "    drained."
        break
    fi
    if [ "$waited" -ge "$DRAIN_TIMEOUT" ]; then
        echo "    drain timeout (${DRAIN_TIMEOUT}s) reached -- if the issuer is paused this is"
        echo "    expected (paused transactions are held open on purpose); proceeding anyway."
        break
    fi
    sleep 2
    waited=$((waited + 2))
done

echo "==> Stopping gateway, issuer-simulator, dashboard, postgres"
if [ "$FINAL_ACTION" = "down" ]; then
    $COMPOSE down
else
    $COMPOSE stop gateway issuer-simulator dashboard postgres
fi

if [ "$FINAL_ACTION" = "down" ]; then
    echo "==> Done. Resume with: $COMPOSE up -d"
else
    echo "==> Done. Resume with: $COMPOSE start"
fi
