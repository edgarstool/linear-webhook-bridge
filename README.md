# linear-webhook-bridge

> Hermes skill: bridge Linear webhooks into Hermes agent sessions.
> Four-layer pipeline `Linear webhook → session mapper → agent runner → Linear API writeback`.

[![Version](https://img.shields.io/badge/version-1.0.0-blue.svg)](./CHANGELOG.md)
[![License](https://img.shields.io/badge/license-MIT-green.svg)](./LICENSE)
[![Platform](https://img.shields.io/badge/platform-linux%20%7C%20macos%20%7C%20windows-lightgrey.svg)]()
[![Hermes Skill](https://img.shields.io/badge/hermes-skill-purple.svg)]()
[![Built by](https://img.shields.io/badge/built%20by-kanban%20swarm-orange.svg)]()

## What it does

When someone @mentions or comments on a Linear issue, this bridge:

1. **Receives** the Linear webhook (HTTPS POST, `Linear-Signature` HMAC)
2. **Verifies** signature + timestamp freshness, dedupes by `Linear-Delivery`
3. **Resolves** the issue to a deterministic Hermes session: `linear_{workspace}_{issue_identifier}`
4. **Fast-acks** with 200 OK in <100ms, queues the work
5. **Spawns** a worker that runs `hermes --resume <session>` async
6. **Writes back** the agent's output as a Linear comment (idempotent via marker)

End result: Linear becomes a thread-style UI for long-running Hermes agent sessions.

## Quick start

```bash
# 1. Install the skill
ln -sf "$(pwd)/SKILL.md" ~/.hermes/profiles/default/skills/linear-webhook-bridge.md
hermes skills reload

# 2. Subscribe a Linear webhook
export LINEAR_API_KEY="lin_api_..."
export WEBHOOK_URL="https://your-host:8644/webhooks/linear"
export WEBHOOK_SECRET="$(openssl rand -hex 32)"
bash scripts/linear_webhook_subscribe.sh

# 3. Configure the route
cp templates/route.yaml ~/.hermes/config/routes/linear-webhook-bridge.yaml
# edit secrets, model defaults, rate limits

# 4. Verify ingress works
hermes webhook test linear-webhook-bridge --payload '{"type":"Issue","action":"create","data":{...}}'
```

Full installation steps in [`SKILL.md`](./SKILL.md).

## Repo structure

```
linear-webhook-bridge/
├── SKILL.md                              # ⭐ Hermes skill spec (forge, 418 lines)
├── README.md                             # this file
├── CHANGELOG.md                          # release history
├── LICENSE                               # MIT
├── .gitignore
├── scripts/
│   └── linear_webhook_subscribe.sh       # idempotent GraphQL webhook create/verify
├── templates/
│   └── route.yaml                        # pipeline topology + routing rules
└── docs/
    └── risk-analysis.md                  # security / idempotency / session-mapping (oracle)
```

## Four-layer pipeline

```
Layer 1  Linear webhook ingress      ← HTTPS POST, HMAC, idempotency, fast-ack
Layer 2  Hermes session mapper       ← issue.identifier → linear_{ws}_{id}
Layer 3  agent runner                ← `hermes --resume <sid>` (async worker)
Layer 4  Linear API writeback        ← commentCreate / agentActivityCreate
```

Critical timing: **layer 1 must respond 200 within 5 s** (Linear's hard timeout). The agent run is always slower than that, so it MUST go through a queue.

## Three-layer dedupe

| Layer | Key | Catches |
|---|---|---|
| L1 | `Linear-Delivery` UUID | Linear webhook retries |
| L2 | `(issueId, action, contentHash)` | Multiple workers pulling the same job |
| L3 | `<!-- hermes:run=<run_id> -->` HTML comment marker | Bot self-loops on `Comment:created` |

## Configuration

| Variable | Purpose |
|---|---|
| `LINEAR_API_KEY` | Personal API key or OAuth token (for webhook create + GraphQL writeback) |
| `WEBHOOK_SECRET` | HMAC shared secret (set when subscribing) |
| `WEBHOOK_URL` | Public HTTPS endpoint (the Hermes gateway exposes `/webhooks/<name>` on port 8644) |
| `HERMES_BOT_ACTOR_ID` | The bridge's own Linear actor ID — added to `actor_id_blacklist` to prevent feedback loops |
| `DEFAULT_MODEL` | Fallback model when an issue doesn't pin one |

Full list and tuning notes in [`templates/route.yaml`](./templates/route.yaml) and [`SKILL.md` § Configuration](./SKILL.md#configuration).

## Risks & mitigations

See [`docs/risk-analysis.md`](./docs/risk-analysis.md) for a deep dive (oracle's analysis, ~350 lines).

Top three to know:

1. **Webhook signature must be over raw body** — re-serializing the JSON before HMAC will fail verification. Use `hmac.compare_digest()`.
2. **Don't `subprocess.run("hermes ...")` inside the webhook handler** — Linear's 5 s timeout will fire. Always enqueue.
3. **Rate-limit the writeback** — Linear's GraphQL has request + complexity quotas. Watch `X-RateLimit-Requests-Remaining`.

## How this repo was built

Built via `hermes kanban swarm` — parallel multi-profile orchestration:

| Worker | Profile | Output |
|---|---|---|
| Core spec | `forge` | SKILL.md |
| Risk analysis | `oracle` | docs/risk-analysis.md |
| Project report | `chronicler` | (filed to Obsidian) |
| Scripts + templates | `default` | scripts/, templates/ |
| Verification | `sentinel` | gating before synthesis |
| Synthesis | `quartermaster` | this repo |

This is the second skill packaged this way (after [`deploy-pilot`](https://github.com/Edgars-tool/deploy-pilot)).

## Version

Current **v1.0.0**. See [CHANGELOG.md](./CHANGELOG.md).

## License

MIT — see [LICENSE](./LICENSE).
