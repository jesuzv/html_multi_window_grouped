import os
import re
import json
import html
import subprocess
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo

import requests

# ---------- Config ----------
# OWNER/REPO are auto-detected (Actions, env override, or local git remote)
SOURCE_BRANCH = "main"     # routes.txt lives here
OUTPUT_BRANCH = "gh-pages" # generated HTML goes here (GitHub Pages publishes this)

ROUTE_FILE_PATH = "routes.txt"
TARGET_DIR = ""            # keep "" to preserve URLs like /weekend_1_to_108.html

# Durable "already succeeded this week (Monday run)?" marker stored on gh-pages
STATE_PATH = ".run-state/last_success.json"

# London local time (weekly runs intended for Mondays)
TZ = ZoneInfo("Europe/London")
today_local: date = datetime.now(TZ).date()

# Group size (10 routes per HTML)
BATCH_SIZE = 10

# ---------- Repo detection ----------
def detect_owner_repo() -> tuple[str, str]:
    """
    Priority:
      1) Explicit override: GITHUB_OWNER + GITHUB_REPO
      2) GitHub Actions: GITHUB_REPOSITORY = "owner/repo"
      3) Local git: parse `git remote get-url origin`
    """
    owner = os.environ.get("GITHUB_OWNER")
    repo = os.environ.get("GITHUB_REPO")
    if owner and repo:
        return owner, repo

    gh_repo = os.environ.get("GITHUB_REPOSITORY")
    if gh_repo and "/" in gh_repo:
        o, r = gh_repo.split("/", 1)
        return o, r

    # Local fallback: try git remote
    try:
        url = subprocess.check_output(
            ["git", "remote", "get-url", "origin"],
            stderr=subprocess.DEVNULL,
            text=True,
        ).strip()

        # Handles:
        #   https://github.com/owner/repo.git
        #   git@github.com:owner/repo.git
        m = re.search(r"github\.com[:/](?P<owner>[^/]+)/(?P<repo>[^/.]+)", url)
        if m:
            return m.group("owner"), m.group("repo")
    except Exception:
        pass

    raise RuntimeError(
        "Could not detect owner/repo. Set env vars GITHUB_OWNER and GITHUB_REPO, "
        "or run inside a git repo with an 'origin' remote, or on GitHub Actions."
    )

