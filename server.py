#!/usr/bin/env python3
"""
Local web UI สำหรับเช็คว่า branch ปลายทางมี "ของ" จากทุก MR ในไฟล์ CSV ครบไหม

รันแบบ local เท่านั้น (ไม่ได้ deploy ขึ้นเน็ต) — เปิดเบราว์เซอร์ไปที่ http://127.0.0.1:8765
Token ที่กรอกในหน้าเว็บจะถูกส่งมาที่ server ตัวนี้ (ซึ่งรันอยู่บนเครื่องคุณเอง) เท่านั้น
แล้ว server เป็นคนยิงไปหา GitLab API ต่อ (กันปัญหา CORS จากการยิง fetch ตรงจากเบราว์เซอร์)

วิธีรัน:
    python3 server.py
"""

import collections
import csv
import io
import json
import queue
import re
import ssl
import threading
import urllib.parse
import urllib.request
import urllib.error
from concurrent.futures import ThreadPoolExecutor, as_completed
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MR_URL_RE = re.compile(r"^https://([^/]+)/(.+)/-/merge_requests/(\d+)")

INDEX_HTML_PATH = __file__.rsplit("/", 1)[0] + "/index.html"

# The token must only ever be sent to the one GitLab host the user actually
# means to query — never to an arbitrary host smuggled into an MR "URL" in
# the CSV (or in a "View diff" request). A CSV always lists MRs from one
# GitLab instance, so within a single /api/check run we pin the request to
# whichever host most of its MR URLs point to (majority, not "the first
# entry", so a single injected row can't hijack the pin on its own) and
# refuse to contact anything else. /api/mr-diff has no CSV of its own to
# pin against, so it's only ever allowed a host that a /api/check run in
# this same server process already vetted.
_vetted_hosts_lock = threading.Lock()
_vetted_hosts = set()


def _url_host(url):
    m = MR_URL_RE.match(url)
    return m.group(1) if m else None


def _remember_vetted_host(host):
    with _vetted_hosts_lock:
        _vetted_hosts.add(host)


def _is_vetted_host(host):
    with _vetted_hosts_lock:
        return host in _vetted_hosts

# Live "which API is being called" log, for the /api/check stream. Each MR is
# checked on its own worker thread (see check_mr_with_context below), and
# that thread sets _log_ctx.queue for the duration of that one MR's check —
# gitlab_get / gitlab_get_raw push a line onto it right before every request.
_log_ctx = threading.local()


def _log_api_call(method_and_path):
    q = getattr(_log_ctx, "queue", None)
    label = getattr(_log_ctx, "label", "")
    if q is not None:
        q.put({"type": "log", "message": f"{label}{method_and_path}" if label else method_and_path})


def _ssl_context(insecure):
    if not insecure:
        return None
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def gitlab_get(host, token, path_and_query, insecure=False):
    _log_api_call(f"GET /{path_and_query}")
    url = f"https://{host}/api/v4/{path_and_query}"
    req = urllib.request.Request(url, headers={"PRIVATE-TOKEN": token})
    ctx = _ssl_context(insecure)
    try:
        with urllib.request.urlopen(req, timeout=20, context=ctx) as resp:
            return json.loads(resp.read().decode("utf-8")), None
    except urllib.error.HTTPError as e:
        try:
            body = json.loads(e.read().decode("utf-8"))
            msg = body.get("message", str(e))
        except Exception:
            msg = str(e)
        return None, f"HTTP {e.code}: {msg}"
    except urllib.error.URLError as e:
        return None, f"connection error: {e.reason}"
    except Exception as e:
        return None, str(e)


