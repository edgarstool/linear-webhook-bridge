---
name: linear-webhook-bridge
description: Use when bridging Linear webhooks into Hermes agent sessions. Covers the 4-layer pipeline (Linear webhook → Hermes session mapper → agent runner → Linear API writeback) with HMAC-SHA256 signature verification, fast-ack queue pattern, deterministic `linear_{workspace}_{issue}` session IDs, three-layer dedupe (Linear-Delivery / action-hash / comment marker), and Linear API rate-limit handling.
version: 1.0.0
author: Hermes Agent
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [linear, webhook, bridge, hermes-agent, integration, hmac, idempotency, session-mapping]
    related_skills: [hermes-agent, kanban-worker, github-pr-workflow]
---

# Linear Webhook Bridge

## Overview

`linear-webhook-bridge` 把 Linear 的 webhook 事件接進 Hermes，讓每個 issue / comment 對應一條長壽 Hermes session：使用者在 Linear 留言 → bridge 收到 webhook → 啟動 / resume 對應 session → agent 跑完後把 comment / activity 回寫到 Linear。

整條路徑分四層，每層責任邊界清楚、可單獨替換：

```
Layer 1  Linear webhook ingress      ← HTTPS POST，驗章 + 冪等 + fast-ack
Layer 2  Hermes session mapper       ← issue.identifier → linear_{ws}_{id}
Layer 3  agent runner                ← `hermes --resume <sid>` 異步執行
Layer 4  Linear API writeback        ← commentCreate / agentActivityCreate
```

設計目標：
- **5 秒內回 200 OK**：Linear 5s timeout，超時必觸發重送，所以 spawn agent 一定要異步。
- **冪等**：Linear retry + bot 自迴圈 + 多 worker 競態都不能造成「同一句話被回兩次」。
- **可恢復**：Hermes session 落地 SQLite，`hermes --resume <sid>` 可從終端接手該 issue。
- **安全**：HMAC-SHA256 over raw body + 5 分鐘 freshness window + actor 黑名單。

## When to Use

Use when:
- 你要做「Linear issue 上 @mention 觸發 agent」這類整合，需要 webhook ingress + agent runner。
- 你想把 Linear 變成 Hermes 的工單 UI：人在 Linear 留言、Hermes 在 Linear 回覆，全流程不離開瀏覽器。
- 你要把已存在的 Hermes profile / skill 接上 Linear 而非重寫一套 agent。
- 你在維運此 skill：補簽章驗章、補冪等鍵、調整 session 命名、加新事件 type。

Don't use for:
- **單向通知**（Hermes → Linear 一次性留言），用 Linear SDK 直接打 GraphQL 比較簡單。
- **Linear → Slack / Discord 純轉發**，不需要 Hermes session 的場景。
- **Linear OAuth app 開發本身** — 那是 Linear 官方 SDK 的事，這個 skill 假設你已有 personal API key 或 OAuth token。

## 4-Layer Pipeline (full picture)

```
[Linear]                        [Edge / Bridge]                   [Hermes core]                 [Linear]
   │                                  │                                  │                          │
   │  POST /linear/webhook            │                                  │                          │
   │  Linear-Signature: <hex>         │                                  │                          │
   │  Linear-Delivery: <uuid>         │                                  │                          │
   │  Linear-Event: Issue|Comment     │                                  │                          │
   │  body: {action, type, data, ...} │                                  │                          │
   ├─────────────────────────────────►│                                  │                          │
   │                                  │ 1. verify HMAC-SHA256 (raw body) │                          │
   │                                  │ 2. webhookTimestamp ≤ 5min       │                          │
   │                                  │ 3. dedupe by Linear-Delivery     │                          │
   │                                  │ 4. resolve session_id            │                          │
   │                                  │ 5. enqueue + 200 OK fast-ack ◄───┤  ≤ 5000 ms 必須 ack 完   │
   │                                  │                                  │                          │
   │                                  │ 6. worker dequeues               │                          │
   │                                  ├─────────────────────────────────►│                          │
   │                                  │                                  │ 7. hermes --resume <sid> │
   │                                  │                                  │ 8. commentCreate ────────┤
   │                                  │                                  │                          │
```

