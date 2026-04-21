#!/usr/bin/env python3
"""Upsert a markdown Lab CI report on the upstream PR.

Invoked as the final step of each lab-ci workflow when
upstream_repo / upstream_pr / upstream_sha inputs are present.

The report is rebuilt from GitHub's state every time (commit
statuses + workflow run jobs/steps), so it's idempotent and
race-free: whichever workflow finishes last simply rebuilds with
complete data. Earlier calls produce partial reports that get
overwritten as more data arrives.

Environment:
  UPSTREAM_REPO        owner/repo of the upstream (where statuses + comment live)
  UPSTREAM_SHA         commit SHA being tested
  UPSTREAM_PR          PR number (where the comment goes)
  FORK_REPO            fork repo (where the workflow runs live)
  GH_TOKEN             PAT with public_repo scope (statuses + comments)

Exits 0 on any expected failure (missing token, API error) so a
reporting hiccup never fails the main CI check. Writes a one-line
warning to stderr in those cases.
"""

import json
import os
import re
import sys
import urllib.error
import urllib.request
from typing import Any

# Contexts we care about. Order = display order in the report table.
CONTEXTS = [
    ("lab-ci / unit-tests", "🧪", "Unit tests"),
    ("lab-ci / cat8kv",     "🖥️",  "Cat8kv (virtual)"),
    ("lab-ci / cat9k",      "🛰️",  "Cat9k (physical)"),
]

STATE_EMOJI = {
    "success": "✅",
    "failure": "❌",
    "error":   "❌",
    "pending": "⏳",
}

GITHUB_API = "https://api.github.com"


def warn(msg: str) -> None:
    sys.stderr.write(f"::warning::post-pr-report: {msg}\n")


def api(path: str, method: str = "GET", body: dict | None = None) -> Any:
    """Minimal GitHub API call. Returns parsed JSON (or None on 204)."""
    url = path if path.startswith("http") else f"{GITHUB_API}{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("Authorization", f"Bearer {os.environ['GH_TOKEN']}")
    req.add_header("X-GitHub-Api-Version", "2022-11-28")
    if data:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=30) as resp:
        raw = resp.read()
        return json.loads(raw) if raw else None


def fmt_duration(started: str | None, completed: str | None) -> str:
    if not started or not completed:
        return ""
    from datetime import datetime
    s = datetime.fromisoformat(started.replace("Z", "+00:00"))
    e = datetime.fromisoformat(completed.replace("Z", "+00:00"))
    secs = int((e - s).total_seconds())
    if secs < 60:
        return f"{secs}s"
    return f"{secs // 60}m{secs % 60:02d}s"


def latest_status_per_context(repo: str, sha: str) -> dict[str, dict]:
    """Return {context: latest_status_json} for our CONTEXTS."""
    # Statuses API returns most recent first.
    statuses = api(f"/repos/{repo}/commits/{sha}/statuses?per_page=100") or []
    out: dict[str, dict] = {}
    for s in statuses:
        ctx = s.get("context", "")
        if ctx not in out and any(ctx == c for c, _, _ in CONTEXTS):
            out[ctx] = s
    return out


def run_id_from_target_url(url: str) -> int | None:
    m = re.search(r"/actions/runs/(\d+)", url or "")
    return int(m.group(1)) if m else None


def fetch_steps(fork_repo: str, run_id: int) -> list[dict]:
    """Steps from the first (and only) job of a workflow run."""
    try:
        data = api(f"/repos/{fork_repo}/actions/runs/{run_id}/jobs") or {}
    except urllib.error.HTTPError as e:
        warn(f"fetch jobs {run_id}: {e}")
        return []
    jobs = data.get("jobs", [])
    if not jobs:
        return []
    return jobs[0].get("steps", [])