def gitlab_get_raw(host, token, path_and_query, insecure=False):
    """Like gitlab_get but returns the raw response body as text (not JSON-parsed)."""
    _log_api_call(f"GET /{path_and_query}")
    url = f"https://{host}/api/v4/{path_and_query}"
    req = urllib.request.Request(url, headers={"PRIVATE-TOKEN": token})
    ctx = _ssl_context(insecure)
    try:
        with urllib.request.urlopen(req, timeout=20, context=ctx) as resp:
            return resp.read().decode("utf-8", errors="replace"), None
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None, "not found on that ref"
        return None, f"HTTP {e.code}: {e.reason}"
    except urllib.error.URLError as e:
        return None, f"connection error: {e.reason}"
    except Exception as e:
        return None, str(e)


# Scans a cell for every MR-URL-shaped substring, regardless of what (if
# anything) separates them — handles newline/comma/semicolon/space-separated
# URLs, and also URLs pasted with zero separator between them (e.g.
# "...merge_requests/109https://gitlab.../merge_requests/108"). The path part
# is non-greedy so it stops at the first "/-/merge_requests/<digits>" it
# finds rather than swallowing into the next URL.
_MR_URL_FINDALL_RE = re.compile(r"https?://[^\s,;]+?/-/merge_requests/\d+")

# Jira-style ticket key, e.g. "CXFT-7734" — matched inside either a raw key
# or a full Jira URL like ".../browse/CXFT-7734".
_TICKET_KEY_RE = re.compile(r"[A-Z][A-Z0-9]+-\d+")


