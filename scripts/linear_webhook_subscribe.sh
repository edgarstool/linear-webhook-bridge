#!/usr/bin/env bash
set -euo pipefail

# linear_webhook_subscribe.sh
# Subscribe / verify a Linear webhook endpoint for the linear-webhook-bridge.
# Idempotent: if the same URL is already registered, it will verify and exit 0.
# Final deliverable: script artifact for the linear-webhook-bridge skill repo.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
DEFAULT_CONFIG="$PROJECT_ROOT/templates/route.yaml"
DEFAULT_ENV_FILE="$PROJECT_ROOT/.env"

log()  { printf '[subscribe] %s\n' "$*"; }
die()  { log "FATAL: $*"; exit 1; }

# ── Defaults ──────────────────────────────────────────────────────────
WEBHOOK_URL="${WEBHOOK_URL:-}"
LINEAR_API_KEY="${LINEAR_API_KEY:-}"
TEAM_ID="${TEAM_ID:-}"
ENV_FILE="${ENV_FILE:-$DEFAULT_ENV_FILE}"
CONFIG_FILE="${CONFIG_FILE:-$DEFAULT_CONFIG}"
QUIET="${QUIET:-0}"

# Event types we care about for the 4-layer bridge.
DEFAULT_EVENTS=(
    Issue:created
    Issue:updated
    Issue:deleted
    Comment:created
)

# ── Helpers ───────────────────────────────────────────────────────────
usage() {
    cat <<'EOF'
Usage: linear_webhook_subscribe.sh [OPTIONS]

Subscribes the given HTTPS endpoint to Linear webhook events.
Idempotent: existing subscriptions are re-verified.

Options:
  -u, --url URL           Webhook endpoint URL (required)
  -k, --api-key KEY       Linear personal API key (or set LINEAR_API_KEY)
  -t, --team-id ID        Linear team identifier (optional)
  -e, --events LIST       Comma-separated event types (default: Issue:created,Issue:updated,Issue:deleted,Comment:created)
  -c, --config FILE       Route config template path (default: templates/route.yaml)
      --env-file FILE     Env file to source (default: .env)
  -q, --quiet             Reduce output
  -h, --help              Show this help

Environment:
  WEBHOOK_URL, LINEAR_API_KEY, TEAM_ID, CONFIG_FILE, ENV_FILE
EOF
    exit 0
}

parse_args() {
    EVENTS_CSV=""
    while [[ $# -gt 0 ]]; do
        case "$1" in
            -u|--url)      WEBHOOK_URL="$2"; shift 2 ;;
            -k|--api-key)  LINEAR_API_KEY="$2"; shift 2 ;;
            -t|--team-id)  TEAM_ID="$2"; shift 2 ;;
            -e|--events)   EVENTS_CSV="$2"; shift 2 ;;
            -c|--config)   CONFIG_FILE="$2"; shift 2 ;;
            --env-file)    ENV_FILE="$2"; shift 2 ;;
            -q|--quiet)    QUIET=1; shift ;;
            -h|--help)     usage ;;
            *)             die "Unknown option: $1" ;;
        esac
    done

    # Reconstruct events array from CSV if provided.
    if [[ -n "${EVENTS_CSV:-}" ]]; then
        IFS=',' read -r -a DEFAULT_EVENTS <<< "$EVENTS_CSV"
    fi
}

load_env() {
    if [[ -f "$ENV_FILE" ]]; then
        # shellcheck source=/dev/null
        source "$ENV_FILE"
        log "Loaded env from $ENV_FILE"
    fi
}

