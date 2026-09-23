#!/usr/bin/env python3
"""HEOR Job Radar

Fetches internship postings from company career sites (Workday, Greenhouse,
Lever, Ashby), keeps the ones that look HEOR-related, and writes:
  docs/jobs.json    the job list the website reads
  docs/status.json  which sources worked on the last run
  new_jobs.md       new postings since the last run (used for the alert issue)
"""
import datetime as dt
import hashlib
import json
import pathlib
import re
import sys
import time
from urllib.parse import urlparse

import yaml

try:  # curl_cffi looks like a real browser, which some Workday sites require
    from curl_cffi import requests as http
    EXTRA = {"impersonate": "chrome"}
except ImportError:  # fall back to plain requests
    import requests as http
    EXTRA = {}

ROOT = pathlib.Path(__file__).resolve().parent
DOCS = ROOT / "docs"
JOBS_FILE = DOCS / "jobs.json"
STATUS_FILE = DOCS / "status.json"
NEW_FILE = ROOT / "new_jobs.md"
TIMEOUT = 30
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/126.0 Safari/537.36",
    "Accept": "application/json",
}
TODAY = dt.date.today().isoformat()


# ---------------------------------------------------------------- HTTP helpers
def get_json(url):
    r = http.get(url, headers=HEADERS, timeout=TIMEOUT, **EXTRA)
    r.raise_for_status()
    return r.json()


def post_json(url, body):
    h = dict(HEADERS, **{"Content-Type": "application/json"})
    r = http.post(url, json=body, headers=h, timeout=TIMEOUT, **EXTRA)
    r.raise_for_status()
    return r.json()


# ---------------------------------------------------------------- fetchers
# Each fetcher returns a list of {"title", "location", "url", "posted"}.

def fetch_greenhouse(src, cfg):
    data = get_json(f"https://boards-api.greenhouse.io/v1/boards/{src['board']}/jobs")
    return [{
        "title": j.get("title", ""),
        "location": (j.get("location") or {}).get("name", ""),
        "url": j.get("absolute_url", ""),
        "posted": (j.get("updated_at") or "")[:10],
    } for j in data.get("jobs", [])]


def fetch_lever(src, cfg):
    data = get_json(f"https://api.lever.co/v0/postings/{src['site']}?mode=json")
    out = []
    for j in data:
        ms = j.get("createdAt")
        posted = dt.datetime.utcfromtimestamp(ms / 1000).date().isoformat() if ms else ""
        out.append({
            "title": j.get("text", ""),
            "location": (j.get("categories") or {}).get("location", ""),
            "url": j.get("hostedUrl", ""),
            "posted": posted,
        })
    return out


def fetch_ashby(src, cfg):
    data = get_json(f"https://api.ashbyhq.com/posting-api/job-board/{src['board']}")
    return [{
        "title": j.get("title", ""),
        "location": j.get("location", ""),
        "url": j.get("jobUrl", ""),
        "posted": (j.get("publishedAt") or "")[:10],
    } for j in data.get("jobs", [])]


LOCALE = re.compile(r"^[a-z]{2}-[A-Z]{2}$")


def parse_workday(url):
    """Turn a Workday career-site URL into (api_endpoint, job_url_prefix)."""
    u = urlparse(url)
    host = f"{u.scheme}://{u.netloc}"
    parts = [p for p in u.path.split("/") if p and not LOCALE.match(p)]
    if "myworkdaysite.com" in u.netloc:  # shared host: /recruiting/<tenant>/<site>
        i = parts.index("recruiting")
        tenant, site = parts[i + 1], parts[i + 2]
        prefix = f"{host}/recruiting/{tenant}/{site}"
    else:                                # per-tenant host: <tenant>.wdN.myworkdayjobs.com/<site>
        tenant, site = u.netloc.split(".")[0], parts[0]
        prefix = f"{host}/{site}"
    return f"{host}/wday/cxs/{tenant}/{site}/jobs", prefix


def fetch_workday(src, cfg):
    endpoint, prefix = parse_workday(src["url"])
    terms = src.get("search") or cfg["keywords"].get("workday_search", ["intern"])
    max_pages = cfg.get("workday_max_pages", 25)
    out, seen = [], set()
    for term in terms:
        offset, total = 0, None
        for _ in range(max_pages):
            data = post_json(endpoint, {"appliedFacets": {}, "limit": 20,
                                        "offset": offset, "searchText": term})
            if total is None:            # Workday only reports the total on page 1
                total = data.get("total", 0)
            posts = data.get("jobPostings", [])
            for p in posts:
                path = p.get("externalPath", "")
                if not path or path in seen:
                    continue
                seen.add(path)
                out.append({
                    "title": p.get("title", ""),
                    "location": p.get("locationsText", ""),
                    "url": prefix + path,
                    "posted": p.get("postedOn", ""),
                })
            offset += 20
            # Workday wraps around instead of returning an empty page, so stop at total
            if not posts or offset >= total:
                break
            time.sleep(0.5)
    return out


