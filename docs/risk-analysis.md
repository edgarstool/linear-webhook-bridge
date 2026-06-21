---
worker: oracle
task: t_7afe67eb
swarm_root: t_b6eb02c0
artifact_kind: risk_analysis
audience: forge (SKILL.md), default (scripts/templates), chronicler (Obsidian report), sentinel (verifier), quartermaster (synthesizer)
sources:
  - https://linear.app/developers/webhooks
  - https://linear.app/developers/rate-limiting
  - hermes-agent/agent/agent_init.py (session_id semantics)
  - hermes-agent/hermes_state.py (parent_session_id chain, sqlite session store)
---

# linear-webhook-bridge — 技術風險分析

四層管線：`Linear webhook → Hermes session mapper → agent runner → Linear API 回寫`。本文鎖定**安全、冪等、會話映射**三條主軸的具體決策，給其他 worker 直接抄作業用。

---

## 1. 四層管線參考圖

```
[Linear]                      [Edge / Bridge]                    [Hermes core]              [Linear]
   │                                │                                  │                       │
   │  POST /linear-webhook          │                                  │                       │
   │  Linear-Signature: <hex>       │                                  │                       │
   │  Linear-Delivery: <uuid>       │                                  │                       │
   │  Linear-Event: Issue|Comment   │                                  │                       │
   │  body: {action,type,data,...}  │                                  │                       │
   ├───────────────────────────────►│                                  │                       │
   │                                │ 1. verify HMAC-SHA256 (raw body) │                       │
   │                                │ 2. timestamp freshness window    │                       │
   │                                │ 3. dedupe by Linear-Delivery     │                       │
   │                                │ 4. resolve issue → session_id    │                       │
   │                                │ 5. enqueue + 200 OK fast-ack ◄───┤ ≤ 5000 ms 必須回 200  │
   │                                │                                  │                       │
   │                                │ 6. spawn `hermes ... --resume`   │                       │
   │                                ├─────────────────────────────────►│                       │
   │                                │                                  │ 7. agent 執行         │
   │                                │                                  │ 8. issueComment...   │
   │                                │                                  │   Mutation (GraphQL) ─►
   │                                │                                  │                       │
   │                                │                                  │◄──────────────────────┤
```

關鍵邊界：**步驟 5 必須在 5 秒內完成**（Linear 5000 ms timeout 直接重送），所以 agent 執行只能異步。

---

## 2. 安全風險：Linear Webhook 簽章

### 2.1 簽章機制（事實層）

| 項目 | 值 |
| --- | --- |
| Header | `Linear-Signature` |
| 演算法 | HMAC-SHA256，**hex** 編碼（不是 base64） |
| 簽章對象 | **raw HTTP body bytes**（必須在解析 JSON 前抓住原文） |
| 共享密鑰 | webhook 建立時由 Linear 產生的 `secret` |
| 額外驗證欄位 | body 內 `webhookTimestamp`（unix ms）+ header `Linear-Delivery`（UUIDv4） |

### 2.2 必須做、實作上很容易踩雷

1. **永遠 verify raw body，不是 re-serialize 過的 JSON**。Express、FastAPI、Cloudflare Workers 中介層常自動 `JSON.parse`，導致比對失敗。
   - Node: `express.raw({type:'application/json'})` 或 `req.rawBody`
   - FastAPI: `await request.body()`，**不要**在驗章前讀 `await request.json()`
   - Cloudflare Workers: `await request.text()` 一次取完，後面 `JSON.parse` 同一個字串
2. **constant-time compare**：用 `crypto.timingSafeEqual` / `hmac.compare_digest`，不要直接 `===`，避免時間側通道。
3. **Replay 防禦**：Linear 沒提供 nonce 過期保證，自己加：
   - 比對 `webhookTimestamp` 與當前時間差 ≤ **5 分鐘** → 否則 401。
   - 紀錄 `Linear-Delivery` UUID 至少 24 小時（同時兼任冪等鍵，見 §3）。
