#!/usr/bin/env python3
"""List a GitHub user's repos, sorted by stars, views, clones and more.

GitHub only keeps 14 days of traffic data, so this script stores every run in a
local SQLite database. Run it regularly (cron, Task Scheduler) and the "views_all"
and "clones_all" columns become true cumulative totals, and the star delta column
shows growth over time.

Examples:
    python github_repos.py                          # sorted by stars, then views
    python github_repos.py --sort views_all         # cumulative tracked views
    python github_repos.py --sort stars_delta       # fastest growing (needs history)
    python github_repos.py --no-traffic --no-db     # public data only, no token needed
    python github_repos.py --json > repos.json
    python github_repos.py --star-history CppKAI   # stars per month for one repo
    python github_repos.py --sources CppKAI        # referrers and popular paths

Auth (first match wins): GITHUB_TOKEN, GH_TOKEN, then `gh auth token`.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from pathlib import Path

import requests

API = "https://api.github.com"
DEFAULT_USER = "cschladetsch"
DEFAULT_DB = Path.home() / ".github_repos.db"
MAX_WAIT = 120  # never sleep longer than this on a rate limit

SORT_KEYS = [
    "stars", "stars_delta", "views", "views_all", "uniques",
    "clones", "clones_all", "forks", "issues", "updated", "name",
]
NUMERIC = {"stars", "stars_delta", "forks", "issues", "views", "views_all",
           "uniques", "clones", "clones_all"}
CSV_FIELDS = [
    "name", "url", "stars", "stars_delta", "forks", "issues", "views", "views_all",
    "uniques", "clones", "clones_all", "language", "license", "topics", "updated",
    "fork", "private", "archived",
]


# --------------------------------------------------------------------------- auth

def get_token():
    for var in ("GITHUB_TOKEN", "GH_TOKEN"):
        if os.environ.get(var):
            return os.environ[var]
    if shutil.which("gh"):
        try:
            out = subprocess.run(["gh", "auth", "token"], capture_output=True,
                                 text=True, timeout=10)
            if out.returncode == 0 and out.stdout.strip():
                return out.stdout.strip()
        except (OSError, subprocess.SubprocessError):
            pass
    return None


# ------------------------------------------------------------------------ HTTP layer

def rate_limit_wait(resp, now=None):
    """Seconds to wait before retrying a rate-limited response, or None if it is
    not a rate limit (for example a plain 403 for missing push access)."""
    if resp.status_code not in (403, 429):
        return None
    retry_after = resp.headers.get("Retry-After")
    if retry_after and retry_after.isdigit():
        return min(int(retry_after) + 1, MAX_WAIT)
    if resp.headers.get("X-RateLimit-Remaining") == "0":
        reset = resp.headers.get("X-RateLimit-Reset")
        if reset and reset.isdigit():
            delta = int(reset) - (now if now is not None else time.time())
            return min(max(delta, 0) + 1, MAX_WAIT)
    if "rate limit" in (resp.text or "").lower():
        return 30
    return None


class GitHub:
    """Thin requests wrapper with retry and rate-limit backoff."""

    def __init__(self, token=None, max_retries=5):
        self.session = requests.Session()
        self.session.headers.update({
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "github-repos-list",
        })
        if token:
            self.session.headers["Authorization"] = f"Bearer {token}"
        self.authed = bool(token)
        self.max_retries = max_retries

    def get(self, url, params=None, accept=None):
        headers = {"Accept": accept} if accept else None
        resp = None
        for attempt in range(self.max_retries + 1):
            last = attempt == self.max_retries
            try:
                resp = self.session.get(url, params=params, headers=headers, timeout=30)
            except requests.RequestException:
                if last:
                    raise
                time.sleep(min(2 ** attempt, 30))
                continue
            wait = rate_limit_wait(resp)
            if wait is not None and not last:
                time.sleep(wait)
                continue
            if resp.status_code >= 500 and not last:
                time.sleep(min(2 ** attempt, 30))
                continue
            return resp
        return resp


def paginate(gh, url, params=None, accept=None):
    while url:
        resp = gh.get(url, params=params, accept=accept)
        resp.raise_for_status()
        yield from resp.json()
        url = resp.links.get("next", {}).get("url")
        params = None  # the next URL already carries its query string


# ------------------------------------------------------------------------ fetching

def fetch_repos(gh, user, include_forks=True, include_archived=True):
    # /user/repos includes private repos, but only when authenticated as the owner.
    url = f"{API}/user/repos" if gh.authed else f"{API}/users/{user}/repos"
    params = {"per_page": 100, "type": "owner", "sort": "updated"}
    repos = [r for r in paginate(gh, url, params)
             if r["owner"]["login"].lower() == user.lower()]
    if not include_forks:
        repos = [r for r in repos if not r["fork"]]
    if not include_archived:
        repos = [r for r in repos if not r.get("archived")]
    return repos


def fetch_traffic(gh, full_name):
    """Return (data, error). data is None when traffic is not accessible, and error
    is then the HTTP status as a string, so failures are never mistaken for zeros."""
    data = {}
    for kind in ("views", "clones"):
        try:
            resp = gh.get(f"{API}/repos/{full_name}/traffic/{kind}")
        except requests.RequestException as exc:
            return None, type(exc).__name__
        if resp.status_code != 200:
            return None, str(resp.status_code)
        body = resp.json()
        data[kind] = {
            "count": body.get("count", 0),
            "uniques": body.get("uniques", 0),
            "days": body.get(kind, []),
        }
    return data, None


def fetch_all_traffic(gh, repos, workers=8):
    names = [r["full_name"] for r in repos]
    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(lambda n: fetch_traffic(gh, n), names))
    return dict(zip(names, results))


def fetch_star_dates(gh, full_name):
    dates = []
    for item in paginate(gh, f"{API}/repos/{full_name}/stargazers",
                         {"per_page": 100}, accept="application/vnd.github.star+json"):
        dates.append(item["starred_at"][:10])
    return sorted(dates)


def monthly_cumulative(dates):
    """[(YYYY-MM, new_stars, running_total), ...] from a list of YYYY-MM-DD strings."""
    per_month = Counter(d[:7] for d in dates)
    out, total = [], 0
    for month in sorted(per_month):
        total += per_month[month]
        out.append((month, per_month[month], total))
    return out


# ----------------------------------------------------------------------- history DB

SCHEMA = """
CREATE TABLE IF NOT EXISTS traffic (
    repo TEXT NOT NULL, day TEXT NOT NULL, kind TEXT NOT NULL,
    count INTEGER NOT NULL, uniques INTEGER NOT NULL,
    PRIMARY KEY (repo, day, kind)
);
CREATE TABLE IF NOT EXISTS stars (
    repo TEXT NOT NULL, day TEXT NOT NULL, stars INTEGER NOT NULL,
    PRIMARY KEY (repo, day)
);
"""


def open_db(path):
    conn = sqlite3.connect(str(path))
    conn.executescript(SCHEMA)
    return conn


def store_traffic(conn, repo, data):
    """Merge a 14-day window into history. Days that have since dropped out of the
    window are kept; days present again (the latest one may be partial) are replaced."""
    for kind in ("views", "clones"):
        for d in data[kind]["days"]:
            conn.execute(
                "INSERT OR REPLACE INTO traffic VALUES (?, ?, ?, ?, ?)",
                (repo, d["timestamp"][:10], kind, d["count"], d["uniques"]))


def store_stars(conn, repo, stars, day):
    conn.execute("INSERT OR REPLACE INTO stars VALUES (?, ?, ?)", (repo, day.isoformat(), stars))


def traffic_total(conn, repo, kind):
    row = conn.execute("SELECT SUM(count), COUNT(*) FROM traffic WHERE repo=? AND kind=?",
                       (repo, kind)).fetchone()
    return row[0] if row and row[1] else None


def star_delta(conn, repo, current, days, today):
    """Stars gained since the newest snapshot that is at least `days` old, or None
    if no snapshot that old exists yet."""
    cutoff = (today - timedelta(days=days)).isoformat()
    row = conn.execute("SELECT stars FROM stars WHERE repo=? AND day<=? ORDER BY day DESC LIMIT 1",
                       (repo, cutoff)).fetchone()
    return None if row is None else current - row[0]


# ------------------------------------------------------------------ rows and sorting

def build_rows(repos, traffic, conn, days, today):
    """Combine API data, this run's traffic and stored history into flat rows."""
    rows = []
    for r in repos:
        full = r["full_name"]
        stars = r["stargazers_count"]
        data = (traffic.get(full) or (None, None))[0]

        delta = None
        if conn is not None:
            delta = star_delta(conn, full, stars, days, today)  # before today's snapshot
            store_stars(conn, full, stars, today)
            if data:
                store_traffic(conn, full, data)

        views_all = traffic_total(conn, full, "views") if conn is not None else None
        clones_all = traffic_total(conn, full, "clones") if conn is not None else None
        lic = (r.get("license") or {}).get("spdx_id") or ""
        rows.append({
            "name": r["name"],
            "url": r["html_url"],
            "stars": stars,
            "stars_delta": delta,
            "forks": r["forks_count"],
            "issues": r["open_issues_count"],
            "views": data["views"]["count"] if data else None,
            "views_all": views_all,
            "uniques": data["views"]["uniques"] if data else None,
            "clones": data["clones"]["count"] if data else None,
            "clones_all": clones_all,
            "language": r["language"] or "",
            "license": "" if lic == "NOASSERTION" else lic,
            "topics": r.get("topics") or [],
            "updated": (r.get("pushed_at") or "")[:10],
            "fork": bool(r["fork"]),
            "private": bool(r["private"]),
            "archived": bool(r.get("archived")),
        })
    if conn is not None:
        conn.commit()
    return rows


