# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.0.0] - 2026-06-21

### Added
- Initial release of the `linear-webhook-bridge` Hermes skill
- Four-layer pipeline spec: Linear webhook → Hermes session mapper → agent runner → Linear API writeback
- HMAC-SHA256 signature verification (Linear-Signature header, raw body)
- Anti-replay timestamp window (60 s, configurable via `freshness_window_ms`)
- Fast-ack ingress pattern (≤100 ms 200 OK; agent runs in worker queue)
- Bot actor blacklist (`HERMES_BOT_ACTOR_ID`) to prevent feedback loops
- Three-layer dedupe strategy:
  - L1: `Linear-Delivery` UUID
  - L2: `(issueId, action, contentHash)`
  - L3: `<!-- hermes:run=<run_id> -->` comment marker
- Deterministic session ID convention: `linear_{workspace_slug}_{issue_identifier}`
- `scripts/linear_webhook_subscribe.sh` — idempotent webhook create/verify via Linear GraphQL
- `templates/route.yaml` — pipeline topology, session templates, agent runners, writeback templates, retry, observability
- `docs/risk-analysis.md` — security / idempotency / session mapping risk analysis (oracle)

### Built by
- **forge** (claude-opus-4-7) — SKILL.md spec
- **oracle** (claude-opus-4-7) — risk analysis
- **chronicler** (gpt-5.4) — Obsidian project report
- **default** (stepfun/step-3.7-flash) — scripts and templates
- **sentinel** (minimax/minimax-m2.5) — verification
- **quartermaster** (stepfun/step-3.7-flash) — synthesis & repo packaging

Built via `hermes kanban swarm` parallel multi-profile orchestration.

[1.0.0]: https://github.com/Edgars-tool/linear-webhook-bridge/releases/tag/v1.0.0
