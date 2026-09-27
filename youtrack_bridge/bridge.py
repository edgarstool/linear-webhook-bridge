"""YouTrack Webhook Triggers -> durable queue -> external agent -> issue comment.

Only the standard library is required. Run one ingress process and one worker
process against the same SQLite database on a single host.
"""

import hashlib
import hmac
import json
import os
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def connect(path):
    db = sqlite3.connect(path, timeout=30, isolation_level=None)
    db.execute("PRAGMA busy_timeout=30000")
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("""CREATE TABLE IF NOT EXISTS jobs (
        event_key TEXT PRIMARY KEY, issue_id TEXT NOT NULL, issue_readable TEXT NOT NULL,
        payload TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'queued',
        attempts INTEGER NOT NULL DEFAULT 0, result TEXT, error TEXT,
        created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL
    )""")
    return db


def event_key(payload):
    if payload.get("event") != "commentAdded":
        raise ValueError("unsupported event")
    comments = payload.get("comments")
    if not isinstance(comments, list) or len(comments) != 1:
        raise ValueError("expected exactly one comment")
    comment = comments[0]
    issue = payload.get("id")
    cid = comment.get("id") if isinstance(comment, dict) else None
    project = payload.get("project") or {}
    short = project.get("shortName") if isinstance(project, dict) else None
    number = payload.get("numberInProject")
    if not all(isinstance(x, str) and x for x in (issue, cid, short)) or not isinstance(number, int) or number < 1:
        raise ValueError("missing issue/comment identity")
    return f"comment:{issue}:{cid}", issue, f"{short}-{number}"