def sort_rows(rows, key):
    """Sort descending by key; ties fall back to stars, then views, then name."""
    rows = sorted(rows, key=lambda r: r["name"].lower())
    if key == "name":
        return rows

    def missing(v):
        return -1 if v is None else v

    return sorted(rows, key=lambda r: (missing(r[key]), r["stars"], missing(r["views"])),
                  reverse=True)  # stable, so remaining ties stay in name order


# --------------------------------------------------------------------------- output

def flags(row):
    return ("F" if row["fork"] else "") + ("P" if row["private"] else "") + \
           ("A" if row["archived"] else "")


def render_table(rows, days):
    cols = [("name", "name"), ("stars", "stars"), ("stars_delta", f"+{days}d"),
            ("forks", "forks"), ("issues", "issues"), ("views", "views"),
            ("views_all", "views_all"), ("uniques", "uniques"), ("clones", "clones"),
            ("clones_all", "clones_all"), ("language", "language"),
            ("updated", "updated"), ("flags", "flags")]

    def cell(row, key):
        v = flags(row) if key == "flags" else row[key]
        return "-" if v is None else str(v)

    widths = [max([len(h)] + [len(cell(r, k)) for r in rows]) for k, h in cols]

    def line(values):
        parts = []
        for (k, _), w, v in zip(cols, widths, values):
            parts.append(v.rjust(w) if k in NUMERIC else v.ljust(w))
        return "  ".join(parts).rstrip()

    out = [line([h for _, h in cols]), "  ".join("-" * w for w in widths)]
    out += [line([cell(r, k) for k, _ in cols]) for r in rows]
    return "\n".join(out)


