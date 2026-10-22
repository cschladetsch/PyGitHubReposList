#!/usr/bin/env python3
"""List GitHub repos for a user, sorted by stars, views, etc.

Usage:
    python github_repos.py                       # user cschladetsch, sort by stars then views
    python github_repos.py --sort views          # sort by 14-day views
    python github_repos.py --no-traffic          # skip traffic calls (much faster, no token needed)
    python github_repos.py --csv repos.csv       # also write CSV
    python github_repos.py --top 50 --no-forks

Auth:
    Set GITHUB_TOKEN (classic PAT with `repo` scope, or fine-grained with
    "Administration: read" on the repos). Traffic data (views/clones) is only
    available to the repo owner and only covers the last 14 days.
"""
import argparse
import csv
import os
import sys
from concurrent.futures import ThreadPoolExecutor

import requests

API = "https://api.github.com"


def make_session(token):
    s = requests.Session()
    s.headers.update({
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "repo-lister",
    })
    if token:
        s.headers["Authorization"] = f"Bearer {token}"
    return s


def fetch_repos(s, user, authed):
    # /user/repos includes private repos when authenticated as the owner.
    url = f"{API}/user/repos" if authed else f"{API}/users/{user}/repos"
    params = {"per_page": 100, "type": "owner", "sort": "updated"}
    repos = []
    while url:
        r = s.get(url, params=params)
        r.raise_for_status()
        repos.extend(r.json())
        url = r.links.get("next", {}).get("url")
        params = None  # next URL already carries the query string
    return [r for r in repos if r["owner"]["login"].lower() == user.lower()]


def fetch_traffic(s, full_name):
    out = {"views": 0, "uniques": 0, "clones": 0}
    for kind in ("views", "clones"):
        r = s.get(f"{API}/repos/{full_name}/traffic/{kind}")
        if r.status_code == 200:
            j = r.json()
            out[kind] = j.get("count", 0)
            if kind == "views":
                out["uniques"] = j.get("uniques", 0)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--user", default="cschladetsch")
    ap.add_argument("--sort", default="stars",
                    choices=["stars", "views", "uniques", "clones", "forks", "issues", "updated", "name"])
    ap.add_argument("--no-traffic", action="store_true")
    ap.add_argument("--no-forks", action="store_true")
    ap.add_argument("--top", type=int, default=0)
    ap.add_argument("--csv")
    args = ap.parse_args()

    token = os.environ.get("GITHUB_TOKEN")
    s = make_session(token)

    repos = fetch_repos(s, args.user, bool(token))
    if args.no_forks:
        repos = [r for r in repos if not r["fork"]]

    traffic = {}
    if not args.no_traffic:
        if not token:
            print("No GITHUB_TOKEN set: skipping traffic (views/clones need auth).", file=sys.stderr)
        else:
            with ThreadPoolExecutor(max_workers=16) as ex:
                names = [r["full_name"] for r in repos]
                for name, t in zip(names, ex.map(lambda n: fetch_traffic(s, n), names)):
                    traffic[name] = t

    rows = []
    for r in repos:
        t = traffic.get(r["full_name"], {})
        rows.append({
            "name": r["name"],
            "stars": r["stargazers_count"],
            "forks": r["forks_count"],
            "issues": r["open_issues_count"],
            "views": t.get("views", 0),
            "uniques": t.get("uniques", 0),
            "clones": t.get("clones", 0),
            "language": r["language"] or "",
            "updated": r["pushed_at"][:10] if r.get("pushed_at") else "",
            "fork": "Y" if r["fork"] else "",
            "private": "Y" if r["private"] else "",
        })

    # Primary key from --sort, then stars, views, name as tiebreakers.
    key = args.sort
    if key == "name":
        rows.sort(key=lambda x: x["name"].lower())
    else:
        rows.sort(key=lambda x: (x[key], x["stars"], x["views"]), reverse=True)

    if args.top:
        rows = rows[:args.top]

    cols = ["name", "stars", "forks", "issues", "views", "uniques", "clones",
            "language", "updated", "fork", "private"]
    widths = {c: max(len(c), *(len(str(r[c])) for r in rows)) if rows else len(c) for c in cols}
    print("  ".join(c.ljust(widths[c]) for c in cols))
    print("  ".join("-" * widths[c] for c in cols))
    for r in rows:
        print("  ".join(str(r[c]).ljust(widths[c]) for c in cols))

    total_stars = sum(r["stars"] for r in rows)
    total_views = sum(r["views"] for r in rows)
    print(f"\n{len(rows)} repos, {total_stars} stars, {total_views} views (last 14 days)")

    if args.csv:
        with open(args.csv, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=cols)
            w.writeheader()
            w.writerows(rows)
        print(f"Wrote {args.csv}")


if __name__ == "__main__":
    main()