def extract_mr_urls_from_csv(csv_text):
    """
    Returns a list of {"team": ..., "ticket": ..., "url": ...} associations —
    one per (ticket row, MR) pair. The same url CAN appear more than once
    (one MR closing several tickets), and that's intentional: each ticket
    still gets its own row in the results, even though the MR itself only
    gets checked against GitLab once (see _handle_check_stream, which groups
    associations by url before calling the API).

    The "Team" column in this CSV format is only filled in on the first row
    of each group and left blank on the rows below it, so a blank cell means
    "same team as the row above" — forward-fill it rather than reading blank.

    Both the "MR" column AND the "Ticket" column can *also* be merged cells
    spanning several rows within the same team block — one MR resolving
    several tickets at once (Promo/CXFT-7734 case), or one ticket having
    several MRs at once (Account/CXFT-7276 case: one ticket cell merged
    across many MR rows). Either way, when such a sheet is exported to CSV,
    a merged cell's value only lives in the block's *first* row but visually
    centers on a row further down, which is easy to misread. So a blank MR
    or Ticket cell inherits the most recent non-blank value of that same
    column within the current team block, and both reset to nothing at the
    start of each new team block (a blank Team cell doesn't start a new
    block, since it means "still the same team").
    """
    reader = csv.reader(io.StringIO(csv_text))
    rows = list(reader)
    if not rows:
        return []
    header = [h.strip().lower() for h in rows[0]]
    try:
        mr_col = header.index("mr")
    except ValueError:
        mr_col = 3  # fallback: 4th column, 0-indexed
    try:
        team_col = header.index("team")
    except ValueError:
        team_col = None
    try:
        ticket_col = header.index("ticket")
    except ValueError:
        ticket_col = None

    associations = []
    seen_pairs = set()  # (ticket, url) — guards against re-inheriting the same pair on a padding row
    current_team = ""
    current_mr_cell = ""
    current_ticket = ""
    current_ticket_url = None
    for row in rows[1:]:
        if not any(c.strip() for c in row):
            # Fully blank row — e.g. trailing padding, or a gap before a
            # leftover formula/summary row (like a stray "Count: N" cell).
            # A real merged cell in the spreadsheet never has a genuinely
            # empty row inside it, so this is a reliable "section ended"
            # signal — reset everything instead of letting some later,
            # unrelated row inherit a ticket/MR from across the gap.
            current_team = ""
            current_mr_cell = ""
            current_ticket = ""
            current_ticket_url = None
            continue

        if team_col is not None and team_col < len(row):
            cell_team = row[team_col].strip()
            if cell_team:
                current_team = cell_team
                current_mr_cell = ""       # new team block — nothing to inherit yet
                current_ticket = ""
                current_ticket_url = None

        own_ticket_cell = row[ticket_col].strip() if (ticket_col is not None and ticket_col < len(row)) else ""
        if own_ticket_cell:
            m = _TICKET_KEY_RE.search(own_ticket_cell)
            current_ticket = m.group(0) if m else own_ticket_cell
            current_ticket_url = own_ticket_cell if own_ticket_cell.startswith("http") else None
        ticket = current_ticket
        ticket_url = current_ticket_url

        # One cell can have several MRs pasted together — with a separator
        # (newline/comma/semicolon/space) or, sometimes, none at all. Scan
        # for URL-shaped substrings instead of splitting on a delimiter, so
        # nothing after the first MR silently gets dropped.
        own_mr_cell = row[mr_col].strip() if mr_col < len(row) else ""
        if own_mr_cell:
            own_candidates = _MR_URL_FINDALL_RE.findall(own_mr_cell)
            if own_candidates:
                # Real MR url(s) — use them, and remember for forward-fill.
                current_mr_cell = own_mr_cell
                candidates = own_candidates
            else:
                # Cell has text but it's not a URL — e.g. a note like "no mr"
                # or "N/A". That's an explicit signal, not an empty cell, so
                # it must NOT inherit the previous row's MR, and must NOT be
                # remembered for later rows to inherit either.
                candidates = []
        else:
            # Genuinely empty cell — inherit whatever MR was last seen in
            # this team block (the merged-cell heuristic).
            candidates = _MR_URL_FINDALL_RE.findall(current_mr_cell) if current_mr_cell else []

        if not candidates:
            # Ticket listed but no MR anywhere in this team block so far —
            # e.g. a ticket that doesn't need code (config-only, docs, etc),
            # or the cell just has a note like "no mr" instead of a URL.
            # Still surface it instead of silently dropping it, so "team X
            # has N tickets" in the CSV actually shows N rows.
            if ticket and (ticket, None) not in seen_pairs:
                seen_pairs.add((ticket, None))
                associations.append({"team": current_team, "ticket": ticket, "ticket_url": ticket_url, "url": None})
            continue

        for candidate in candidates:
            # Skip a (ticket, url) pair we've already recorded — this is what
            # catches a "padding" row where both Ticket and MR are blank (but
            # some other column, like MR Status, isn't) so the row doesn't
            # get caught by the fully-blank-row check above, yet it inherits
            # the exact same ticket+MR pair as a row we already emitted.
            pair = (ticket, candidate)
            if pair in seen_pairs:
                continue
            seen_pairs.add(pair)
            associations.append({"team": current_team, "ticket": ticket, "ticket_url": ticket_url, "url": candidate})
    return associations


def branch_contains_ref(host, token, enc_path, sha, ref_name, insecure):
    """
    Return True if `ref_name` branch's history already contains commit `sha`
    (i.e. `sha` is an ancestor of `ref_name`) — equivalent to
    `git merge-base --is-ancestor <sha> <ref_name>`.

    Uses the compare API (a direct two-ref check) rather than listing every
    branch that contains the commit — the "list branches containing this
    commit" endpoint caps out at 100 results and doesn't paginate further, so
    on a commit that's an ancestor of many branches (e.g. the tip of a busy
    long-lived branch like "development", which is the base for lots of
    feature branches) the one branch we actually care about can silently be
    missing from that first page — giving a wrong/unstable answer depending
    on how many other branches happen to also contain that commit.
    """
    enc_ref = urllib.parse.quote(ref_name, safe="")
    result, err = gitlab_get(
        host, token,
        f"projects/{enc_path}/repository/compare?from={enc_ref}&to={sha}",
        insecure,
    )
    if err:
        return None, err
    commits = (result or {}).get("commits") or []
    # No commits "ahead" of ref_name on the way to sha means sha is already
    # reachable from ref_name.
    return (len(commits) == 0), None