關鍵時序：**第 5 步必須在 5 秒內**送出 200 OK。所以 webhook handler 自己 **不能** `subprocess.run("hermes ...")`，一定要 enqueue + 獨立 worker。

## Conventions

### Session ID 命名（決定性）

```
session_id = f"linear_{workspace_slug}_{issue_identifier}"
# 例：linear_acme_ENG-1423
```

理由：
- **Deterministic**：同 issue 永遠對到同 session_id，bridge 不需要查表。
- **可讀**：`hermes sessions list` 一眼看出這條 session 屬於哪個 issue。
- **不撞預設格式**：Hermes 預設 session_id 是 `YYYYMMDD_HHMMSS_<short>`（見 `agent_init.py:1038`），加 `linear_` 前綴零碰撞。
- **Workspace 隔離**：`workspace_slug` 防多組織共用同一個 Hermes home 時碰撞。

雖然 ID 是決定性的，仍建議落一張 `linear_session_map` 表做反查、清理、黑名單（見 §Pitfalls）。

### Webhook URL 路徑

```
POST https://<your-host>/linear/<workspace_slug>/webhook
```

路徑帶 `workspace_slug` 是為了讓 verify 時做雙重比對：先驗 HMAC，再比 `payload.data.organizationId` 是否屬於該 workspace。誤接到別組織直接 401。

### Secret 存放

- **不進 git、不進 SKILL.md**。
- 推薦：`~/.hermes/secrets/linear_webhook.secret`（mode 600）。
- 或環境變數：`HERMES_LINEAR_WEBHOOK_SECRET`，bridge 啟動時讀取。
- Secret 是 webhook 建立時 Linear 給的；scripts/linear_webhook_subscribe.sh 跑完會把 secret 印在 stdout，務必導向安全位置不要留在 shell history。

### Bot Actor 黑名單

Bot 自留 comment 也會觸發 webhook，導致無窮迴圈。第一次 bot 留言後抓 `actor.id` 存成 `HERMES_BOT_ACTOR_ID`，bridge 入口 short-circuit：

```python
if payload.actor.id == HERMES_BOT_ACTOR_ID:
    return 200, "ack but skip"
```

## Webhook Handler Reference

### Signature verification（Python，raw body + constant-time compare）

```python
import hmac
import hashlib
import json
import time
from typing import Tuple

FRESHNESS_WINDOW_MS = 5 * 60 * 1000  # 5 分鐘

def verify_linear_webhook(
    raw_body: bytes,
    headers: dict,
    secret: str,
    now_ms: int | None = None,
) -> Tuple[bool, str]:
    """
    回傳 (ok, reason)。raw_body 必須是收到的原始 bytes，**不可** 被 JSON.parse + re-serialize 過。
    """
    sig_header = headers.get("linear-signature") or headers.get("Linear-Signature")
    if not sig_header:
        return False, "missing signature header"

    expected = hmac.new(
        secret.encode("utf-8"),
        raw_body,
        hashlib.sha256,
    ).hexdigest()

    if not hmac.compare_digest(expected, sig_header):
        return False, "signature mismatch"

    # webhookTimestamp 在 body 內，簽完章後不可被改
    try:
        payload = json.loads(raw_body)
    except Exception:
        return False, "body not json after signature ok"

    ts = payload.get("webhookTimestamp")
    if not isinstance(ts, int):
        return False, "missing webhookTimestamp"

    now_ms = now_ms or int(time.time() * 1000)
    if abs(now_ms - ts) > FRESHNESS_WINDOW_MS:
        return False, "timestamp outside freshness window"

    return True, "ok"
```

完整版見 `scripts/verify_signature.py`。

### Fast-ack handler（FastAPI，`< 100ms` 回 200）