def render_report(
    upstream_repo: str,
    upstream_sha: str,
    upstream_pr: str,
    fork_repo: str,
    latest: dict[str, dict],
) -> str:
    sha_short = upstream_sha[:12]
    lines: list[str] = []
    lines.append(f"<!-- lab-ci-report:{upstream_sha} -->")
    lines.append(f"## 🤖 Lab CI Report")
    lines.append("")
    lines.append(
        f"Commit `{sha_short}` · validated on lab hardware via "
        f"[`{fork_repo}`](https://github.com/{fork_repo})."
    )
    lines.append("")

    # Summary table
    lines.append("| Check | Result | Duration | Details |")
    lines.append("| :--- | :---: | :---: | :--- |")
    total_pass = total_fail = total_pending = 0
    for ctx, emoji, label in CONTEXTS:
        s = latest.get(ctx)
        if not s:
            state = "pending"
            duration = "—"
            link = "—"
            total_pending += 1
        else:
            state = s.get("state", "pending")
            if state == "success":
                total_pass += 1
            elif state in ("failure", "error"):
                total_fail += 1
            else:
                total_pending += 1
            duration = fmt_duration(s.get("created_at"), s.get("updated_at"))
            tgt = s.get("target_url") or ""
            link = f"[logs]({tgt})" if tgt else "—"
        verdict_emoji = STATE_EMOJI.get(state, "❔")
        lines.append(f"| {emoji} {label} | {verdict_emoji} `{state}` | {duration or '—'} | {link} |")

    lines.append("")
    totals = []
    if total_pass:
        totals.append(f"**{total_pass} passed** ✅")
    if total_fail:
        totals.append(f"**{total_fail} failed** ❌")
    if total_pending:
        totals.append(f"**{total_pending} pending** ⏳")
    lines.append("**Summary:** " + " · ".join(totals) if totals else "**Summary:** no results yet")
    lines.append("")

    # Per-workflow step breakdown
    for ctx, emoji, label in CONTEXTS:
        s = latest.get(ctx)
        if not s:
            continue
        run_id = run_id_from_target_url(s.get("target_url", ""))
        if not run_id:
            continue
        steps = fetch_steps(fork_repo, run_id)
        # Filter out skipped post-action steps to keep the table compact.
        visible = [st for st in steps if st.get("conclusion") != "skipped" or st.get("status") == "in_progress"]
        if not visible:
            continue
        pass_n = sum(1 for st in visible if st.get("conclusion") == "success")
        fail_n = sum(1 for st in visible if st.get("conclusion") == "failure")
        state = s.get("state", "pending")
        hdr_emoji = STATE_EMOJI.get(state, "❔")
        lines.append(
            f"<details><summary>{emoji} {label} — {hdr_emoji} "
            f"{pass_n} passed, {fail_n} failed</summary>\n"
        )
        lines.append("| # | Step | Status | Duration |")
        lines.append("| ---: | :--- | :---: | :---: |")
        for i, st in enumerate(visible, 1):
            name = st.get("name", "?").replace("|", "\\|")
            conclusion = st.get("conclusion") or st.get("status") or "?"
            step_emoji = {
                "success": "✅", "failure": "❌", "skipped": "⏭️",
                "cancelled": "🚫", "in_progress": "⏳",
            }.get(conclusion, "❔")
            dur = fmt_duration(st.get("started_at"), st.get("completed_at"))
            lines.append(f"| {i} | {name} | {step_emoji} | {dur or '—'} |")
        lines.append("\n</details>\n")

    lines.append("---")
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines.append(
        f"_Updated {now}. Report rebuilt from commit statuses on each run; "
        f"see individual workflow logs via the **logs** links above._"
    )
    return "\n".join(lines)


def upsert_comment(
    upstream_repo: str,
    upstream_pr: str,
    upstream_sha: str,
    body: str,
) -> None:
    marker = f"<!-- lab-ci-report:{upstream_sha} -->"
    # Find existing comment with our marker. Issue comments are paginated;
    # we iterate but for practical PRs one page (30) is enough.
    existing_id: int | None = None
    for page in range(1, 10):
        comments = api(
            f"/repos/{upstream_repo}/issues/{upstream_pr}/comments"
            f"?per_page=100&page={page}"
        ) or []
        if not comments:
            break
        for c in comments:
            if marker in (c.get("body") or ""):
                existing_id = c["id"]
                break
        if existing_id or len(comments) < 100:
            break

    if existing_id:
        api(
            f"/repos/{upstream_repo}/issues/comments/{existing_id}",
            method="PATCH",
            body={"body": body},
        )
    else:
        api(
            f"/repos/{upstream_repo}/issues/{upstream_pr}/comments",
            method="POST",
            body={"body": body},
        )


def main() -> int:
    required = ["UPSTREAM_REPO", "UPSTREAM_SHA", "UPSTREAM_PR", "FORK_REPO"]
    missing = [k for k in required if not os.environ.get(k)]
    if missing:
        warn(f"missing env: {missing}; skipping")
        return 0
    if not os.environ.get("GH_TOKEN"):
        warn("GH_TOKEN not set; skipping report")
        return 0

    upstream_repo = os.environ["UPSTREAM_REPO"]
    upstream_sha = os.environ["UPSTREAM_SHA"]
    upstream_pr = os.environ["UPSTREAM_PR"]
    fork_repo = os.environ["FORK_REPO"]

    try:
        latest = latest_status_per_context(upstream_repo, upstream_sha)
        body = render_report(upstream_repo, upstream_sha, upstream_pr, fork_repo, latest)
        upsert_comment(upstream_repo, upstream_pr, upstream_sha, body)
    except urllib.error.HTTPError as e:
        warn(f"API error {e.code}: {e.reason}")
        return 0
    except Exception as e:  # noqa: BLE001 — never fail the main check for reporting
        warn(f"{type(e).__name__}: {e}")
        return 0

    return 0


if __name__ == "__main__":
    sys.exit(main())
