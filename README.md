# MR Branch Checker

A local web page that checks whether a target branch (e.g. a release branch)
already has every MR from a CSV file merged in.

It checks two things per MR:
1. Is the MR's state `merged`?
2. Is the MR's commit actually an ancestor of the target branch? (Catches
   cases where GitLab shows "merged" but the change is missing from the
   target branch because of a revert, force-push, or cherry-pick to the
   wrong branch.)

Runs as a local server on your own machine only — never deployed to the
internet. The token you type into the page is sent only to the server
running on `127.0.0.1`, and that server is the only thing that calls the
GitLab API — nothing else ever sees the token.

## Requirements

- Python 3 (already built into macOS — no install, no external dependencies)
- A GitLab Personal Access Token with `read_api` scope, with access to the
  project(s) the MRs live in

## How to run

```bash
cd mr-check-web
python3 server.py
```

Then open **http://127.0.0.1:8899** in your browser.

If port 8899 is already in use, override it:
```bash
python3 server.py 9000
# or
PORT=9000 python3 server.py
```

## How to use

1. Enter your GitLab Personal Access Token
2. Enter the target branch name, e.g. `release/3.7.0`
3. Choose a CSV file with a `MR` column containing merge request URLs
   (format: `https://<gitlab-host>/<group>/.../-/merge_requests/<iid>`)
4. Click "Check now"

While it runs, a progress bar and a live log show which GitLab API call is
currently in flight (e.g. `[7/22] GET /projects/.../merge_requests/11912`)
and a running `done/total (%)` count — so a big CSV doesn't look frozen, and
you can see which specific call is slow if one stalls. MRs are checked 8 at
a time concurrently, so log lines from different MRs interleave in whatever
order their API calls actually finish.

Once results come in:
- **Search** box + **Team** / **Project** dropdowns filter the table. Search
  matches ticket key, MR url/number, project, title, detail text, and team —
  all in one box. Filters combine with each other.
- The summary chips (**Total / OK / MISS / SKIP / WARN / ERROR / NO_MR**) are
  also clickable filters — click one to show only that status, click again
  (or click **Total**) to clear it.
- **"View diff"** on any row opens a modal with the MR's file-by-file diff,
  line numbers included. Each added line (`+`) is checked live against that
  file's current content on the target branch — **green** if already there,
  **orange** if not. This is a best-effort line match, not a byte-exact diff,
  so treat orange as "worth a second look," not a guarantee.

## Result statuses

| Status  | Meaning |
|---------|---------|
| `OK`    | MR is merged and its commit is already in the target branch |
| `MISS`  | MR is merged **but its commit isn't in the target branch yet** — needs follow-up |
| `SKIP`  | MR isn't merged yet |
| `WARN`  | No commit sha available, and the diff-based fallback found only a partial content match — needs a manual look |
| `ERROR` | GitLab API call failed (bad URL, token lacks access, connection error, etc.) |
| `NO_MR` | This ticket has no MR listed in the CSV at all — nothing to check |

## Handling messy CSVs

### Multiple MR URLs in one cell

If a ticket's `MR` cell has more than one URL — separated by newlines,
commas, semicolons, or even pasted with no separator at all — all of them
are picked up, not just the first.

### One MR resolving several tickets

Sometimes the same MR closes several tickets — either because the URL is
copy-pasted onto multiple rows, or because the `MR` cell is a merged cell in
the spreadsheet spanning several ticket rows. Either way, the MR is checked
against GitLab only **once**, and shown as **one row listing every ticket it
resolves** (e.g. `CXFT-9007` might list several MRs; `CXFT-10356` might share
one MR with another ticket) — rather than duplicating the row per ticket.

### Merged Team/Ticket/MR cells

The `Team` column in a typical export is only filled in on the first row of
a group, left blank below it — a blank cell there always means "same team as
the row above."

The `MR` and `Ticket` columns can *also* behave like merged cells: a blank
`MR` cell inherits the most recent MR seen earlier in the same team block
(one MR shared by several tickets), and a blank `Ticket` cell inherits the
most recent ticket the same way (one ticket with several MRs listed below
it). Both resets happen at the start of each new team block, and also
whenever a fully blank row is hit (a real merged cell never has a genuinely
empty row inside it, so a blank row reliably means "this section ended" —
without this reset, a stray leftover row like a pasted `Count: 72` formula
result at the bottom of a sheet could otherwise inherit an unrelated ticket
from far above it).