4. **失敗時回 4xx，不要回 200**。回 200 等於告訴 Linear「這個請求 OK」，駭客送的偽造請求就被你埋掉了；回 4xx Linear 不會重送（重送只在非 200 + timeout）。
5. **HTTPS only**：Linear 強制 https + 非 localhost，不要在文件示範 ngrok 之外的明文 URL。

### 2.3 Hermes 整合層的額外要求

- **Secret 不進 git、不進 SKILL.md**：放 `~/.hermes/secrets/linear_webhook.secret`（mode 600）或 `HERMES_LINEAR_WEBHOOK_SECRET` env，bridge 服務啟動時讀取。
- **Webhook URL 設計**：路徑包含 workspace 識別字串避免跨組織誤觸發，例如 `/linear/<workspace_slug>/webhook`，並在 verify 後再次比對 `data.organizationId`。
- **Body 大小上限**：bridge reverse proxy 設 `client_max_body_size 1m`；Linear payload 通常 < 50 KB，超過直接拒掉避免被當 DoS 入口。

### 2.4 偽代碼（Python，給 forge / default 直接搬）

```python
import hmac, hashlib, time
from typing import Tuple

FRESHNESS_WINDOW_MS = 5 * 60 * 1000  # 5 分鐘

def verify_linear_webhook(raw_body: bytes, headers: dict, secret: str,
                          now_ms: int | None = None) -> Tuple[bool, str]:
    sig_header = headers.get("linear-signature") or headers.get("Linear-Signature")
    if not sig_header:
        return False, "missing signature header"
    expected = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, sig_header):
        return False, "signature mismatch"

    # 取 body 內 webhookTimestamp 比對 freshness（Linear 簽完章之後簽章本身保證不可被改）
    import json
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

---

## 3. 冪等性風險：Linear-Delivery + 內部 dedupe

### 3.1 為什麼會收到重複事件

Linear 的 retry 邏輯：**5 秒沒回 200，或回非 200，會在 1 分鐘 / 1 小時 / 6 小時各重送一次**，總共最多 4 次嘗試。也就是任何 200 OK 必須在 5 秒內送出，**否則保證會被重送**。

實務重複來源：
1. Bridge 處理慢（spawn agent 同步執行）→ 5 秒後重送，但本機其實已經跑起來。
2. Bridge 回 200 但下游 agent crash → 你以為已完成，事件本體已被 ack。
3. 同一 issue 被多 worker 同時操作（webhook + cron + 手動 CLI），事件互相覆蓋 Linear 上的狀態。

### 3.2 三層 dedupe 設計

| 層 | 鍵 | 儲存 | TTL | 目的 |
| --- | --- | --- | --- | --- |
| L1: webhook 入口 | `Linear-Delivery` | SQLite `linear_webhook_deliveries(delivery_id PRIMARY KEY, received_at, status)` | 7 天 | 同一 delivery 重複到達直接回 200 不再 enqueue |
| L2: 業務動作 | `(issueId, action, contentHash)` | 同表 + 額外索引 | 24 小時 | Linear 偶爾把 `update` 連發兩次（actor 連續編輯），同 hash 視為同一動作 |
| L3: Linear 回寫 | `idempotencyKey` 自填到 `commentCreate` 的 client-side metadata | Linear 沒有原生 idempotency，得用「先 query 再 mutate」 | — | 避免 agent 跑兩次留兩條一樣 comment |

> **重要事實**：Linear GraphQL **沒有像 Stripe 那樣的 `Idempotency-Key` header**。L3 必須自己做：在 mutation 前先 `comments(filter:{...})` 找有沒有 body 開頭含同一 `<!-- hermes:run=<run_id> -->` 標記的 comment，有就跳過或 update。

### 3.3 fast-ack pattern（避開 5 秒窗）

```
POST /linear/webhook
  ├─ verify signature + freshness        (≤ 50 ms)
  ├─ INSERT OR IGNORE delivery_id        (≤ 10 ms, SQLite WAL)
  │     └─ if existed → return 200 OK    ← 重複事件直接結束
  ├─ enqueue job(issue_id, payload)      (寫一個 jobs table，≤ 20 ms)
  └─ return 200 OK                       (總計 < 100 ms)

