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

import io
import json
import os
import re
import sys
import urllib.error
import urllib.request
import zipfile
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

# Go package path → CRD identifier. Used to bucket gotestsum output
# into the per-CRD table on the upstream PR comment.
#
# Edit this list when a new CRD or a new test package lands; missing
# packages fall through to the "Other / shared code" bucket so they're
# still counted but don't pollute the per-CRD breakdown.
PACKAGE_TO_CRD: dict[str, str] = {
    # CRDs implemented by their own controller package
    "github.com/cisco/virtual-kubelet-cisco/internal/controller":                              "CiscoDevice",
    "github.com/cisco/virtual-kubelet-cisco/internal/aggregator":                              "CiscoDevice",
    "github.com/cisco/virtual-kubelet-cisco/internal/provider":                                "CiscoDevice",
    "github.com/cisco/virtual-kubelet-cisco/internal/provider/deviceoperation":                "DeviceOperation",
    "github.com/cisco/virtual-kubelet-cisco/internal/provider/diagnostic":                     "IOSXEDiagnostic",
    "github.com/cisco/virtual-kubelet-cisco/internal/provider/diagnostic/adminserver":         "IOSXEDiagnostic",
    "github.com/cisco/virtual-kubelet-cisco/internal/provider/softwareupgrade":                "IOSXESoftwareUpgrade",
    "github.com/cisco/virtual-kubelet-cisco/internal/provider/operationalaction":              "IOSXEOperationalAction",
    # Driver / transport packages — bucket against the CRD whose
    # reconciler exercises them most. Operators viewing the report
    # want CRD-level signal, not "transport-X passes".
    "github.com/cisco/virtual-kubelet-cisco/internal/drivers":                                  "CiscoDevice",
    "github.com/cisco/virtual-kubelet-cisco/internal/drivers/common":                          "CiscoDevice",
    "github.com/cisco/virtual-kubelet-cisco/internal/drivers/iosxe":                           "CiscoDevice",
    "github.com/cisco/virtual-kubelet-cisco/internal/drivers/iosxe/configdriver":              "IOSXEConfig",
    "github.com/cisco/virtual-kubelet-cisco/internal/drivers/iosxe/configdriver/engine":       "IOSXEConfig",
    "github.com/cisco/virtual-kubelet-cisco/internal/drivers/iosxe/configdriver/intent":       "IOSXEConfig",
    "github.com/cisco/virtual-kubelet-cisco/internal/drivers/iosxe/configdriver/schema":       "IOSXEConfig",
    "github.com/cisco/virtual-kubelet-cisco/internal/drivers/iosxe/configdriver/transport":    "IOSXEConfig",
    "github.com/cisco/virtual-kubelet-cisco/internal/drivers/iosxe/configdriver/writers":      "IOSXEConfig",
    "github.com/cisco/virtual-kubelet-cisco/internal/drivers/iosxe/telemetry":                 "IOSXETelemetry",
    "github.com/cisco/virtual-kubelet-cisco/internal/drivers/iosxe/devicegrpc":                "(shared) gRPC pool",
    "github.com/cisco/virtual-kubelet-cisco/internal/drivers/iosxe/gnoi":                      "(shared) gNOI client",
    # Telemetry / mapper packages — pre-PR shape.
    "github.com/cisco/virtual-kubelet-cisco/internal/telemetry/classifier":                    "IOSXETelemetry",
    "github.com/cisco/virtual-kubelet-cisco/internal/telemetry/correlation":                   "IOSXETelemetry",
    "github.com/cisco/virtual-kubelet-cisco/internal/telemetry/emit":                          "IOSXETelemetry",
    "github.com/cisco/virtual-kubelet-cisco/internal/telemetry/mapper":                        "IOSXETelemetry",
    "github.com/cisco/virtual-kubelet-cisco/internal/telemetry/state":                         "IOSXETelemetry",
    "github.com/cisco/virtual-kubelet-cisco/internal/telemetry/yang":                          "IOSXETelemetry",
}

# Stable display order for the per-CRD table.
CRD_ORDER = [
    "CiscoDevice",
    "IOSXEConfig",
    "IOSXEConfigBundle",
    "IOSXEConfigDefaults",
    "IOSXEConfigRevision",
    "IOSXEConfigApplyLog",
    "IOSXEDiagnostic",
    "IOSXETelemetry",
    "IOSXEDeviceGroupConfig",
    "IOSXEInterfaceGroupConfig",
    "IOSXETemplate",
    "DeviceOperation",
    "IOSXESoftwareUpgrade",
    "IOSXEOperationalAction",
    "(shared) gRPC pool",
    "(shared) gNOI client",
    "Other",
]

# Single-line marker the lab Argo scenarios emit. See
# cvk-gitops/environments/lab/cvk-cicd/cat{8kv,9k}-pr-test-template.yaml.
SCENARIO_MARKER_RE = re.compile(
    r"::cvk-scenario:: crd=(?P<crd>\S+) name=(?P<name>\S+) phase=(?P<phase>\S+)"
    r"(?:\s+duration=(?P<duration>\d+))?"
)


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