def branch_head_sha(host, token, enc_path, branch_name, insecure):
    """Return the current tip commit sha of `branch_name`."""
    enc_branch = urllib.parse.quote(branch_name, safe="")
    info, err = gitlab_get(host, token, f"projects/{enc_path}/repository/branches/{enc_branch}", insecure)
    if err:
        return None, err
    sha = (info or {}).get("commit", {}).get("id")
    if not sha:
        return None, "branch has no commit info"
    return sha, None


def _added_lines_from_diff(diff_text):
    """Extract non-blank added lines (the '+' side) from a unified diff, skipping headers."""
    lines = []
    for line in (diff_text or "").splitlines():
        if line.startswith("+++") or line.startswith("---"):
            continue
        if line.startswith("+"):
            stripped = line[1:].strip()
            if stripped:
                lines.append(stripped)
    return lines


def get_mr_diff_files(host, token, enc_path, iid, target_branch, insecure):
    """
    Return the MR's per-file unified diffs, plus (when target_branch is given)
    the current line set of that same file on target_branch, so the UI can
    show — line by line — whether the MR's added code is actually there yet.
    """
    changes, err = gitlab_get(host, token, f"projects/{enc_path}/merge_requests/{iid}/changes", insecure)
    if err:
        return None, f"could not fetch MR diff: {err}"

    file_changes = (changes or {}).get("changes", [])
    enc_target = urllib.parse.quote(target_branch, safe="") if target_branch else None

    files = []
    for ch in file_changes:
        path = ch.get("new_path") or ch.get("old_path")
        entry = {
            "path": path,
            "old_path": ch.get("old_path"),
            "new_file": bool(ch.get("new_file")),
            "deleted_file": bool(ch.get("deleted_file")),
            "renamed_file": bool(ch.get("renamed_file")),
            "diff": ch.get("diff", ""),
            "target_lines": None,
            "target_error": None,
        }

        if enc_target and not ch.get("deleted_file"):
            enc_file = urllib.parse.quote(path, safe="")
            raw, ferr = gitlab_get_raw(
                host, token,
                f"projects/{enc_path}/repository/files/{enc_file}/raw?ref={enc_target}",
                insecure,
            )
            if ferr:
                entry["target_error"] = ferr
            else:
                entry["target_lines"] = list({l.strip() for l in raw.splitlines() if l.strip()})

        files.append(entry)

    return {"title": changes.get("title", ""), "files": files}, None


def diff_content_check(host, token, enc_path, iid, target_branch, insecure):
    """
    Best-effort fallback when we can't confirm via commit ancestry (no sha, or
    sha not found in target): compare the MR's added lines per file against
    the current content of that file on the target branch. This can still
    detect that "the code is there" even if the original commit was squashed,
    rebased, or cherry-picked (git hash no longer matches).

    This is NOT a rename/move-aware diff — it's a line-presence heuristic,
    so treat the result as a hint, not a guarantee.
    """
    changes, err = gitlab_get(host, token, f"projects/{enc_path}/merge_requests/{iid}/changes", insecure)
    if err:
        return None, f"could not fetch MR diff: {err}"

    file_changes = (changes or {}).get("changes", [])
    if not file_changes:
        return None, "MR has no file changes to compare"

    # Cap how many files we'll fetch-and-compare per MR — a huge MR (dozens of
    # files) would otherwise fire that many extra API calls just for one row,
    # which is what made the whole check feel like it hung.
    MAX_FILES = 15
    truncated = len(file_changes) > MAX_FILES
    file_changes = file_changes[:MAX_FILES]

    total = 0
    matched = 0
    per_file_notes = []
    enc_target = urllib.parse.quote(target_branch, safe="")

    for ch in file_changes:
        if ch.get("deleted_file"):
            continue
        path = ch.get("new_path") or ch.get("old_path")
        added = _added_lines_from_diff(ch.get("diff", ""))
        if not added:
            continue

        enc_file = urllib.parse.quote(path, safe="")
        raw, ferr = gitlab_get_raw(
            host, token,
            f"projects/{enc_path}/repository/files/{enc_file}/raw?ref={enc_target}",
            insecure,
        )
        if ferr:
            per_file_notes.append(f"{path}: {ferr}")
            continue

        target_lines = {l.strip() for l in raw.splitlines() if l.strip()}
        file_matched = sum(1 for l in added if l in target_lines)
        total += len(added)
        matched += file_matched
        if file_matched < len(added):
            per_file_notes.append(f"{path}: {file_matched}/{len(added)} added lines found")

    if total == 0:
        return None, "no added-line content to compare (only deletions/renames, or files unreadable)"

    if truncated:
        per_file_notes.append(f"only checked first {MAX_FILES} changed files")

    return {"matched": matched, "total": total, "notes": per_file_notes}, None