check_deps() {
    local missing=()
    for cmd in curl jq; do
        if ! command -v "$cmd" >/dev/null 2>&1; then
            missing+=("$cmd")
        fi
    done
    if [[ ${#missing[@]} -gt 0 ]]; then
        die "Missing required commands: ${missing[*]}"
    fi
}

linear_graphql() {
    local query="$1"
    local variables="${2:-{}}"

    curl --silent --show-error --fail \
        -X POST \
        -H "Content-Type: application/json" \
        -H "Authorization: $LINEAR_API_KEY" \
        -d "$(jq -n --arg q "$query" --argjson v "$variables" '{query: $q, variables: $v}')" \
        https://api.linear.app/graphql
}

find_existing_webhook() {
    local url="$1"
    local query
    query=$(cat <<'GRAPHQL'
query($first: Int) {
  webhooks(first: $first) {
    nodes {
      id
      url
      teamId
      team {
        key
        name
      }
      enabled
      createdAt
    }
  }
}
GRAPHQL
)

    linear_graphql "$query" '{"first": 100}' |
        jq -r --arg url "$url" '
          .data.webhooks.nodes[]
          | select(.url == $url)
          | {id, teamId, teamKey: .team.key, enabled}
        '
}

create_webhook() {
    local url="$1"
    local team_id="${2:-null}"
    local events_json
    events_json=$(printf '%s\n' "${DEFAULT_EVENTS[@]}" | jq -R . | jq -s .)

    local query
    query=$(cat <<GRAPHQL
mutation(\$url: String!, \$teamId: String, \$events: [WebhookEventType!]!) {
  webhookCreate(input: {url: \$url, teamId: \$teamId, events: \$events}) {
    success
    webhook {
      id
      url
      teamId
      enabled
      createdAt
    }
  }
}
GRAPHQL
)

    local resp
    resp=$(linear_graphql "$query" \
        "$(jq -n \
            --arg url "$url" \
            --argjson teamId "${team_id:-null}" \
            --argjson events "$events_json" \
            '{url: $url, teamId: $teamId, events: $events}')")

    echo "$resp" | jq '.data.webhookCreate'
}

enable_webhook() {
    local webhook_id="$1"
    local query
    query=$(cat <<'GRAPHQL'
mutation($id: String!) {
  webhookUpdate(id: $id, input: {enabled: true}) {
    success
    webhook { id enabled }
  }
}
GRAPHQL
)
    linear_graphql "$query" "{\"id\": \"$webhook_id\"}"
}

print_facts() {
    jq -n \
        --arg url       "$WEBHOOK_URL" \
        --arg webhookId "${WEBHOOK_ID:-null}" \
        --argjson events "$(printf '%s\n' "${DEFAULT_EVENTS[@]}" | jq -R . | jq -s .)" \
        --arg status    "${OP_STATUS:-verified}" \
        --arg config    "$CONFIG_FILE" \
        '{
            webhook_url: $url,
            webhook_id: $webhookId,
            events: $events,
            status: $status,
            config_template: $config
        }'
}

# ── Main ──────────────────────────────────────────────────────────────
main() {
    parse_args "$@"
    load_env
    check_deps

    [[ -z "$WEBHOOK_URL" ]] && die "WEBHOOK_URL is required (pass -u or set WEBHOOK_URL env)"
    [[ -z "$LINEAR_API_KEY" ]] && die "LINEAR_API_KEY is required (pass -k or set LINEAR_API_KEY env)"

    log "Target webhook URL: $WEBHOOK_URL"
    log "Event types: ${DEFAULT_EVENTS[*]}"

    # 1) Check existing subscription.
    existing=$(find_existing_webhook "$WEBHOOK_URL" || true)

    if [[ -n "$existing" ]]; then
        WEBHOOK_ID=$(echo "$existing" | jq -r '.id')
        team_id=$(echo "$existing" | jq -r '.teamId // empty')
        enabled=$(echo "$existing" | jq -r '.enabled')

        log "Existing webhook found: $WEBHOOK_ID (team=$team_id enabled=$enabled)"
        if [[ "$enabled" == "false" ]]; then
            enable_webhook "$WEBHOOK_ID"
            log "Re-enabled existing webhook $WEBHOOK_ID"
        fi
        OP_STATUS="verified"
    else
        # 2) Create new subscription.
        log "Creating new webhook subscription..."
        create_resp=$(create_webhook "$WEBHOOK_URL" "${TEAM_ID:-}")
        WEBHOOK_ID=$(echo "$create_resp" | jq -r '.webhook.id // empty')
        [[ -z "$WEBHOOK_ID" ]] && die "Webhook creation failed: $(echo "$create_resp" | jq -c '.')"

        log "Created webhook: $WEBHOOK_ID"
        OP_STATUS="created"
    fi

    # 3) Output machine-readable facts.
    if [[ "$QUIET" -eq 1 ]]; then
        print_facts
    else
        echo ""
        echo "=== linear-webhook-bridge subscription result ==="
        print_facts
        echo "================================================"
    fi
}

main "$@"