def http_get_bytes(url: str) -> bytes | None:
    """Authenticated GET returning raw response body. None on any error."""
    try:
        req = urllib.request.Request(url, method="GET")
        req.add_header("Accept", "application/vnd.github+json")
        req.add_header("Authorization", f"Bearer {os.environ['GH_TOKEN']}")
        req.add_header("X-GitHub-Api-Version", "2022-11-28")
        with urllib.request.urlopen(req, timeout=60) as resp:
            return resp.read()
    except (urllib.error.HTTPError, urllib.error.URLError) as e:
        warn(f"http_get_bytes {url}: {e}")
        return None


def fetch_job_log_text(fork_repo: str, run_id: int) -> str:
    """Concatenated stdout of every job in the run.

    Used to grep ::cvk-scenario:: markers out of the Argo workflow
    output without depending on the artefact upload step (which the
    lab-ci workflows only do on failure today).
    """
    try:
        data = api(f"/repos/{fork_repo}/actions/runs/{run_id}/jobs") or {}
    except urllib.error.HTTPError as e:
        warn(f"fetch jobs {run_id}: {e}")
        return ""
    pieces: list[str] = []
    for job in data.get("jobs", []):
        job_id = job.get("id")
        if not job_id:
            continue
        body = http_get_bytes(f"{GITHUB_API}/repos/{fork_repo}/actions/jobs/{job_id}/logs")
        if body:
            pieces.append(body.decode("utf-8", errors="replace"))
    return "\n".join(pieces)


def fetch_artefact_file(fork_repo: str, run_id: int, artefact_name: str, file_in_zip: str) -> bytes | None:
    """Download a single file out of a named workflow artefact zip.

    The GitHub artefacts API returns a redirect to a temporary signed
    URL; urllib follows it automatically when authenticated. Returns
    None on any failure — the report degrades gracefully.
    """
    try:
        listing = api(f"/repos/{fork_repo}/actions/runs/{run_id}/artifacts") or {}
    except urllib.error.HTTPError as e:
        warn(f"list artefacts {run_id}: {e}")
        return None
    for art in listing.get("artifacts", []):
        if art.get("name") != artefact_name:
            continue
        zip_bytes = http_get_bytes(art.get("archive_download_url", ""))
        if not zip_bytes:
            return None
        try:
            with zipfile.ZipFile(io.BytesIO(zip_bytes)) as z:
                with z.open(file_in_zip) as f:
                    return f.read()
        except (zipfile.BadZipFile, KeyError) as e:
            warn(f"artefact {artefact_name} missing {file_in_zip}: {e}")
            return None
    return None


def parse_gotestsum_json(content: bytes) -> dict[str, dict[str, int]]:
    """Parse gotestsum's JSON event stream into per-package counts.

    Each line is one event with Action ∈ {run,pass,fail,skip,output,...}.
    We use the package-level summary events (Test field absent / empty)
    to derive {package: {pass, fail, skip, total}}. Falls back to
    counting individual test events when no package summary appears.
    """
    stats: dict[str, dict[str, int]] = {}
    by_test: dict[tuple[str, str], str] = {}
    for line in content.splitlines():
        line = line.strip()
        if not line or not line.startswith(b"{"):
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        pkg = ev.get("Package", "")
        action = ev.get("Action", "")
        test = ev.get("Test", "")
        if not pkg:
            continue
        if test:
            if action in ("pass", "fail", "skip"):
                by_test[(pkg, test)] = action
        else:
            # Package summary
            if action in ("pass", "fail", "skip"):
                stats.setdefault(pkg, {"pass": 0, "fail": 0, "skip": 0})
                # Aggregate from individual test events for the count;
                # the package-summary's Action alone tells us the
                # pass/fail of the package, not the count of tests.
    # Roll up per-test outcomes.
    for (pkg, _test), outcome in by_test.items():
        s = stats.setdefault(pkg, {"pass": 0, "fail": 0, "skip": 0})
        s[outcome] = s.get(outcome, 0) + 1
    return stats


def parse_scenario_markers(text: str) -> dict[tuple[str, str], dict[str, Any]]:
    """Extract the latest phase per (crd, scenario-name) tuple.

    Each Argo scenario emits two markers (running + final). We keep
    whichever is most recent in the log, so the end-of-run state wins.
    """
    out: dict[tuple[str, str], dict[str, Any]] = {}
    for m in SCENARIO_MARKER_RE.finditer(text):
        key = (m.group("crd"), m.group("name"))
        out[key] = {
            "crd": m.group("crd"),
            "name": m.group("name"),
            "phase": m.group("phase"),
            "duration": int(m.group("duration") or 0),
        }
    return out


def crd_for_package(pkg: str) -> str:
    """Bucket a Go package path into a CRD label, with longest-prefix match."""
    best = ""
    for candidate in PACKAGE_TO_CRD:
        if pkg == candidate or pkg.startswith(candidate + "/"):
            if len(candidate) > len(best):
                best = candidate
    return PACKAGE_TO_CRD.get(best, "Other") if best else "Other"


