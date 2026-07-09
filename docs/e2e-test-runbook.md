# Linear Webhook Bridge E2E Runbook

這份 runbook 對應 `EDG-108`，目標是驗證完整的 `Linear webhook → queue → Hermes 處理 → Linear 回寫` 流程。

## Scope

- 案例 1：手動發送測試 webhook
- 案例 2：真實 Linear Agent `@mention`
- 案例 3：長時任務處理與追蹤

## Prerequisites

- 已完成 webhook 訂閱，且 `WEBHOOK_URL` / `WEBHOOK_SECRET` 可用。
- bridge 入口已啟動，並有獨立 worker / queue consumer。
- 已配置 `LINEAR_API_KEY` 與 `HERMES_BOT_ACTOR_ID`。
- 可讀取 bridge log、worker log、`event-queue.jsonl` 或同等 queue 落地檔。

缺資料：
- 這個 repo 不包含實際 bridge server / worker 程式，所以 queue 檔路徑、systemd service 名稱、實際 log 路徑需依部署環境替換。

## Test Data

建議固定使用一張測試 issue，避免和真實工作內容混雜。

- Issue identifier: `WHO-210`
- Workspace slug: `<your-workspace>`
- 測試 actor: 非 `HERMES_BOT_ACTOR_ID`

## Case 1: Manual Webhook

用簽過名的測試 payload 打到 bridge，先驗 ingress、queue、fast-ack。

```bash
python3 scripts/send_test_webhook.py \
  --url "https://linear.whoasked.vip/linear-webhook" \
  --secret "$WEBHOOK_SECRET" \
  --raw-body-file /tmp/linear-webhook-body.json \
  --event "Comment:created" \
  --organization-slug "<your-workspace>" \
  --organization-id "<org-id>" \
  --team-id "<team-id>" \
  --issue-id "<issue-id>" \
  --issue-identifier "WHO-210" \
  --issue-url "https://linear.app/<workspace>/issue/WHO-210" \
  --comment-body "@hermesagent 請回覆這則手動 webhook 測試。" \
  --pretty
```

觀察點：

- HTTP 應在 5 秒內收到 `200 OK`。
- `event-queue.jsonl` 應新增一筆對應的 `Linear-Delivery`。
- worker log 應看到 dequeue / session mapping / agent start。
- 若同一個 `--delivery-id` 重送，應被判定為 duplicate，而不是再次 enqueue。

建議檢查：

```bash
tail -n 20 /path/to/event-queue.jsonl
tail -n 100 /path/to/bridge.log
tail -n 100 /path/to/worker.log
```

## Case 2: Real Linear @mention

在 Linear issue 留言：

```text
@hermesagent 請回覆這則真實 mention 測試，並摘要目前 issue 狀態。
```

觀察點：

- Linear webhook 應送到 `linear.whoasked.vip`。
- bridge log 應有 `Comment:created` 事件，且 `actor.id` 不在 blacklist。
- Hermes 應 resume 或建立 `linear_<workspace>_WHO-210` session。
- Linear 應收到一則新 comment，且帶有 `<!-- hermes:run=... -->` marker。

建議檢查：

```bash
python3 scripts/dedupe_check.py --body-file /path/to/captured-payload.json --header "Linear-Delivery=<delivery-id>" --pretty
```

若要驗章：

```bash
python3 scripts/verify_signature.py \
  --body-file /tmp/linear-webhook-body.json \
  --secret "$WEBHOOK_SECRET" \
  --header "Linear-Signature=<signature>" \
  --header "Linear-Delivery=<delivery-id>" \
  --pretty
```

## Case 3: Long-Running Task

在同一張 issue 留言較複雜的任務，例如：

```text
@hermesagent 請先整理這張 issue 的背景，再列出風險、依賴、下一步，最後用條列摘要。
```

觀察點：

- ingress 仍需在 5 秒內先 ack，不可因 Hermes 執行時間變慢。
- worker log 應可追到完整 run，包含 session id、run id、writeback 結果。
- 最終回寫 comment 需結構清楚，且內容與 issue 上下文一致。

建議檢查：

- 是否有 dead-letter / failed job。
- 是否出現重複 comment。
- `X-RateLimit-Requests-Remaining` / `X-RateLimit-Complexity-Remaining` 是否接近警戒值。

## Success Criteria

- webhook 收到後 5 秒內開始處理，bridge 先成功 ack。
- queue 有落地，且 worker 有實際消費。
- Hermes 完成後 Linear comment 回寫成功。
- 回寫 comment 內容正確、沒有重複。
- log 可從 `Linear-Delivery` 一路追到 session id / run id / writeback。

## Failure Triage

- `401 signature mismatch`
  - 用 `scripts/verify_signature.py` 確認是否用 raw body 驗章。
- queue 沒新增資料
  - 檢查 `Linear-Delivery` 是否被當成 duplicate，或 ingress 未開 `fast_ack`。
- Hermes 沒開始跑
  - 檢查 worker 是否啟動，及 job status 是否卡在 `queued`。
- Linear 沒回寫
  - 檢查 `LINEAR_API_KEY`、rate-limit headers、以及 comment marker dedupe 邏輯。

## Suggested Evidence

- 一筆手動 webhook request/response
- 一筆 queue 寫入紀錄
- 一段 worker 啟動與完成 log
- 一則 Linear 最終回寫 comment 截圖或 comment ID