```python
from fastapi import FastAPI, Request, HTTPException
import sqlite3
import os

app = FastAPI()
SECRET = open(os.environ["HERMES_LINEAR_WEBHOOK_SECRET_FILE"]).read().strip()
WORKSPACE_TO_ORG = {"acme": "org-uuid-..."}
DB = sqlite3.connect("bridge.sqlite", check_same_thread=False)
DB.execute("""
    CREATE TABLE IF NOT EXISTS deliveries(
      delivery_id TEXT PRIMARY KEY, received_at INT, status TEXT
    )
""")
DB.execute("""
    CREATE TABLE IF NOT EXISTS jobs(
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      delivery_id TEXT, payload TEXT, status TEXT DEFAULT 'queued',
      created_at INT
    )
""")

@app.post("/linear/{workspace_slug}/webhook")
async def ingress(workspace_slug: str, request: Request):
    raw = await request.body()                             # ← raw body MUST come before .json()
    ok, reason = verify_linear_webhook(raw, dict(request.headers), SECRET)
    if not ok:
        raise HTTPException(status_code=401, detail=reason)

    delivery_id = request.headers.get("linear-delivery")
    payload = await request.json()

    # Workspace 雙重檢查
    if payload.get("organizationId") and \
       payload["organizationId"] != WORKSPACE_TO_ORG[workspace_slug]:
        raise HTTPException(status_code=401, detail="org mismatch")

    # Bot 自迴圈防護
    actor_id = (payload.get("actor") or {}).get("id")
    if actor_id == os.environ.get("HERMES_BOT_ACTOR_ID"):
        return {"ok": True, "skipped": "bot self-event"}

    # L1 dedupe：Linear-Delivery
    cur = DB.execute(
        "INSERT OR IGNORE INTO deliveries(delivery_id, received_at, status) VALUES (?, ?, 'received')",
        (delivery_id, int(time.time())),
    )
    if cur.rowcount == 0:
        return {"ok": True, "skipped": "duplicate delivery"}

    # Enqueue → 不在 handler 裡 spawn agent
    DB.execute(
        "INSERT INTO jobs(delivery_id, payload, created_at) VALUES (?, ?, ?)",
        (delivery_id, json.dumps(payload), int(time.time())),
    )
    DB.commit()
    return {"ok": True}                                    # ← 整段 < 100ms
```

### 獨立 worker process（spawn Hermes）

```python
def worker_loop():
    while True:
        row = DB.execute(
            "SELECT id, payload FROM jobs WHERE status='queued' ORDER BY id LIMIT 1"
        ).fetchone()
        if not row:
            time.sleep(0.5)
            continue

        job_id, payload_json = row
        payload = json.loads(payload_json)
        sid = f"linear_{payload['organizationSlug']}_{payload['data']['identifier']}"
        is_new = not session_exists(sid)

        DB.execute("UPDATE jobs SET status='running' WHERE id=?", (job_id,))
        DB.commit()

        try:
            prompt = render_prompt(payload, is_new=is_new)
            cmd = ["hermes", "--headless", "-p", prompt]
            cmd += ["--session-id", sid] if is_new else ["--resume", sid]
            result = subprocess.run(cmd, capture_output=True, timeout=600, text=True)

            comment_body = extract_assistant_message(result.stdout)
            run_id = extract_run_id(result.stdout)
            post_linear_comment(payload["data"]["id"], comment_body, run_id=run_id)

            DB.execute("UPDATE jobs SET status='done' WHERE id=?", (job_id,))
        except Exception as e:
            DB.execute("UPDATE jobs SET status='failed' WHERE id=?", (job_id,))
            log_dead_letter(job_id, e)
        finally:
            DB.commit()
```

## Three-Layer Dedupe

| Layer | Key | TTL | 目的 |
| --- | --- | --- | --- |
| L1 ingress | `Linear-Delivery` (UUIDv4 header) | 7 天 | 同 delivery 重送直接 200 |
| L2 業務動作 | `(issueId, action, contentHash)` | 24 小時 | Linear 偶把 `update` 連發兩次 |
| L3 回寫 | `<!-- hermes:run=<run_id> -->` 標記嵌入 comment body | 永久 | 避免重跑留兩條 comment |

> **重要事實**：Linear GraphQL **沒有** Stripe-style `Idempotency-Key` header。L3 必須自己做：mutation 前先 `comments(first:50, filter:{...})` 查 marker，有就 update 而非 create。

## Linear API Writeback

### commentCreate（含 hermes 標記）

```python
import requests

LINEAR_API = "https://api.linear.app/graphql"

def post_linear_comment(issue_id: str, body: str, run_id: str):
    body_with_marker = f"<!-- hermes:run={run_id} -->\n{body}"
    query = """
    mutation($input: CommentCreateInput!) {
      commentCreate(input: $input) { success comment { id } }
    }
    """
    variables = {"input": {"issueId": issue_id, "body": body_with_marker}}
    return _call_linear(query, variables)
```