def render_crd_coverage(
    unit_stats: dict[str, dict[str, int]],
    cat8kv_markers: dict[tuple[str, str], dict[str, Any]],
    cat9k_markers: dict[tuple[str, str], dict[str, Any]],
) -> list[str]:
    """Produce the '### Coverage by CRD' section as a list of md lines."""
    # Unit-test aggregation: package counts → CRD totals.
    unit: dict[str, dict[str, int]] = {}
    for pkg, c in unit_stats.items():
        crd = crd_for_package(pkg)
        u = unit.setdefault(crd, {"pass": 0, "fail": 0, "skip": 0})
        u["pass"] += c.get("pass", 0)
        u["fail"] += c.get("fail", 0)
        u["skip"] += c.get("skip", 0)

    # Scenario aggregation: count per (crd, source).
    def bucket_scenarios(markers: dict[tuple[str, str], dict[str, Any]]) -> dict[str, dict[str, int]]:
        b: dict[str, dict[str, int]] = {}
        for v in markers.values():
            crd = v["crd"]
            phase = v["phase"]
            s = b.setdefault(crd, {"pass": 0, "fail": 0})
            if phase == "succeeded":
                s["pass"] += 1
            elif phase == "failed":
                s["fail"] += 1
            # phase=running implies the trap didn't fire → treat as failed
            else:
                s["fail"] += 1
        return b

    cat8kv = bucket_scenarios(cat8kv_markers)
    cat9k = bucket_scenarios(cat9k_markers)

    # Union of CRDs that appear in any bucket, ordered by CRD_ORDER.
    seen = set(unit) | set(cat8kv) | set(cat9k)
    ordered = [c for c in CRD_ORDER if c in seen] + sorted(seen - set(CRD_ORDER))

    if not ordered:
        return []  # nothing to render yet

    lines: list[str] = []
    lines.append("")
    lines.append("### Coverage by CRD")
    lines.append("")
    lines.append("| CRD | Unit | Cat8kv | Cat9k |")
    lines.append("| :--- | :---: | :---: | :---: |")
    for crd in ordered:
        u = unit.get(crd, {"pass": 0, "fail": 0, "skip": 0})
        c8 = cat8kv.get(crd, {"pass": 0, "fail": 0})
        c9 = cat9k.get(crd, {"pass": 0, "fail": 0})
        def cell(c: dict[str, int]) -> str:
            p, f = c.get("pass", 0), c.get("fail", 0)
            tot = p + f
            if tot == 0:
                return "—"
            mark = "✅" if f == 0 else "❌"
            return f"{p}/{tot} {mark}"
        lines.append(f"| `{crd}` | {cell(u)} | {cell(c8)} | {cell(c9)} |")
    lines.append("")
    return lines


def fetch_run_duration(fork_repo: str, run_id: int) -> str:
    """Format a workflow run's wall-clock duration.

    The commit-status's created_at/updated_at are both stamped at the moment
    the status is posted (single API call at workflow completion), so they
    can't be subtracted to get a meaningful duration. The actual run
    timing lives on the workflow run object as run_started_at → updated_at.
    """
    try:
        run = api(f"/repos/{fork_repo}/actions/runs/{run_id}") or {}
    except urllib.error.HTTPError as e:
        warn(f"fetch run {run_id}: {e}")
        return ""
    return fmt_duration(run.get("run_started_at"), run.get("updated_at"))


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
            tgt = s.get("target_url") or ""
            run_id = run_id_from_target_url(tgt)
            duration = fetch_run_duration(fork_repo, run_id) if run_id else ""
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

    # CRD-coverage section. Each block degrades to "—" cells when its
    # source data isn't available yet (e.g. unit-tests run before
    # gotestsum artefact uploads, Cat9k run before Argo scenario logs
    # are emitted).
    unit_stats: dict[str, dict[str, int]] = {}
    unit_run_id = None
    if (s := latest.get("lab-ci / unit-tests")):
        unit_run_id = run_id_from_target_url(s.get("target_url", ""))
    if unit_run_id:
        content = fetch_artefact_file(
            fork_repo, unit_run_id, "unit-test-results", "test-output.json"
        )
        if content:
            unit_stats = parse_gotestsum_json(content)

    def scenario_markers_for(ctx: str) -> dict[tuple[str, str], dict[str, Any]]:
        s = latest.get(ctx)
        if not s:
            return {}
        run_id = run_id_from_target_url(s.get("target_url", ""))
        if not run_id:
            return {}
        return parse_scenario_markers(fetch_job_log_text(fork_repo, run_id))

    cat8kv_markers = scenario_markers_for("lab-ci / cat8kv")
    cat9k_markers = scenario_markers_for("lab-ci / cat9k")

    lines.extend(render_crd_coverage(unit_stats, cat8kv_markers, cat9k_markers))

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