def write_csv(path, rows):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=CSV_FIELDS, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({**r, "topics": ";".join(r["topics"])})


def print_star_history(gh, full_name):
    rows = monthly_cumulative(fetch_star_dates(gh, full_name))
    if not rows:
        print(f"{full_name}: no stars")
        return
    print(f"{'month':<8}{'new':>6}{'total':>8}")
    for month, new, total in rows:
        print(f"{month:<8}{new:>6}{total:>8}")


def print_sources(gh, full_name):
    for title, endpoint, label in (("Referrers", "referrers", "referrer"),
                                   ("Popular paths", "paths", "path")):
        resp = gh.get(f"{API}/repos/{full_name}/traffic/popular/{endpoint}")
        if resp.status_code != 200:
            print(f"{title}: unavailable (HTTP {resp.status_code})")
            continue
        items = resp.json()
        print(f"\n{title} (last 14 days)")
        if not items:
            print("  none")
            continue
        width = max(len(str(i[label])) for i in items)
        for i in items:
            print(f"  {str(i[label]).ljust(width)}  {i['count']:>6} views  {i['uniques']:>5} unique")


# ------------------------------------------------------------------------------ main

def parse_args(argv=None):
    ap = argparse.ArgumentParser(description="List GitHub repos sorted by stars, views and more.")
    ap.add_argument("--user", default=DEFAULT_USER)
    ap.add_argument("--sort", default="stars", choices=SORT_KEYS)
    ap.add_argument("--days", type=int, default=30, help="window for the star delta column")
    ap.add_argument("--db", default=str(DEFAULT_DB), help="history database path")
    ap.add_argument("--no-db", action="store_true", help="do not read or write history")
    ap.add_argument("--no-traffic", action="store_true", help="skip views and clones")
    ap.add_argument("--no-forks", action="store_true")
    ap.add_argument("--no-archived", action="store_true")
    ap.add_argument("--top", type=int, default=0, help="show only the first N rows")
    ap.add_argument("--csv", help="also write results to a CSV file")
    ap.add_argument("--json", action="store_true", help="print JSON instead of a table")
    ap.add_argument("--star-history", metavar="REPO", help="stars per month for one repo")
    ap.add_argument("--sources", metavar="REPO", help="referrers and popular paths for one repo")
    return ap.parse_args(argv)