FETCHERS = {"greenhouse": fetch_greenhouse, "lever": fetch_lever,
            "ashby": fetch_ashby, "workday": fetch_workday}


# ---------------------------------------------------------------- filtering
def has_any(text, terms):
    return any(t.lower() in text for t in terms)


def classify(title, kw):
    """Return 'heor', 'related', or None (not relevant)."""
    t = " " + title.lower() + " "
    if not has_any(t, kw["intern"]):
        return None
    if has_any(t, kw.get("exclude", [])):
        return None
    for y in kw.get("skip_years", []):
        if str(y) in t:
            return None
    if has_any(t, kw["heor"]):
        return "heor"
    if has_any(t, kw.get("related", [])):
        return "related"
    return None


def job_id(url):
    return hashlib.sha1(url.encode()).hexdigest()[:12]


# ---------------------------------------------------------------- main
def main():
    cfg = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
    kw = cfg["keywords"]

    first_run = not JOBS_FILE.exists()
    old = {} if first_run else {j["id"]: j for j in json.loads(JOBS_FILE.read_text())["jobs"]}

    fetched, status, ok_companies = {}, [], set()
    for src in cfg["sources"]:
        name, kind = src["company"], src["type"]
        row = {"company": name, "type": kind, "ok": False, "found": 0, "matched": 0, "error": ""}
        try:
            jobs = FETCHERS[kind](src, cfg)
            row["ok"], row["found"] = True, len(jobs)
            ok_companies.add(name)
            for j in jobs:
                tier = classify(j["title"], kw)
                if tier and j["url"]:
                    j.update(company=name, source=kind, tier=tier, id=job_id(j["url"]))
                    fetched[j["id"]] = j
                    row["matched"] += 1
        except Exception as e:  # one broken source should never stop the others
            row["error"] = f"{type(e).__name__}: {str(e)[:160]}"
        status.append(row)
        mark = "OK " if row["ok"] else "ERR"
        print(f"[{mark}] {name:<32} {kind:<10} found={row['found']:<5} matched={row['matched']:<3} {row['error']}")
        time.sleep(1)

    # merge with previous results
    merged, new = {}, []
    for jid, j in fetched.items():
        prev = old.get(jid)
        j["first_seen"] = prev["first_seen"] if prev else TODAY
        j["last_seen"], j["active"] = TODAY, True
        merged[jid] = j
        if not prev:
            new.append(j)
    keep_days = cfg.get("keep_closed_days", 45)
    for jid, j in old.items():
        if jid in merged:
            continue
        if j["company"] in ok_companies and j.get("active", True):
            j["active"], j["closed_on"] = False, TODAY   # source worked but posting is gone
        closed = j.get("closed_on")
        if closed and (dt.date.today() - dt.date.fromisoformat(closed)).days > keep_days:
            continue
        merged[jid] = j

    DOCS.mkdir(exist_ok=True)
    # newest first, closed postings at the bottom
    jobs = sorted(merged.values(), key=lambda j: j["first_seen"], reverse=True)
    jobs.sort(key=lambda j: not j.get("active", True))
    JOBS_FILE.write_text(json.dumps({"updated": TODAY, "jobs": jobs}, ensure_ascii=False, indent=1))
    STATUS_FILE.write_text(json.dumps({"updated": TODAY, "sources": status}, ensure_ascii=False, indent=1))

    if NEW_FILE.exists():
        NEW_FILE.unlink()
    if new and not first_run:
        lines = [f"今天找到 {len(new)} 筆新職缺：", ""]
        for j in sorted(new, key=lambda j: (j["tier"] != "heor", j["company"])):
            tag = "HEOR" if j["tier"] == "heor" else "相關"
            lines.append(f"- **{j['company']}** [{j['title']}]({j['url']}) ({tag}, {j['location'] or '地點未標示'})")
        NEW_FILE.write_text("\n".join(lines), encoding="utf-8")

    ok = sum(r["ok"] for r in status)
    print(f"\nSources OK: {ok}/{len(status)}  |  jobs listed: {len(jobs)}  |  new today: {len(new)}"
          + ("  (first run: no alert)" if first_run else ""))


if __name__ == "__main__":
    sys.exit(main())