[separate worker process]
  ├─ SELECT next job WHERE status='queued'
  ├─ resolve session_id (見 §4)
  ├─ spawn `hermes --resume <sid> --headless -p "<rendered prompt>"`
  ├─ 取 stdout / 最後一條 assistant message
  └─ commentCreate via Linear GraphQL（含 §3.2 L3 標記）
```

**不要**直接在 webhook handler 裡 `subprocess.run(hermes ...)`。一定要用獨立 worker（thread / process / queue）消費 jobs table，否則 §3.1.1 立刻重現。

---

## 4. Session mapping：Linear issue → Hermes session

### 4.1 設計目標

- 同一個 Linear issue 多次互動 → **共用同一個 Hermes session**，享 prompt cache 與對話連續性。
- 不同 issue → 不同 session，互不汙染。
- 人類也可以在終端 `hermes --resume <sid>` 接手該 issue 的對話。
- session 可被刪除/重置（issue 關閉、誤觸發回滾）。

### 4.2 命名策略（推薦）

```
session_id = f"linear_{workspace_slug}_{issue_identifier}"
# 例：linear_acme_ENG-1423
```

理由：
- **Deterministic**：同 issue 永遠對到同 session_id，不需查表，bridge stateless。
- **可讀**：人類在 `hermes sessions list` 一眼看出來這條 session 屬於哪個 issue。
- **不撞 timestamp 預設格式**：Hermes 預設 `YYYYMMDD_HHMMSS_<short_uuid>`（見 `agent_init.py:1038`），加 `linear_` 前綴零碰撞。
- **Workspace 隔離**：`workspace_slug` 防多組織共用同一個 Hermes home 時碰撞。

### 4.3 Mapping table（仍然需要，原因）

雖然 ID 是決定性的，仍建議落一張表 `linear_session_map`：

| 欄位 | 用途 |
| --- | --- |
| `issue_id` (Linear UUID) | 主鍵之一 |
| `issue_identifier` (`ENG-1423`) | 人類可讀 |
| `session_id` | Hermes session |
| `created_at`, `last_event_at` | TTL / 清理 |
| `state` (`active`, `archived`, `disabled`) | 軟刪除、停用 bot |

用途：
1. **反向查詢**：給一個 session_id 找回對應 issue，方便 agent 在回寫 comment 時精準鎖定 `issueId` 而非依賴 prompt 解析。
2. **清理策略**：issue 關閉超過 30 天 → archive 對應 session（`hermes sessions archive`）。
3. **黑名單**：某些 issue（內部噪音、bot 自家留言）可以 `state=disabled` 跳過。

### 4.4 First-run vs resume 的判斷

```python
def get_or_create_session(issue_identifier: str, workspace_slug: str) -> tuple[str, bool]:
    sid = f"linear_{workspace_slug}_{issue_identifier}"
    is_new = not session_db.exists(sid)
    return sid, is_new
```

- `is_new=True` → 啟動 `hermes -p "<system+first prompt>" --session-id <sid>`
- `is_new=False` → `hermes --resume <sid> -p "<just the new event>"`

### 4.5 跨 actor 的 prompt 構造

Linear webhook 的 `actor` 可能是 user / OauthClient / Integration。bot 自己留 comment 也會觸發 webhook，**必須在 bridge 層過濾掉自家 actor**，否則 agent 會回應自己造成無窮迴圈。

```
if payload.actor.id == HERMES_BOT_ACTOR_ID:
    return 200  # ack but skip