def qualify(repo, user):
    return repo if "/" in repo else f"{user}/{repo}"


def main(argv=None):
    args = parse_args(argv)
    gh = GitHub(get_token())

    try:
        if args.star_history:
            print_star_history(gh, qualify(args.star_history, args.user))
            return 0
        if args.sources:
            if not gh.authed:
                print("Referrers and paths need a token.", file=sys.stderr)
                return 1
            print_sources(gh, qualify(args.sources, args.user))
            return 0

        repos = fetch_repos(gh, args.user, not args.no_forks, not args.no_archived)
    except requests.HTTPError as exc:
        code = exc.response.status_code if exc.response is not None else "?"
        hint = " (bad or expired token?)" if code == 401 else ""
        print(f"GitHub API error: HTTP {code}{hint}", file=sys.stderr)
        return 1
    except requests.RequestException as exc:
        print(f"Network error: {exc}", file=sys.stderr)
        return 1

    traffic = {}
    if args.no_traffic:
        pass
    elif not gh.authed:
        print("No token found (GITHUB_TOKEN, GH_TOKEN or `gh auth login`): "
              "skipping views and clones.", file=sys.stderr)
    else:
        traffic = fetch_all_traffic(gh, repos)
        errors = Counter(err for _, err in traffic.values() if err)
        if errors:
            detail = ", ".join(f"HTTP {code}: {n}" for code, n in sorted(errors.items()))
            print(f"Traffic unavailable for {sum(errors.values())} repos ({detail}); "
                  "shown as '-'.", file=sys.stderr)

    conn = None if args.no_db else open_db(args.db)
    try:
        rows = build_rows(repos, traffic, conn, args.days, date.today())
    finally:
        if conn is not None:
            conn.close()

    rows = sort_rows(rows, args.sort)
    if args.top:
        rows = rows[:args.top]

    if args.json:
        json.dump(rows, sys.stdout, indent=2)
        print()
    else:
        print(render_table(rows, args.days))
        total_stars = sum(r["stars"] for r in rows)
        total_views = sum(r["views"] or 0 for r in rows)
        print(f"\n{len(rows)} repos, {total_stars} stars, {total_views} views (last 14 days)")

    if args.csv:
        write_csv(args.csv, rows)
        print(f"Wrote {args.csv}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