This inheritance only kicks in for a **truly empty** cell. If the cell has
*any* text that isn't a valid MR URL — a note like `no mr`, `N/A`, `TBD` — it
is **never** inherited from, and it's **never remembered** for a later row to
inherit either. That row is shown as `NO_MR` immediately.

If your CSV doesn't use merged cells this way and a blank `MR`/`Ticket` cell
should always mean "genuinely nothing," this heuristic could still
misattribute a distant earlier value to an unrelated row — it's a best
guess, not a guarantee, since a CSV export throws away the spreadsheet's
actual merge information.

## When an MR wasn't opened directly against the target branch

Some MRs go into a feature/intermediate branch first (e.g. merged into
`feature/x`, which is later merged into `main`). The tool compares the MR's
actual target branch (`target_branch` from the MR API) against the branch
you're checking:

- **`OK`**, but not opened directly against your target — the detail notes
  the path: `commit abc123 is already in main (via feature branch 'develop',
  which has been merged into main)`
- **`MISS`** — the tool checks further and explains why:
  - `merged into 'feature/x', which has NOT been merged into main yet` —
    the MR landed in the feature branch, but that branch hasn't reached
    target yet
  - `merged into 'feature/x', which IS already in main — this commit may
    have been squashed/rebased, check manually` — the feature branch did
    reach target, but this specific commit sha can't be found (usually a
    squash merge or rebase changed the sha)
  - `merged into 'feature/x' — could not check if that branch reached main:
    ... (likely deleted)` — the feature branch no longer exists on GitLab

This check uses the feature branch's **current** tip — if it was deleted and
recreated with different commits since this MR merged, it reflects the
branch as it is now, not necessarily its state back then.

Whenever the MR's target branch doesn't match yours (in either the `OK` or
`MISS` case), the tool also runs a content diff and appends it, e.g.:
`diff check vs main: content matches (1/1 added lines found)`. This directly
answers "the branches don't match — does the code actually differ?" instead
of relying only on branch names.

## Fallback: checking by diff content instead of commit hash

A commit hash comparison can't detect a match once the original commit no
longer exists as-is — squash merges, rebases, and cherry-picks all change the
sha. `merge_commit_sha` is only set when GitLab creates an actual merge
commit; a project using squash + fast-forward merge has none at all, so the
tool also checks `squash_commit_sha` before falling back further.

When a commit still can't be matched, the tool falls back to a content
check: it fetches the MR's added lines (the `+` side of its diff, per
changed file) and checks whether those exact lines already exist in that
file's current content on the target branch. This runs automatically when:

1. **No commit sha is available at all** — reports `OK`/`MISS`/`WARN` based
   on how much of the added content it found.
2. **The ancestry check says `MISS`** — as a second opinion, in case the
   commit was squashed/rebased into a different sha the ancestry check can't
   see. Status stays `MISS` either way (ancestry is authoritative); the diff
   result is just extra context.

This is a **line-presence heuristic, not a real diff/patch match** — it
doesn't account for renames, moved code, or reformatted whitespace, so treat
the result as a hint, not a guarantee.

## Which git operations this corresponds to

The tool never clones a repo — everything goes through the GitLab API, each
call standing in for a familiar git operation:

| What we want to know | Equivalent git command | GitLab API call |
|---|---|---|
| Is this MR merged? | checking MR state | `GET /projects/:id/merge_requests/:iid` |
| Is commit `<sha>` an ancestor of branch `<target>`? | `git merge-base --is-ancestor <sha> <target>` | `GET /projects/:id/repository/compare?from=<target>&to=<sha>` — an empty `commits` list means `<sha>` is already reachable from `<target>` |
| What's the current tip of branch `<name>`? | `git rev-parse <name>` (after `git fetch`) | `GET /projects/:id/repository/branches/:name` |

So the "has the feature branch itself reached target?" check is effectively:
```bash
git fetch origin
git rev-parse origin/<feature-branch>            # -> feature_head_sha
git merge-base --is-ancestor <feature_head_sha> origin/<target-branch>
```
done via the API instead of a local clone.

## Parsing the MR URL: which ID goes to which API call

Every check starts from one MR URL, e.g.:
```
https://gitlab.int.cardx.co.th/cardx/channel/frontend/mobile/cardx-app/-/merge_requests/12266
```