```

實作上：第一次 bot 留 comment 後從回應抓 `user.id`，存成 `HERMES_BOT_ACTOR_ID` 設定。

---

## 5. Linear API 回寫風險：rate-limit 與複雜度

### 5.1 限額表

| 認證 | 請求數/小時 | 複雜度/小時 | 單查詢上限 |
| --- | --- | --- | --- |
| API key | 2,500 | 3,000,000 | **10,000** |
| OAuth app | 5,000 | 2,000,000 | 10,000 |
| Unauth | 600 / IP | 100,000 | 10,000 |

> 演算法是 leaky bucket，token 以 `LIMIT/PERIOD` 速率回充。

### 5.2 預期消耗（commentCreate 場景）

`commentCreate(input:{issueId,body}){comment{id}}` 約 2 點。每 webhook 觸發 1 次回寫 → 2 點。即使 1 小時 1000 個 webhook 也才 2000 點，遠在限額內。

**真正會爆的是查詢路徑**：
- `comments(first:50)` 找重複標記 → 50 × N properties × 0.1 + 50 + 1 ≈ 60 點
- 不限 `first` 的話走預設 50 連同子物件展開，輕易飆破 1,000 點/查詢

### 5.3 必做 best practice（取自 Linear 官方文件）

1. **永遠指定 `first:`**：`comments(first:10)` 而不是 `comments`，預設 50 太貴。
2. **避免 polling**：用 webhook 觸發，不要每 N 秒打 issues 查清單。
3. **按 updatedAt 排序**：要做歷史補抓時用 `orderBy: updatedAt`，避免從頭翻。
4. **監聽 response headers**，提早降速：

| Header | 行動 |
| --- | --- |
| `X-RateLimit-Requests-Remaining` < 10% | 暫停非必要查詢 |
| `X-RateLimit-Complexity-Remaining` < 10% | 進入 backoff |
| HTTP 400 + `RATELIMITED` | 退到 `X-RateLimit-Requests-Reset` 後再試，重試上限 3 |

### 5.4 Retry 策略

```python
def call_linear(query, variables, max_retries=3):
    for attempt in range(max_retries):
        resp = requests.post(LINEAR_API, json={"query":query,"variables":variables}, headers=headers)
        if resp.status_code == 200:
            data = resp.json()
            if "errors" in data and any(e.get("extensions",{}).get("code") == "RATELIMITED" for e in data["errors"]):
                reset_ms = int(resp.headers.get("X-RateLimit-Requests-Reset", "0"))
                sleep_for = max(1, (reset_ms - int(time.time()*1000)) / 1000)
                time.sleep(min(sleep_for, 60))
                continue
            return data
        if resp.status_code in (502, 503, 504):
            time.sleep(2 ** attempt)
            continue
        resp.raise_for_status()
    raise RuntimeError("linear api: max retries exceeded")
