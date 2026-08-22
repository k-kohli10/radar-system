#!/bin/sh
# Pull one service's secrets from the dev Vault into /vault/secrets — the compose
# form of the k8s Vault init-container. It writes EVERY field at the service's
# Vault path (mirroring `make agent-secrets`), so a new minted field needs no edit
# here. Unlike k8s (kubernetes auth) the dev Vault uses the root token.
# Usage: fetch-secrets.sh <service>
set -eu

SERVICE="${1:?usage: fetch-secrets.sh <service>}"
: "${VAULT_ADDR:?}" "${VAULT_TOKEN:?}"
OUT=/vault/secrets
mkdir -p "$OUT"

# Separate compose project from Vault, so no depends_on: wait rather than race.
n=0
until vault status >/dev/null 2>&1; do
    n=$((n + 1))
    [ "$n" -gt 60 ] && { echo "vault-init($SERVICE): Vault unreachable at $VAULT_ADDR" >&2; exit 1; }
    sleep 1
done

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
# the native-dev value to the compose service name — a network remap, not a
# credential change.
dsn() {
    vault kv get -field=postgres_dsn secret/radar/postgres |
        sed -e 's#@localhost:#@postgres:#' -e 's#@127\.0\.0\.1:#@postgres:#' >"$OUT/postgres_dsn"
    echo "vault-init($SERVICE): wrote postgres_dsn (host -> postgres)"
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