def accept(db, raw, token, provided, project, bot_login):
    if not token or not provided or not hmac.compare_digest(token.encode(), provided.encode()):
        return 401, "unauthorized"
    if len(raw) > 128 * 1024:
        return 413, "payload too large"
    try:
        payload = json.loads(raw)
        key, issue, readable = event_key(payload)
    except (ValueError, TypeError, UnicodeDecodeError):
        return 400, "invalid payload"
    if payload["project"]["shortName"] != project:
        return 403, "project mismatch"
    comment = payload["comments"][0]
    if not isinstance(comment.get("text"), str) or not comment["text"].strip():
        return 200, "ignored empty comment"
    author = comment.get("author")
    if (isinstance(author, dict) and author.get("login") == bot_login) or "<!-- edgar-agent:" in comment["text"]:
        return 200, "ignored bot comment"
    now = int(time.time())
    with db:
        cursor = db.execute(
            "INSERT OR IGNORE INTO jobs(event_key, issue_id, issue_readable, payload, status, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, 'queued', ?, ?)",
            (key, issue, readable, json.dumps(payload, ensure_ascii=False), now, now),
        )
    return 202, "queued" if cursor.rowcount else "duplicate"


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        if self.path != "/webhooks/youtrack":
            self.send_error(404)
            return
        try:
            size = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            size = 0
        if size <= 0 or size > 128 * 1024:
            self.send_error(413)
            return
        raw = self.rfile.read(size)
        with closing(connect(self.server.db_path)) as db:
            status, message = accept(db, raw, self.server.token,
                                     self.headers.get("X-YouTrack-Token", ""),
                                     self.server.project, self.server.bot_login)
        body = json.dumps({"status": message}).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def request_json(base_url, token, method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(base_url.rstrip("/") + path, data=data, method=method,
                                 headers={"Authorization": "Bearer " + token,
                                          "Accept": "application/json",
                                          "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as response:
        return json.load(response)


def marker(key):
    return "<!-- edgar-agent:" + hashlib.sha256(key.encode()).hexdigest() + " -->"


def has_comment(base_url, token, issue, tag):
    # All pages must be checked; an older run marker may no longer be in the first page.
    offset = 0
    while True:
        qs = urllib.parse.urlencode({"fields": "id,text", "$top": 100, "$skip": offset})
        page = request_json(base_url, token, "GET",
                            "/api/issues/" + urllib.parse.quote(issue, safe="") + "/comments?" + qs)
        if any(tag in (comment.get("text") or "") for comment in page):
            return True
        if len(page) < 100:
            return False
        offset += len(page)


def post_comment(base_url, token, issue, body):
    request_json(base_url, token, "POST",
                 "/api/issues/" + urllib.parse.quote(issue, safe="") + "/comments?fields=id",
                 {"text": body})


def claim(db):
    # BEGIN IMMEDIATE serializes claims across worker processes.
    db.execute("BEGIN IMMEDIATE")
    try:
        row = db.execute("SELECT event_key, issue_id, issue_readable, payload, result FROM jobs AS j "
                         "WHERE status='queued' AND NOT EXISTS "
                         "(SELECT 1 FROM jobs AS active WHERE active.issue_id=j.issue_id AND active.status='running') "
                         "ORDER BY created_at, event_key LIMIT 1").fetchone()
        if row:
            db.execute("UPDATE jobs SET status='running', attempts=attempts+1, updated_at=? WHERE event_key=?",
                       (int(time.time()), row[0]))
        db.execute("COMMIT")
        return row
    except Exception:
        db.execute("ROLLBACK")
        raise


def run_one(db, base_url, token, argv):
    row = claim(db)
    if row is None:
        return False
    key, issue, readable, raw_payload, result = row
    tag = marker(key)
    try:
        if not has_comment(base_url, token, issue, tag):
            if result is None:
                payload = json.loads(raw_payload)
                input_data = {"issue": readable, "session": "youtrack_" + readable,
                              "comment": payload["comments"][0]["text"], "eventKey": key}
                proc = subprocess.run(argv, input=json.dumps(input_data), text=True,
                                      capture_output=True, timeout=900, check=True)
                result = proc.stdout.strip()
                if not result or len(result) > 60000:
                    raise ValueError("runner returned empty or oversized comment")
                db.execute("UPDATE jobs SET result=?, updated_at=? WHERE event_key=?",
                           (result, int(time.time()), key))
            post_comment(base_url, token, issue, tag + "\n" + result)
        db.execute("UPDATE jobs SET status='done', error=NULL, updated_at=? WHERE event_key=?",
                   (int(time.time()), key))
    except Exception as exc:
        db.execute("UPDATE jobs SET status='failed', error=?, updated_at=? WHERE event_key=?",
                   (str(exc)[:1000], int(time.time()), key))
        print(f"failed {key}: {exc}", file=sys.stderr)
    return True


def required(name):
    value = os.environ.get(name, "")
    if not value:
        raise SystemExit(f"missing {name}")
    return value


def main():
    if len(sys.argv) != 2 or sys.argv[1] not in ("serve", "work", "retry-failed"):
        raise SystemExit("usage: python -m youtrack_bridge.bridge serve|work|retry-failed")
    mode = sys.argv[1]
    db_path = required("BRIDGE_DB")
    db = connect(db_path)
    if mode == "retry-failed":
        db.execute("UPDATE jobs SET status='queued', updated_at=? WHERE status='failed'", (int(time.time()),))
        return
    yt_token = required("YOUTRACK_API_KEY") if mode == "work" else None
    if mode == "serve":
        server = ThreadingHTTPServer(("127.0.0.1", int(os.environ.get("BRIDGE_PORT", "8644"))), Handler)
        server.db_path, server.token = db_path, required("YOUTRACK_WEBHOOK_TOKEN")
        server.project, server.bot_login = required("YOUTRACK_PROJECT"), required("YOUTRACK_BOT_LOGIN")
        server.serve_forever()
    else:
        base_url = required("YOUTRACK_URL")
        argv = json.loads(required("AGENT_ARGV_JSON"))
        if not isinstance(argv, list) or not argv or not all(isinstance(x, str) and x for x in argv):
            raise SystemExit("AGENT_ARGV_JSON must be a nonempty JSON array of strings")
        while True:
            if not run_one(db, base_url, yt_token, argv):
                time.sleep(1)


if __name__ == "__main__":
    main()