def _status_from_diff_result(diff_result, target_branch):
    """Turn a diff_content_check() result into a (status, human note) pair."""
    matched, total = diff_result["matched"], diff_result["total"]
    ratio = matched / total
    note_suffix = f" ({'; '.join(diff_result['notes'][:3])})" if diff_result["notes"] else ""

    if ratio >= 0.9:
        return "OK", (
            f"diff check found {matched}/{total} added lines already present in "
            f"{target_branch} (content match, not a hash match){note_suffix}"
        )
    if ratio == 0:
        return "MISS", (
            f"diff check found 0/{total} added lines in {target_branch} — "
            f"content is likely NOT there yet{note_suffix}"
        )
    return "WARN", (
        f"diff check found only {matched}/{total} added lines in {target_branch} — "
        f"partial match, verify manually{note_suffix}"
    )


def _branch_mismatch_diff_note(host, token, enc_path, iid, target_branch, insecure):
    """
    Human-readable note for the case where the MR's own target branch differs
    from the target_branch we're checking against: does the MR's code content
    actually match/differ from what's currently on target_branch?
    """
    diff_result, derr = diff_content_check(host, token, enc_path, iid, target_branch, insecure)
    if derr:
        return f"diff check vs {target_branch}: {derr}"

    matched, total = diff_result["matched"], diff_result["total"]
    ratio = matched / total
    note_suffix = f" ({'; '.join(diff_result['notes'][:3])})" if diff_result["notes"] else ""

    if ratio >= 0.9:
        return f"diff check vs {target_branch}: content matches ({matched}/{total} added lines found)"
    if ratio == 0:
        return f"diff check vs {target_branch}: content DIFFERS (0/{total} added lines found){note_suffix}"
    return f"diff check vs {target_branch}: content PARTIALLY differs ({matched}/{total} added lines found){note_suffix}"