One regex (`MR_URL_RE` in `server.py`) pulls three pieces out, reused across
every API call for that row:

| Extracted | Example value | Used as | Meaning |
|---|---|---|---|
| host | `gitlab.int.cardx.co.th` | API base (`https://<host>/api/v4/...`) | which GitLab instance |
| project path | `cardx/channel/frontend/mobile/cardx-app` | URL-encoded `:id` | the **project** — GitLab accepts a numeric ID or this path, so no extra lookup is needed |
| iid | `12266` | `:iid` | the **MR's number within that project** — what you see in the URL/UI, not GitLab's separate global MR id |

Every following API call for that row reuses the same `host` + encoded
project path as `:id`, so calls can never leak across projects — each row is
always scoped to the project its own URL pointed to.

Endpoints called, and what each is for:

| Endpoint | IDs used | Why |
|---|---|---|
| `GET /projects/:id/merge_requests/:iid` | project id, MR iid | `state`, `title`, `target_branch`, `merge_commit_sha`/`squash_commit_sha`/`sha` |
| `GET /projects/:id/repository/compare?from=:branch&to=:sha` | project id, branch, commit sha | is the commit already an ancestor of that branch? (target branch, and the MR's own `target_branch` when relevant) |
| `GET /projects/:id/repository/branches/:name` | project id, branch name | current tip commit of a branch (for the feature-branch follow-up check) |
| `GET /projects/:id/merge_requests/:iid/changes` | project id, MR iid | per-file diff, for the content-based fallback and the "View diff" modal |
| `GET /projects/:id/repository/files/:file_path/raw?ref=:branch` | project id, file path, branch | current raw content of a file on a branch, compared line-by-line against the MR's added lines |

No endpoint ever needs the MR's global `id` (only project id + iid), and
none needs a local clone.

## Worked example

Checking `!12266` against target branch `release/3.7.0`, where the MR was
opened against `development` instead:

**1. Parse the URL:**
```
host         = gitlab.int.cardx.co.th
project path = cardx/channel/frontend/mobile/cardx-app
iid          = 12266
```

**2. Get the MR:**
```
GET .../projects/cardx%2Fchannel%2Ffrontend%2Fmobile%2Fcardx-app/merge_requests/12266
```
```json
{ "state": "merged", "target_branch": "development", "merge_commit_sha": "6157636..." }
```
`target_branch` is `development`, not `release/3.7.0` — this MR wasn't
opened directly against what we're checking.

**3. Is that commit already on `release/3.7.0`?**
```
GET .../repository/compare?from=release%2F3.7.0&to=6157636...
```
`{"commits": []}` (empty) → yes → **`OK`**.

**4. Note the path taken, and confirm with a content diff:**
```
GET .../merge_requests/12266/changes
GET .../repository/files/<path>/raw?ref=release%2F3.7.0
```

**Final result:**
```
Status: OK
Detail:
 - commit 6157636a is already in release/3.7.0
 - via feature branch 'development', which has been merged into release/3.7.0
 - diff check vs release/3.7.0: content matches (1/1 added lines found)
```

## Architecture

- `server.py` — Python standard-library HTTP server, no external dependencies
  - `GET /` serves `index.html`
  - `POST /api/check` accepts `{ token, target_branch, csv_text }`, parses
    the CSV for MR URLs, calls the GitLab API per MR, and streams back
    newline-delimited JSON results
- `index.html` — plain UI (vanilla HTML/CSS/JS, no framework), calls
  `/api/check` via `fetch`

## Principles

1. **Local only — never deploy to a public URL.** The GitLab instance is
   internal; deploying this tool to the open internet without auth would let
   anyone submit/intercept tokens through it. To share with the team, hand
   out this folder and have each person run `python3 server.py` on their own
   machine — don't stand it up as a shared service.
2. **Never store or log the token anywhere.** It's used for exactly one
   request and then discarded — never written to disk, printed, or logged.
3. **The GitLab API is only called from the server (`server.py`).** The
   browser never calls it directly — partly because the internal GitLab
   instance's CORS setup would likely block it anyway, and partly because
   the token is safer travelling only over a network path we control.
4. **No external dependencies, no build step.** Only the Python standard
   library and vanilla HTML/CSS/JS — this needs to run instantly with one
   command on any teammate's machine, nothing extra to install.
5. **Everyone enters their own token — no shared central token.** Keeps
   requests auditable per person, and avoids needing a central secret store.