# ---------- Helpers ----------
def safe_name(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", s)

def url_for(route: str, direction: str) -> str:
    r = route.strip()
    return f"https://tfl.gov.uk/bus/status/?input={r}&lineIds={r}&direction={direction}"

def day_bounds_encoded(day: date):
    d = day.strftime("%Y-%m-%d")
    return f"{d}T00%3A00%3A00", f"{d}T23%3A59%3A59"

def weekend_bounds_encoded(today: date):
    wd = today.weekday()  # Mon=0..Sun=6
    offset_to_sat = (5 - wd) % 7
    sat = today + timedelta(days=offset_to_sat)
    sun = sat + timedelta(days=1)
    s0, _ = day_bounds_encoded(sat)
    _, e1 = day_bounds_encoded(sun)
    return s0, e1, sat, sun  # Sat 00:00 .. Sun 23:59 + dates for debugging

# --- Weekly (Monday) marker helpers ---
def london_week_monday(d: date) -> date:
    # Monday for the week containing d (London date)
    return d - timedelta(days=d.weekday())

def london_week_monday_iso() -> str:
    return london_week_monday(datetime.now(TZ).date()).isoformat()

def already_succeeded_this_week(owner: str, repo: str, branch: str) -> bool:
    """
    Checks a committed marker file on the output branch to see if we already
    successfully published for THIS WEEK (keyed by London week Monday date).

    If the check fails (network, parse error), we DO NOT skip (safer to run again).
    """
    url = f"https://raw.githubusercontent.com/{owner}/{repo}/{branch}/{STATE_PATH}"
    try:
        r = requests.get(url, timeout=15)
        if r.status_code == 404:
            return False
        r.raise_for_status()
        data = r.json()
        return data.get("week_monday") == london_week_monday_iso() and data.get("status") == "success"
    except Exception:
        return False

def success_state_payload(range_hint: str, weekend_hint: str) -> str:
    return json.dumps(
        {
            "week_monday": london_week_monday_iso(),
            "status": "success",
            "range": range_hint,
            "weekend": weekend_hint,
            "updated_at": datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S %Z"),
        },
        ensure_ascii=False,
        indent=2,
    ) + "\n"

# ---------- Grouped weekend-only opener ----------
def open_plan_for_group(routes_group, today_for_weekend: date):
    """
    Returns a list of tuples: (label, url, delay_ms), preserving the exact opening order/timing.
    Grouped version:
      - Only THIS WEEKEND
      - 2 tabs per route (inbound + outbound)
      - Stagger per route
    """
    wk_start, wk_end, _, _ = weekend_bounds_encoded(today_for_weekend)
    plan = []

    for idx, route in enumerate(routes_group):
        base_in = url_for(route, "inbound")
        base_out = url_for(route, "outbound")
        w_in = f"{base_in}&startDate={wk_start}&endDate={wk_end}&dateTypeSelect=This%20weekend"
        w_out = f"{base_out}&startDate={wk_start}&endDate={wk_end}&dateTypeSelect=This%20weekend"

        delay_base = idx * 200
        plan.append((f"{route} — This weekend — inbound", w_in, delay_base))
        plan.append((f"{route} — This weekend — outbound", w_out, delay_base + 60))

    return plan

def html_for_group(
    routes_group,
    today_for_weekend: date,
) -> str:
    plan = open_plan_for_group(routes_group, today_for_weekend)

    labels = [lbl for (lbl, _, _) in plan]
    urls = [u for (_, u, _) in plan]
    delays = [d for (_, _, d) in plan]

    labels_js = json.dumps(labels, ensure_ascii=False)
    urls_js = json.dumps(urls, ensure_ascii=False)
    delays_js = json.dumps(delays)

    first_r, last_r = routes_group[0], routes_group[-1]
    title_esc = html.escape(f"Routes {first_r}–{last_r}")
    routes_list = ", ".join(html.escape(r) for r in routes_group)

    # Notes:
    # - Auto-open uses window.open(url)
    # - If anything is blocked, stop and show fallback.
    # - Fallback "Open all tabs" uses the same schedule, but ONLY after verifying popups are allowed.
    js = f"""
const labels = {labels_js};
const urls = {urls_js};
const delays = {delays_js};

let timeouts = [];

function clearSchedule() {{
  for (const id of timeouts) clearTimeout(id);
  timeouts = [];
}}

function setStatus(msg) {{
  const el = document.getElementById("status");
  if (el) el.textContent = msg;
}}

function renderPlannedList() {{
  const ol = document.getElementById("plannedList");
  if (!ol) return;
  ol.innerHTML = "";
  for (let i = 0; i < labels.length; i++) {{
    const li = document.createElement("li");

    const labelSpan = document.createElement("span");
    labelSpan.textContent = labels[i];

    const link = document.createElement("a");
    link.href = urls[i];
    link.target = "_blank";
    link.rel = "noopener";
    link.textContent = "open";

    li.appendChild(labelSpan);
    li.appendChild(document.createTextNode(" "));
    li.appendChild(link);
    ol.appendChild(li);
  }}
}}

function showFallback() {{
  clearSchedule();
  const box = document.getElementById("blocked");
  box.style.display = "block";
  document.getElementById("tabCount").textContent = String(urls.length);
  renderPlannedList();
  setStatus("Popups are blocked. Allow popups for this site, then click “Open all tabs” to open everything in order.");

  document.getElementById("openAll").onclick = () => {{
    setStatus("Checking popup permission…");
    // Must succeed on *this click* (user gesture). If blocked here, schedule will also fail.
    const test = window.open(urls[0]);
    if (!test) {{
      setStatus("Still blocked. Allow popups for this site first — otherwise the browser will open 0–1 tab.");
      return;
    }}

    setStatus("Popups allowed — opening tabs in order…");
    // We already opened index 0 in the permission test, so schedule the rest preserving spacing.
    scheduleFrom(1, delays[1] || 0);
  }};
}}

function scheduleFrom(startIdx, t0Delay) {{
  for (let i = startIdx; i < urls.length; i++) {{
    const d = Math.max(0, (delays[i] || 0) - t0Delay);
    const id = setTimeout(() => {{
      const w = window.open(urls[i]);
      if (!w) {{
        // If the browser starts blocking mid-stream, fall back immediately.
        showFallback();
      }}
    }}, d);
    timeouts.push(id);
  }}
}}

window.onload = function() {{
  const w0 = window.open(urls[0]);
  if (!w0) {{
    showFallback();
    return;
  }}
  scheduleFrom(1, delays[1] || 0);
}};
""".strip()

    wk_start, wk_end, sat, sun = weekend_bounds_encoded(today_for_weekend)
    # wk_start / wk_end are encoded; the date is still obvious in the YYYY-MM-DD prefix.
    wk_hint = f"{wk_start[:10]} → {wk_end[:10]} (London)"

    return f"""<!doctype html><html lang="en">
<head><meta charset="utf-8"><title>{title_esc}</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  body {{ font-family: system-ui, -apple-system, Segoe UI, Roboto, Arial, sans-serif; margin: 24px; line-height: 1.4; }}
  .blocked {{
    display:none; margin-top:16px; padding:12px 14px;
    border:1px solid #e5e5e5; border-radius:12px; background:#fafafa;
  }}
  button {{
    font-size:18px; padding:10px 14px; border-radius:12px;
    border:1px solid #ccc; cursor:pointer;
  }}
  ol {{ margin-top: 12px; padding-left: 20px; }}
  li {{ margin: 6px 0; }}
  a {{ margin-left: 6px; }}
  .note {{ margin-top:10px; color:#555; }}
  #status {{ margin-top:10px; color:#333; }}
</style>
<script>
{js}
</script>
</head>
<body>
<p>
  Opening “This weekend” tabs for routes {html.escape(first_r)} → {html.escape(last_r)}.<br>
  Routes in this batch: {routes_list}<br>
  • Weekend: {wk_hint}<br>
  • Both inbound and outbound directions.
</p>

<div id="blocked" class="blocked">
  <strong>Popups are blocked for this site.</strong><br>
  Planned tabs: <span id="tabCount">?</span>.<br>
  Allow popups for this site <em>first</em>, then click “Open all tabs” to open everything in order.<br>
  Otherwise the browser may open only 0–1 tab.<br><br>

  <button id="openAll">Open all tabs</button>
  <div id="status"></div>

  <p class="note">
    Manual option (intended order):
  </p>
  <ol id="plannedList"></ol>

  <p class="note">
    Best experience: allow popups for this site to enable the one-click opener.
  </p>
</div>
</body>
</html>"""

def index_html_exact(groups, generated_at, range_hint, total_routes):
    """
    Indexes grouped weekend pages.
    """
    items = []
    for g in groups:
        first_r, last_r = g[0], g[-1]
        fname = f"weekend_{safe_name(first_r)}_to_{safe_name(last_r)}.html"
        href = f"{TARGET_DIR}/{fname}" if TARGET_DIR else fname
        label = f"{first_r} → {last_r}"
        items.append(
            f'<li><a href="{href}" target="_blank" rel="noopener">{html.escape(label)}</a></li>'
        )
    items_html = "\n".join(items)

    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Weekend batches</title>

  <!-- Cache-busting hints (not perfect, but helps) -->
  <meta http-equiv="Cache-Control" content="no-cache, no-store, must-revalidate">
  <meta http-equiv="Pragma" content="no-cache">
  <meta http-equiv="Expires" content="0">

  <style>
    body {{ font-family: system-ui, -apple-system, Segoe UI, Roboto, Arial, sans-serif; margin: 24px; line-height: 1.4; }}
    .meta {{ color: #555; margin: 8px 0 16px; }}
    ul {{
      list-style: none;
      padding: 0;
      display: grid;
      grid-template-columns: repeat(auto-fill, minmax(180px, 1fr));
      gap: 8px;
    }}
    li {{ border: 1px solid #e5e5e5; border-radius: 10px; padding: 8px 10px; text-align: center; }}
    a {{ text-decoration: none; }}
    a:hover {{ text-decoration: underline; }}
  </style>
</head>
<body>
  <h1>Weekend batches</h1>
  <div class="meta">
    Generated: <strong>{generated_at}</strong><br>
    Weekend: <strong>{range_hint}</strong><br>
    Total routes: <strong>{total_routes}</strong><br>
    Total batch pages: <strong>{len(groups)}</strong>
  </div>

  <ul>
    {items_html}
  </ul>
</body>
</html>"""

# ---------- GitHub REST helpers ----------
API = "https://api.github.com"

def gh_req(session: requests.Session, method: str, url: str, **kwargs):
    r = session.request(method, url, **kwargs)
    if not r.ok:
        raise RuntimeError(f"{method} {url} -> {r.status_code}\n{r.text}")
    return r.json()

def _get_ref_or_none(gh: requests.Session, url: str):
    r = gh.get(url)
    if r.status_code == 404:
        return None
    if not r.ok:
        raise RuntimeError(f"GET {url} -> {r.status_code}\n{r.text}")
    return r.json()

def ensure_output_branch_exists(gh: requests.Session, owner: str, repo: str, branch: str) -> str:
    """
    Ensures OUTPUT_BRANCH exists. If missing, creates it pointing at SOURCE_BRANCH HEAD.
    Returns the branch HEAD commit SHA.
    """
    ref_url = f"{API}/repos/{owner}/{repo}/git/ref/heads/{branch}"
    ref = _get_ref_or_none(gh, ref_url)
    if ref is not None:
        return ref["object"]["sha"]

    # Create OUTPUT_BRANCH from SOURCE_BRANCH head
    src_ref = gh_req(gh, "GET", f"{API}/repos/{owner}/{repo}/git/ref/heads/{SOURCE_BRANCH}")
    src_sha = src_ref["object"]["sha"]

    gh_req(gh, "POST", f"{API}/repos/{owner}/{repo}/git/refs", json={
        "ref": f"refs/heads/{branch}",
        "sha": src_sha,
    })
    return src_sha

def main():
    owner, repo = detect_owner_repo()

    # 0) Skip if one of the earlier Monday runs already succeeded this week (London week Monday)
    if already_succeeded_this_week(owner, repo, OUTPUT_BRANCH):
        print("Skip: already succeeded this week (per gh-pages state file).")
        return

    # 1) Load routes.txt from main (exact order)
    raw_url = f"https://raw.githubusercontent.com/{owner}/{repo}/{SOURCE_BRANCH}/{ROUTE_FILE_PATH}"
    resp = requests.get(raw_url, timeout=30)
    resp.raise_for_status()

    routes = [ln.strip() for ln in resp.text.splitlines() if ln.strip()]
    if not routes:
        raise RuntimeError("routes.txt loaded but contained no routes.")
    if len(routes) != len(set(routes)):
        raise RuntimeError("Duplicate routes found in routes.txt.")

    # Compute weekend hint from London run date (intended Monday run)
    wk_start, wk_end, sat, sun = weekend_bounds_encoded(today_local)
    range_hint = f"{wk_start[:10]} → {wk_end[:10]} (London)"
    weekend_hint = f"{sat.isoformat()} → {sun.isoformat()}"

    generated_at = datetime.now(TZ).strftime("%Y-%m-%d %H:%M %Z")

    # 2) Build output files (GROUPED)
    groups = [routes[i:i + BATCH_SIZE] for i in range(0, len(routes), BATCH_SIZE)]
    files_to_commit = {}

    for g in groups:
        first_r, last_r = g[0], g[-1]
        page_html = html_for_group(g, today_local)
        fname = f"weekend_{safe_name(first_r)}_to_{safe_name(last_r)}.html"
        relpath = f"{TARGET_DIR}/{fname}" if TARGET_DIR else fname
        files_to_commit[relpath] = page_html

    files_to_commit["index.html"] = index_html_exact(groups, generated_at, range_hint, len(routes))

    # 2b) Update success marker (committed to gh-pages; used for skip logic)
    files_to_commit[STATE_PATH] = success_state_payload(range_hint, weekend_hint)

    # 3) Auth (use GitHub Actions token or local PAT)
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if not token:
        raise RuntimeError(
            "Missing token. On GitHub Actions this is GITHUB_TOKEN. "
            "Locally, set GH_TOKEN to a PAT with repo permissions."
        )

    gh = requests.Session()
    gh.headers.update({
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    })

    # 4) Ensure gh-pages exists; get its HEAD commit SHA
    out_head_sha = ensure_output_branch_exists(gh, owner, repo, OUTPUT_BRANCH)

    # 5) Create tree containing ONLY published site files
    tree_items = [{"path": p, "mode": "100644", "type": "blob", "content": c}
                  for p, c in files_to_commit.items()]

    commit_message = (
        f"Weekly weekend grouped update: {range_hint} ({len(routes)} routes, {len(groups)} pages + index)"
    )

    new_tree = gh_req(gh, "POST", f"{API}/repos/{owner}/{repo}/git/trees", json={
        "tree": tree_items
    })
    new_tree_sha = new_tree["sha"]

    # 6) Create commit
    new_commit = gh_req(gh, "POST", f"{API}/repos/{owner}/{repo}/git/commits", json={
        "message": commit_message,
        "tree": new_tree_sha,
        "parents": [out_head_sha],
    })
    new_commit_sha = new_commit["sha"]

    # 7) Update gh-pages ref
    gh_req(gh, "PATCH", f"{API}/repos/{owner}/{repo}/git/refs/heads/{OUTPUT_BRANCH}", json={
        "sha": new_commit_sha,
        "force": False
    })

    # Example URL shape:
    # https://{owner}.github.io/{repo}/weekend_1_to_108.html
    print(f"OK: committed {len(groups)} grouped pages + index to {OUTPUT_BRANCH}: {new_commit_sha}")

if __name__ == "__main__":
    main()