def check_mr(url, token, target_branch, insecure):
    m = MR_URL_RE.match(url)
    if not m:
        return {"url": url, "status": "ERROR", "detail": "could not parse url"}

    host, project_path, iid = m.group(1), m.group(2), m.group(3)
    enc_path = urllib.parse.quote(project_path, safe="")

    mr, err = gitlab_get(host, token, f"projects/{enc_path}/merge_requests/{iid}", insecure)
    if err:
        return {"url": url, "iid": iid, "project": project_path, "status": "ERROR", "detail": err}

    state = mr.get("state")
    title = mr.get("title", "")

    # What ticket keys does this MR actually reference? Looked up regardless
    # of what the CSV says, so a mismatch can show "here's what IS in the MR"
    # instead of just "doesn't match".
    haystack = " ".join([
        title,
        mr.get("description") or "",
        mr.get("source_branch") or "",
    ])
    mr_tickets = sorted(set(_TICKET_KEY_RE.findall(haystack.upper())))

    if state != "merged":
        return {
            "url": url, "iid": iid, "project": project_path, "title": title, "mr_tickets": mr_tickets,
            "status": "SKIP", "detail": f"state={state} (not merged yet)",
        }

    # merge_commit_sha is only set when GitLab actually creates a merge
    # commit. Projects using squash + fast-forward merge have NO merge
    # commit at all — the real landed commit is in squash_commit_sha instead.
    # Falling back straight to `sha` (the pre-squash source-branch tip) would
    # never be found as an ancestor of target (squash rewrites history),
    # causing a false MISS for MRs that really did land.
    sha = mr.get("merge_commit_sha") or mr.get("squash_commit_sha") or mr.get("sha")
    if not sha:
        diff_result, derr = diff_content_check(host, token, enc_path, iid, target_branch, insecure)
        if derr:
            return {
                "url": url, "iid": iid, "project": project_path, "title": title, "mr_tickets": mr_tickets,
                "status": "WARN",
                "detail": "\n".join(["no commit sha from API", f"diff check also failed: {derr}"]),
            }
        status, note = _status_from_diff_result(diff_result, target_branch)
        return {
            "url": url, "iid": iid, "project": project_path, "title": title, "mr_tickets": mr_tickets,
            "status": status, "detail": "\n".join(["no commit sha from API", note]),
        }

    mr_target_branch = mr.get("target_branch")

    in_target, err = branch_contains_ref(host, token, enc_path, sha, target_branch, insecure)
    if err:
        return {
            "url": url, "iid": iid, "project": project_path, "title": title, "mr_tickets": mr_tickets,
            "status": "ERROR", "detail": f"could not check refs: {err}",
        }

    if in_target:
        lines = [f"commit {sha[:8]} is already in {target_branch}"]
        if mr_target_branch and mr_target_branch != target_branch:
            lines.append(f"via feature branch '{mr_target_branch}', which has been merged into {target_branch}")
            lines.append(_branch_mismatch_diff_note(host, token, enc_path, iid, target_branch, insecure))
        return {
            "url": url, "iid": iid, "project": project_path, "title": title, "mr_tickets": mr_tickets,
            "status": "OK", "detail": "\n".join(lines),
        }

    lines = [f"commit {sha[:8]} is NOT in {target_branch} yet"]

    # MR wasn't opened directly against the target branch — it went into some
    # feature/intermediate branch instead. Check whether *that* branch has
    # since made it into the target branch, to explain why the commit is missing.
    if mr_target_branch and mr_target_branch != target_branch:
        feature_sha, ferr = branch_head_sha(host, token, enc_path, mr_target_branch, insecure)
        if ferr:
            note = " (likely deleted)" if "not found" in ferr else ""
            lines.append(f"merged into '{mr_target_branch}' — could not check if that branch reached {target_branch}: {ferr}{note}")
        else:
            feature_in_target, ferr2 = branch_contains_ref(host, token, enc_path, feature_sha, target_branch, insecure)
            if ferr2:
                lines.append(f"merged into '{mr_target_branch}' — could not check if that branch reached {target_branch}: {ferr2}")
            elif feature_in_target:
                lines.append(
                    f"merged into '{mr_target_branch}', which IS already in {target_branch} — "
                    f"this commit may have been squashed/rebased, check manually"
                )
            else:
                lines.append(f"merged into '{mr_target_branch}', which has NOT been merged into {target_branch} yet")
        lines.append(_branch_mismatch_diff_note(host, token, enc_path, iid, target_branch, insecure))
    else:
        # MR was opened directly against the target branch, yet ancestry says
        # MISS — the commit could've been squashed/rebased into a different
        # sha. Do a best-effort content check before giving up.
        diff_result, derr = diff_content_check(host, token, enc_path, iid, target_branch, insecure)
        if derr:
            lines.append(f"diff check: {derr}")
        else:
            _, note = _status_from_diff_result(diff_result, target_branch)
            lines.append(f"diff check: {note}")

    return {
        "url": url, "iid": iid, "project": project_path, "title": title, "mr_tickets": mr_tickets,
        "status": "MISS", "detail": "\n".join(lines),
    }