### Rate-limit headers（必監控）

| Header | 行動 |
| --- | --- |
| `X-RateLimit-Requests-Remaining` < 10% | 暫停非必要查詢 |
| `X-RateLimit-Complexity-Remaining` < 10% | 進入 backoff |
| HTTP 400 + `RATELIMITED` | 退到 `X-RateLimit-Requests-Reset` 後再試，重試上限 3 |

### 限額對照（API key 認證）

| 維度 | 上限 |
| --- | --- |
| 請求數 / 小時 | 2,500 |
| 複雜度 / 小時 | 3,000,000 |
| 單查詢複雜度 | 10,000 |

> **必做**：所有 list 查詢加 `first:` 參數，`comments` 預設 50 太貴；建議 `comments(first:10)`。

## Hermes Integration Recipes

### One-shot：訂閱 webhook + 啟動 bridge

```bash
# 1. 訂閱 Linear webhook（idempotent）
./scripts/linear_webhook_subscribe.sh \
    -u https://bridge.example.com/linear/acme/webhook \
    -k "$LINEAR_API_KEY" \
    -t "$LINEAR_TEAM_ID"
# stdout 會吐 JSON facts：{webhook_url, webhook_id, events, status, config_template}

# 2. 啟動 bridge（uvicorn 範例）
uvicorn linear_bridge.app:app \
    --host 0.0.0.0 --port 8443 \
    --ssl-keyfile /etc/ssl/private/bridge.key \
    --ssl-certfile /etc/ssl/certs/bridge.crt &

# 3. 啟動 worker（獨立 process）
python -m linear_bridge.worker &
```

### Resume 從 Linear 接到終端

```bash
# 在 Linear UI 看到的 issue identifier 是 ENG-1423
hermes --resume linear_acme_ENG-1423
# → 接續同一條對話，所有 Linear webhook 觸發的歷史都還在
```

### 路由設定

`templates/route.yaml` 是宣告式設定，bridge 啟動時載入；事件型別、session 範本、agent runner、回寫格式都在裡面。最少必填三欄：

```yaml
freshness_window_ms: 300000
fast_ack: true
actor_id_blacklist: ["${HERMES_BOT_ACTOR_ID}"]
```

完整範本見 `templates/route.yaml`，包含 4 種事件（Issue:created/updated/deleted、Comment:created）的路由規則、session_templates、agent_runners、output_templates、retry / observability。

## Common Pitfalls

1. **驗章用了 reserialized JSON 而非 raw body**。Express、FastAPI 中介層常自動 `JSON.parse`，導致 `expected != sig_header`。一定要在驗章前抓住 raw bytes。Cloudflare Workers 用 `await request.text()` 一次取完，後面 `JSON.parse` 同一個字串。

2. **直接在 webhook handler 裡 spawn agent**。`subprocess.run(["hermes", ...])` 動輒 30 秒以上，5 秒 timeout 立刻觸發 Linear 重送。**必須** enqueue + 獨立 worker process / thread / queue 消費。

3. **回 200 表示「驗章失敗但不要重送」**。錯。驗章失敗應回 401，Linear 不會重送非 timeout / 非 200 的請求；回 200 反而把偽造請求埋掉。重送只發生在 5 秒 timeout 或非 200 的真實處理失敗。

4. **沒有 bot actor 黑名單，造成無窮迴圈**。Bot 自留 comment → Linear 觸發 Comment:created → bridge 又跑一次 agent → 又留 comment …。第一次 bot 留言後抓 `actor.id` 存成 `HERMES_BOT_ACTOR_ID`，bridge 入口 short-circuit。

5. **以為 Linear GraphQL 有 idempotency header，沒有**。L3 dedupe 要自己做：mutation 前先用 `comments(first:50)` 查有沒有同一 `run_id` marker 的 comment，有就 update 而非 create。

6. **session_id 用 timestamp 當前綴**。會跟 Hermes 預設格式 (`YYYYMMDD_HHMMSS_<short>`) 撞、人類看不懂這條是哪個 issue。固定用 `linear_{workspace_slug}_{issue_identifier}`。

