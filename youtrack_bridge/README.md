# YouTrack review bridge (pilot)

This folder replaces the retired Linear webhook example with a runnable, single-host
YouTrack ingress and worker. It is **not deployed**. It does not change the live
`hooks.edgars.tools` Worker (`edgar-hooks-inbox`, maintained in `edgar-mail`). Keep
the existing mail webhook path isolated. The bridge binds to loopback; expose only
`POST /webhooks/youtrack` through a separately configured HTTPS route when ready.

## Contract

Use YouTrack's **Webhook Triggers** app, with the **Comment added** event for the
chosen project. Set a random 64-character token and leave the header name at its
default `X-YouTrack-Token`. YouTrack authenticates with this shared header token,
**not** with a Linear-style HMAC signature. Protect the route with HTTPS and keep
the webhook token separate from the YouTrack REST permanent token.

The ingress compares the token in constant time, checks the project, excludes bot
comments, and inserts one job per source comment ID into SQLite before returning
202. A duplicate returns 202 without inserting another job. `work` runs an
external agent command outside the HTTP request, then posts the output to the
same issue through YouTrack REST. The output carries a deterministic HTML marker.
Before each write, the worker searches every page of comments for that marker;
after a failed write it retains the agent result so a retry does not run the
agent twice. Failed jobs remain visible in the database and require an explicit
`retry-failed` call. The worker handles one job per issue at a time. Use **one**
worker process during the pilot. The single-host SQLite design is not a
multi-VM queue.

## Local start

Run from the repository root, in two terminals. Supply secrets through a secret
manager or process environment; never place them in a checked-in file or a shell
command recorded in history.

```bash
export BRIDGE_DB=/var/lib/youtrack-review/queue.sqlite
export YOUTRACK_PROJECT=EDG
export YOUTRACK_BOT_LOGIN=review-bot
export YOUTRACK_WEBHOOK_TOKEN='<secret from YouTrack Webhook Triggers>'
python3 -m youtrack_bridge.bridge serve
```

```bash
export BRIDGE_DB=/var/lib/youtrack-review/queue.sqlite
export YOUTRACK_URL=https://edgars.youtrack.cloud
export YOUTRACK_API_KEY='<permanent token with comment permissions>'
export AGENT_ARGV_JSON='["/path/to/your/agent-wrapper"]'
python3 -m youtrack_bridge.bridge work
```

The wrapper receives one JSON line on stdin with `issue`, `session`, `comment`,
and `eventKey`; it must print only the comment text to stdout and return exit
code 0. Wire that wrapper to the selected cloud agent only after checking its
actual account/API permissions. The existing Linear Hermes command templates
are not a verified YouTrack or Warp Oz integration.

To retry a failed job after fixing its cause:

```bash
BRIDGE_DB=/var/lib/youtrack-review/queue.sqlite python3 -m youtrack_bridge.bridge retry-failed
```

## Acceptance gate

1. `python3 -m unittest -v youtrack_bridge.test_bridge` passes.
2. Point the YouTrack `Comment added` webhook for a **test project/issue** at the
   HTTPS route. A bad token gives 401, a wrong project gives 403, and a valid
   human comment returns 202 with a queued row. Keep only this event enabled.
3. Confirm the agent wrapper receives the right issue and text; the worker posts
   exactly one marked YouTrack comment. A repeated delivery and a bot's own
   comment produce no new job or agent run.
4. Force a transient REST write failure. Confirm `failed` state and saved result;
   run `retry-failed`, confirm a single final comment and no extra agent run.
5. Record the actual YouTrack issue URL, webhook delivery, queue row status,
   comment URL, and timestamp. Only then mark EDG-47/48/55/59 replacement
   accepted. A passing unit test alone is not production acceptance.

Do not place this workload on Contabo based on old free-space snapshots. Verify
the target VM's current disk, memory, swap, ingress, and backup path first.