def check_mr_logged(url, token, target_branch, insecure, log_queue, index, total):
    """
    Runs check_mr for one *unique* MR url with live progress reporting: every
    gitlab_get/gitlab_get_raw call made during this MR's check pushes a "log"
    line onto log_queue (via the thread-local set here). Does NOT know about
    team/ticket — a single MR can resolve several tickets at once (an
    explicitly repeated url, or a merged MR cell spanning several ticket
    rows), so attaching team/ticket/ticket_match per association happens
    afterwards in _handle_check_stream, once per association sharing this url.
    """
    _log_ctx.queue = log_queue
    _log_ctx.label = f"[{index}/{total}] "
    try:
        return check_mr(url, token, target_branch, insecure)
    finally:
        _log_ctx.queue = None
        _log_ctx.label = ""


def _attach_association(base_result, entry):
    """Copy a shared check_mr result and stamp it with one association's team/ticket."""
    result = dict(base_result)
    result["team"] = entry["team"]
    ticket = entry["ticket"]
    result["ticket"] = ticket
    result["ticket_url"] = entry.get("ticket_url")
    if not ticket:
        result["ticket_match"] = None
    else:
        result["ticket_match"] = ticket.upper() in (result.get("mr_tickets") or [])
    return result


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass  # silence access log

    def _send_json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/" or self.path == "/index.html":
            with open(INDEX_HTML_PATH, "rb") as f:
                body = f.read()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        if self.path == "/api/mr-diff":
            self._handle_mr_diff()
            return
        if self.path != "/api/check":
            self.send_response(404)
            self.end_headers()
            return

        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length)
        try:
            payload = json.loads(raw.decode("utf-8"))
        except Exception:
            self._send_json({"error": "invalid json body"}, 400)
            return

        token = payload.get("token", "").strip()
        target_branch = payload.get("target_branch", "").strip()
        csv_text = payload.get("csv_text", "")
        insecure = bool(payload.get("insecure_ssl", False))

        if not token or not target_branch or not csv_text:
            self._send_json({"error": "token, target branch, and csv file are all required"}, 400)
            return

        entries = extract_mr_urls_from_csv(csv_text)
        self._handle_check_stream(token, target_branch, entries, insecure)

    def _write_ndjson(self, obj):
        try:
            self.wfile.write((json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8"))
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass  # client navigated away mid-stream — nothing to do

    def _handle_check_stream(self, token, target_branch, entries, insecure):
        """
        Streams newline-delimited JSON as MRs are checked, instead of one big
        response at the end — so the page can show a live "which API is being
        called" log plus a running X/Y (%) progress count. Each line is one of:
          {"type": "start", "total": N}
          {"type": "log", "message": "..."}      — one GitLab API call fired
          {"type": "result", "result": {...}}     — one MR fully checked
          {"type": "done", "summary": {...}}
        """
        total = len(entries)  # one per ticket association — what the user sees as "rows"

        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()

        self._write_ndjson({"type": "start", "total": total})

        if total == 0:
            self._write_ndjson({"type": "done", "summary": {
                "total": 0, "ok": 0, "miss": 0, "skip": 0, "warn": 0, "error": 0, "no_mr": 0,
            }})
            return

        # Group associations by url so each unique MR is only checked once
        # against GitLab, even if several tickets reference it (an explicitly
        # repeated url, or a merged MR cell spanning several ticket rows) —
        # every association still gets its own result row in the output.
        no_mr_entries = [e for e in entries if e["url"] is None]

        hosts = [h for h in (_url_host(e["url"]) for e in entries if e["url"]) if h]
        expected_host = collections.Counter(hosts).most_common(1)[0][0] if hosts else None
        if expected_host:
            _remember_vetted_host(expected_host)

        by_url = {}
        host_mismatch_entries = []
        for e in entries:
            if e["url"] is None:
                continue
            if expected_host and _url_host(e["url"]) != expected_host:
                host_mismatch_entries.append(e)
                continue
            by_url.setdefault(e["url"], []).append(e)
        unique_urls = list(by_url.keys())

        log_queue = queue.Queue()
        results = []

        def _worker():
            for entry in no_mr_entries:
                result = _attach_association({
                    "url": None, "iid": None, "project": None, "title": "", "mr_tickets": [],
                    "status": "NO_MR", "detail": "No MR listed for this ticket in the CSV.",
                }, entry)
                results.append(result)
                log_queue.put({"type": "result", "result": result})

            for entry in host_mismatch_entries:
                bad_host = _url_host(entry["url"])
                result = _attach_association({
                    "url": entry["url"], "iid": None, "project": None, "title": "", "mr_tickets": [],
                    "status": "ERROR",
                    "detail": (
                        f"refused: host '{bad_host}' does not match the other MR URLs in "
                        f"this CSV ('{expected_host}') — not queried, to avoid sending your "
                        f"token to an unexpected host"
                    ),
                }, entry)
                results.append(result)
                log_queue.put({"type": "result", "result": result})

            # These are independent, I/O-bound (network) checks — run them
            # concurrently so a CSV with many MRs doesn't take forever end-to-end.
            with ThreadPoolExecutor(max_workers=8) as pool:
                futures = {
                    pool.submit(check_mr_logged, url, token, target_branch, insecure, log_queue, i + 1, len(unique_urls)): url
                    for i, url in enumerate(unique_urls)
                }
                for f in as_completed(futures):
                    url = futures[f]
                    base_result = f.result()
                    for entry in by_url[url]:
                        result = _attach_association(base_result, entry)
                        results.append(result)
                        log_queue.put({"type": "result", "result": result})
            log_queue.put(None)  # sentinel: everything is done

        threading.Thread(target=_worker, daemon=True).start()

        while True:
            item = log_queue.get()
            if item is None:
                break
            self._write_ndjson(item)

        summary = {
            "total": len(results),
            "ok": sum(1 for r in results if r["status"] == "OK"),
            "miss": sum(1 for r in results if r["status"] == "MISS"),
            "skip": sum(1 for r in results if r["status"] == "SKIP"),
            "warn": sum(1 for r in results if r["status"] == "WARN"),
            "error": sum(1 for r in results if r["status"] == "ERROR"),
            "no_mr": sum(1 for r in results if r["status"] == "NO_MR"),
        }
        self._write_ndjson({"type": "done", "summary": summary})

    def _handle_mr_diff(self):
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length)
        try:
            payload = json.loads(raw.decode("utf-8"))
        except Exception:
            self._send_json({"error": "invalid json body"}, 400)
            return

        token = payload.get("token", "").strip()
        url = payload.get("url", "").strip()
        target_branch = payload.get("target_branch", "").strip()
        insecure = bool(payload.get("insecure_ssl", False))

        if not token or not url:
            self._send_json({"error": "token and url are required"}, 400)
            return

        m = MR_URL_RE.match(url)
        if not m:
            self._send_json({"error": "could not parse MR url"}, 400)
            return
        host, project_path, iid = m.group(1), m.group(2), m.group(3)
        if not _is_vetted_host(host):
            self._send_json({"error": f"host '{host}' has not been checked via /api/check in this session — run Check now first"}, 400)
            return
        enc_path = urllib.parse.quote(project_path, safe="")

        result, err = get_mr_diff_files(host, token, enc_path, iid, target_branch, insecure)
        if err:
            self._send_json({"error": err}, 502)
            return

        self._send_json(result)


if __name__ == "__main__":
    import os
    import sys

    port = int(os.environ.get("PORT", sys.argv[1] if len(sys.argv) > 1 else 8766))
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"Open http://127.0.0.1:{port} in your browser  (Ctrl+C to stop)")
    server.serve_forever()