7. **Webhook URL 沒帶 workspace 區分**。多組織共用同一個 bridge 時，誤觸發風險高。路徑加 `/linear/<workspace_slug>/webhook`，verify 後再比 `payload.data.organizationId`。

8. **Secret 寫進 SKILL.md / route.yaml / git**。永遠用 `~/.hermes/secrets/linear_webhook.secret` (mode 600) 或 env var；範本檔只放 `${HERMES_LINEAR_WEBHOOK_SECRET}` 佔位符。

9. **list 查詢沒寫 `first:`**。`comments` / `issues` 預設 50 條，加上子物件展開輕易飆破 1000 點/查詢，吃光 X-RateLimit-Complexity-Remaining。永遠 `first: 10` 起跳。

10. **連續 4 次失敗 webhook 會被 Linear 自動停用**。bridge 出錯時務必告警 + 監控 webhook `enabled` 狀態；scripts/linear_webhook_subscribe.sh 會自動 re-enable disabled webhook。

## Failure Mode Table

| # | 失敗模式 | 偵測 | 恢復 |
| --- | --- | --- | --- |
| F1 | 偽造 webhook | signature mismatch | 401，記 incident |
| F2 | Replay > 5 分鐘 | webhookTimestamp 比對 | 401 |
| F3 | 重複 delivery | `Linear-Delivery` 已存在 | 200 直接 ack |
| F4 | Bridge 處理 > 5 秒 | 監控 P95 latency | 立即把 spawn 移到 worker |
| F5 | Bot 自留言觸發迴圈 | actor.id 比對 | filter at bridge ingress |
| F6 | Agent crash 但已 ack | jobs table state=`failed` | dead-letter + 人工 retry |
| F7 | Linear API 限速 | header / RATELIMITED | exponential backoff，最多 3 次 |
| F8 | Comment 重複留 | 查 marker comment | upsert 而非 create |
| F9 | Workspace 串錯 | `data.organizationId` 比對失敗 | 401 |
| F10 | Webhook 被 Linear 停用 | 連續 4 次失敗 | 監控 + 告警 + scripts/linear_webhook_subscribe.sh re-enable |

## Verification Checklist

- [ ] `scripts/verify_signature.py` 用 raw body + `hmac.compare_digest`，**不是** 比對 reserialized JSON
- [ ] webhook handler 在 100ms 內回 200，spawn agent 走獨立 worker
- [ ] `templates/route.yaml` 含 `freshness_window_ms`、`fast_ack`、`actor_id_blacklist` 三欄
- [ ] session_id 命名遵循 `linear_<workspace>_<issue_identifier>`
- [ ] L1 dedupe 用 `Linear-Delivery`，L2 用 `(issueId, action, contentHash)`，L3 用 `<!-- hermes:run=<run_id> -->` marker
- [ ] commentCreate 前先 `comments(first:10, filter:...)` 查 marker，避免重複留言
- [ ] 監控 `X-RateLimit-Requests-Remaining` / `X-RateLimit-Complexity-Remaining`
- [ ] Bot actor 黑名單已設（`HERMES_BOT_ACTOR_ID`）
- [ ] Webhook URL 路徑帶 workspace_slug，verify 後雙重比對 organizationId
- [ ] Secret 存放在 `~/.hermes/secrets/linear_webhook.secret` (mode 600) 或 env，**不在 git**
- [ ] Hermes session 可從終端 `hermes --resume linear_<ws>_<issue>` 接手

## References

- `experimental/risk-analysis.md` — oracle 出品的完整風險分析（簽章、冪等、session mapping、rate limit）
- `templates/route.yaml` — 4 層管線的宣告式路由設定
- `scripts/linear_webhook_subscribe.sh` — idempotent webhook 訂閱腳本
- `scripts/verify_signature.py` — 完整簽章驗證實作
- `scripts/dedupe_check.py` — 三層 dedupe 查詢實作
- [Linear Webhooks](https://linear.app/developers/webhooks) — 官方文件，HMAC-SHA256 + retry 規則
- [Linear Rate Limiting](https://linear.app/developers/rate-limiting) — 限額與 leaky bucket 演算法
- Hermes session 結構：`hermes-agent/agent/agent_init.py:1031-1049`、`hermes_state.py:521-548`