```

### 5.5 OAuth Actor Authorization（dynamic limits）

如果預期高量、多 workspace，建議走 OAuth + Actor Authorization，Linear 會依 workspace 付費人數動態調升上限（官方 rate-limiting doc 末段）。MVP 階段 API key 就夠。

---

## 6. 失敗模式總表（給 sentinel 對照）

| # | 失敗模式 | 偵測 | 恢復 |
| --- | --- | --- | --- |
| F1 | 偽造 webhook | signature mismatch | 401，記 incident |
| F2 | Replay > 5 分鐘 | timestamp 比對 | 401 |
| F3 | 重複 delivery | `Linear-Delivery` 已存在 | 200 直接 ack |
| F4 | Bridge 處理 > 5 秒 | 監控 P95 latency | 立即把 spawn 移到 worker（§3.3） |
| F5 | Bot 自留言觸發迴圈 | actor.id 比對 | filter at bridge |
| F6 | Agent crash 但已 ack | jobs table state=`failed` | dead-letter + 人工 retry |
| F7 | Linear API 限速 | header / RATELIMITED | exponential backoff，最多 3 次 |
| F8 | Comment 重複留 | 查 marker comment | upsert 而非 create |
| F9 | Workspace 串錯 | `data.organizationId` 比對失敗 | 401 |
| F10 | Webhook 被 Linear 自動停用 | 連續 4 次失敗 | 監控 + 告警 + 手動重啟用 |

---

## 7. 給其他 worker 的明確掛鉤點

### 給 forge（SKILL.md）

- §2.4 簽章驗證範例可直接放「Webhook handler 範例」段落。
- §3.3 fast-ack pattern 是關鍵安全敘事，必須寫進 SKILL 的 Pitfalls。
- §4.2 session_id 命名 (`linear_{workspace}_{issue_identifier}`) 寫進 Conventions 段。
- §6 失敗模式表可直接抄成 Pitfalls 子表。

### 給 default（scripts/templates）

- `scripts/linear_webhook_subscribe.sh`：先呼叫 §2 GraphQL `webhookCreate`，secret 寫到 `~/.hermes/secrets/linear_webhook.secret` (mode 600)。
- `templates/route.yaml`：包含 `actor_id_blacklist`、`workspace_slug`、`session_id_prefix=linear`、`freshness_window_ms=300000`、`fast_ack=true`。
- `scripts/verify_signature.py`：抄 §2.4。
- `scripts/dedupe_check.py`：實作 §3.2 三層查詢。

### 給 chronicler（Obsidian 報告）

- §1 流程圖可轉 mermaid。
- §5.1 限額表、§6 失敗模式表直接搬。
- 風險矩陣建議 4×3：嚴重度（高/中/低）× 機率（高/中/低），把 F1–F10 填進去。

### 給 sentinel（驗收）

PASS 條件至少：
1. SKILL.md 有 §2 簽章驗章 + §3 fast-ack 兩段。
2. `templates/route.yaml` 包含 `freshness_window_ms`、`fast_ack`、`actor_id_blacklist` 三個欄位。
3. `scripts/verify_signature.py` 用 raw body + `compare_digest`，**不是** 比對 reserialized JSON。
4. session_id 命名遵循 `linear_<workspace>_<issue_identifier>`。
5. 文件至少提到 Linear-Delivery、webhookTimestamp、X-RateLimit-* 三類頭。

### 給 quartermaster（合成）

最終 deliverable 對應檔位：
- `linear-webhook-bridge/SKILL.md` — forge
- `linear-webhook-bridge/scripts/{linear_webhook_subscribe.sh,verify_signature.py,dedupe_check.py}` — default
- `linear-webhook-bridge/templates/route.yaml` — default
- `linear-webhook-bridge/experimental/risk-analysis.md` — **本檔，建議直接複製過去**
- Obsidian: `Projects/linear-webhook-bridge/` 報告 — chronicler
- GitHub repo: push master + tag `v1.0.0`

---

## 附錄 A — 引用憑據

- **Linear webhook signature 演算法**：[Linear Webhooks docs](https://linear.app/developers/webhooks) — "createHmac('sha256', secret).update(payload).digest('hex')" 範例。
- **Retry 策略**：同上 — 5 秒 timeout / non-200 後 1 分鐘、1 小時、6 小時各 retry 一次，連 4 次失敗會被 disable。
- **Headers**：`Linear-Delivery` (UUIDv4)、`Linear-Event`、`Linear-Signature`，body 內含 `webhookTimestamp`。
- **Rate limits**：[Linear Rate limiting docs](https://linear.app/developers/rate-limiting) — API key 2,500 req/hr, 3M complexity/hr, 單查詢 ≤ 10K complexity；leaky bucket。
- **Hermes session 結構**：`hermes-agent/agent/agent_init.py:1031–1049`（session_id 注入 + env）、`hermes-agent/hermes_state.py:521–548`（schema 含 `parent_session_id` chain）。
- **Hermes resume CLI**：`cli.py:3270–3299`（`--resume <id>`，會從 SQLite 還原歷史）。
