#!/bin/sh
# Pull one service's secrets from Vault into /vault/secrets. This is the SINGLE
# SOURCE for both deployment forms: compose bind-mounts it as the vault-init
# sidecar, and the k8s chart embeds a byte-identical copy (deploy/helm/radar/
# files/fetch-secrets.sh, via .Files.Get) into a ConfigMap for the Vault
# init-container. A drift test pins the two copies together
# (tests/deploy/test_config_copies_byte_identical.py).
#
# It writes EVERY field at the service's Vault path (mirroring `make agent-secrets`),
# so a newly minted field needs no edit here.
#
# Auth is auto-detected by environment:
#   - compose passes the dev root token via VAULT_TOKEN.
#   - k8s has no shared token: with VAULT_TOKEN unset, authenticate with the pod
#     ServiceAccount via the kubernetes auth method and role radar-<service>.
# The Postgres DSN host is rewritten to $RADAR_POSTGRES_HOST (default: postgres,
# the compose service name; k8s sets the radar-infra FQDN).
# Usage: fetch-secrets.sh <service>
set -eu

SERVICE="${1:?usage: fetch-secrets.sh <service>}"
: "${VAULT_ADDR:?}"
OUT=/vault/secrets
mkdir -p "$OUT"

# Vault may still be coming up (a separate compose project, or a starting pod):
# wait rather than race. `vault status` needs no auth.
n=0
until vault status >/dev/null 2>&1; do
    n=$((n + 1))
    [ "$n" -gt 60 ] && { echo "vault-init($SERVICE): Vault unreachable at $VAULT_ADDR" >&2; exit 1; }
    sleep 1
done

# With no VAULT_TOKEN (k8s), log in via the kubernetes auth method using the pod
# ServiceAccount JWT and the per-service role. Compose sets VAULT_TOKEN, so this
# branch is skipped there and the dev root token is used as-is.
if [ -z "${VAULT_TOKEN:-}" ]; then
    VAULT_TOKEN="$(vault write -field=token auth/kubernetes/login \
        role="radar-$SERVICE" \
        jwt="$(cat /var/run/secrets/kubernetes.io/serviceaccount/token)")"
    export VAULT_TOKEN
    echo "vault-init($SERVICE): authenticated via kubernetes role radar-$SERVICE"
fi

# Write every data field at <path> to its own file. The kv-v2 read wraps the
# secret under .data.data; the names below are the response/metadata envelope,
# filtered out so only real fields remain.
pull_all() {
    path="$1"
    fields=$(vault kv get -format=json "$path" |
        grep -oE '"[A-Za-z0-9_]+"[[:space:]]*:' |
        sed -E 's/"([A-Za-z0-9_]+)".*/\1/' |
        grep -vxE 'request_id|lease_id|lease_duration|renewable|data|metadata|created_time|custom_metadata|deletion_time|destroyed|version|warnings|mount_type|auth|wrap_info' |
        sort -u)
    [ -n "$fields" ] || { echo "vault-init($SERVICE): no fields at $path" >&2; exit 1; }
    for f in $fields; do
        vault kv get -field="$f" "$path" >"$OUT/$f"
        echo "vault-init($SERVICE): wrote $f"
    done
}

# postgres_dsn is shared (secret/radar/postgres) and its host is rewritten from
# the native-dev value to the deployment's Postgres address — a network remap, not
# a credential change.
dsn() {
    host="${RADAR_POSTGRES_HOST:-postgres}"
    vault kv get -field=postgres_dsn secret/radar/postgres |
        sed -e "s#@localhost:#@${host}:#" -e "s#@127\.0\.0\.1:#@${host}:#" >"$OUT/postgres_dsn"
    echo "vault-init($SERVICE): wrote postgres_dsn (host -> ${host})"
}

case "$SERVICE" in
    llm-gateway)
        pull_all secret/radar/llm          # provider api keys
        pull_all secret/radar/llm-gateway  # gateway_tokens (+ any gateway fields)
        ;;
    ingestion)
        pull_all secret/radar/ingestion    # webhook_token_* per source
        dsn
        ;;
    watcher-agent | planner-agent | reasoner-agent | outbox-worker | knowledge-service | feedback-service)
        pull_all "secret/radar/$SERVICE"   # agent_token, gateway_token(s), dispatch/knowledge/slack…
        dsn
        ;;
    *)
        echo "vault-init: unknown service '$SERVICE'" >&2
        exit 1
        ;;
esac

echo "vault-init($SERVICE): done"
