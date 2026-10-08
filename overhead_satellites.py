#!/usr/bin/env python3
"""Overhead's satellite list: the orbital elements and catalog every phone needs, fetched ONCE a day and published for
all of them, so the sources see one download instead of one per phone.

  overhead_satellites.py nightly            fetch today's elements, write the files, commit and push (what the Pi runs)
  overhead_satellites.py build [src] [out]  write the files from a folder holding active.csv + satcat.csv (no fetch)
  overhead_satellites.py fetch [dir]        download active.csv + satcat.csv into a folder (one request each)
  overhead_satellites.py prune              fold history older than KEEP_DAYS into one commit (nightly does this weekly)

Needs only the Python standard library and git. Three parts, so the fetch-and-build can run anywhere (a Vercel cron, a
GitHub Actions job) and only `publish` knows about this machine's git checkout:
  fetch(dir)        -> downloads, stops on anything but HTTP 200
  build(src, out)   -> elements/core.csv, elements/starlink.csv, catalog.csv, latest.json (commit left null)
  publish(day)      -> commits the files, fills latest.json's commit, pushes, purges jsDelivr's copy of latest.json
"""
from __future__ import annotations

import csv
import io
import json
import os
import subprocess
import sys
import urllib.request
from datetime import date, datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.abspath(__file__))
WORK = os.path.join(ROOT, "work")
REPO = "GumDropSoftware/overhead-satellites"
UA = {"User-Agent": "overhead-satellites (+https://overhead.gumdrop.space; one download a day)"}

# CelesTrak publishes US Space Force (18 SDS) tracking data. Its policy: download a set at most once per update (every
# 2 h for GP), stop on anything but 200, never retry a refusal. This job asks once a day, from one machine.
GP_URL = "https://celestrak.org/NORAD/elements/gp.php?GROUP=active&FORMAT=csv"
SATCAT_URL = "https://celestrak.org/pub/satcat.csv"
CATALOG_COLUMNS = ["NORAD_CAT_ID", "OBJECT_TYPE", "OWNER", "LAUNCH_DATE", "LAUNCH_SITE", "PERIOD", "INCLINATION", "APOGEE",
                   "PERIGEE", "RCS"]
PUBLISHED = {"latest.json", "catalog.csv"}  # plus everything under elements/
KEEP_DAYS = 30
MIN_OBJECTS = 5_000  # a file with fewer rows than this is a broken download, not a quiet sky


def log(message):
    print(f"{datetime.now(timezone.utc):%H:%M:%S} {message}", flush=True)


# ---------------------------------------------------------------- fetch

class Refused(Exception):
    """CelesTrak answered something other than 200: stop, don't retry before the next day's run."""


class Unreachable(Exception):
    """No answer at all (network down): the later run the same day may try again."""


def download(url, path):
    request = urllib.request.Request(url, headers=UA)
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            data = response.read()
    except urllib.error.HTTPError as error:
        raise Refused(f"{url} answered HTTP {error.code}") from error
    except urllib.error.URLError as error:
        raise Unreachable(f"{url}: {error.reason}") from error
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(data)
    log(f"downloaded {len(data):,} bytes to {os.path.relpath(path, ROOT)}")


def fetch(folder):
    """Both files, one request each, once a day (two requests a day in all; the catalog daily too, so new launches are
    named as soon as they're catalogued)."""
    download(GP_URL, os.path.join(folder, "active.csv"))
    download(SATCAT_URL, os.path.join(folder, "satcat.csv"))
    return folder


# ---------------------------------------------------------------- build

def read_csv(path):
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.reader(f)
        header = next(reader)
        return header, [row for row in reader if len(row) == len(header)]


def write_csv(path, header, rows):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f, lineterminator="\n")
        writer.writerow(header)
        writer.writerows(rows)


def build(src, out, day=None):
    """The published files: the elements split into everything-but-Starlink (what every phone reads) and Starlink (read
    only by phones showing it, two thirds of the objects), the catalog rows for those objects, and latest.json."""
    day = day or datetime.now(timezone.utc).date()
    header, rows = read_csv(os.path.join(src, "active.csv"))
    if "NORAD_CAT_ID" not in header or "EPOCH" not in header or len(rows) < MIN_OBJECTS:
        raise SystemExit(f"active.csv doesn't look like a GP set ({len(rows)} rows)")
    name_at, id_at, epoch_at = header.index("OBJECT_NAME"), header.index("NORAD_CAT_ID"), header.index("EPOCH")
    rows.sort(key=lambda r: int(r[id_at]) if r[id_at].isdigit() else 0)  # a stable order makes small git diffs
    starlink = [r for r in rows if r[name_at].startswith("STARLINK")]
    core = [r for r in rows if not r[name_at].startswith("STARLINK")]
    write_csv(os.path.join(out, "elements", "core.csv"), header, core)
    write_csv(os.path.join(out, "elements", "starlink.csv"), header, starlink)

    cat_header, cat_rows = read_csv(os.path.join(src, "satcat.csv"))
    missing = [c for c in CATALOG_COLUMNS if c not in cat_header]
    if missing:
        raise SystemExit(f"satcat.csv lacks {missing}")
    wanted = {r[id_at] for r in rows}
    cat_id = cat_header.index("NORAD_CAT_ID")
    picks = [cat_header.index(c) for c in CATALOG_COLUMNS]
    catalog = [[r[i] for i in picks] for r in cat_rows if r[cat_id] in wanted]
    catalog.sort(key=lambda r: int(r[0]) if r[0].isdigit() else 0)
    write_csv(os.path.join(out, "catalog.csv"), CATALOG_COLUMNS, catalog)

    epochs = sorted(r[epoch_at][:19] for r in rows)
    latest = {
        "date": day.isoformat(),
        "commit": None,
        "source": "celestrak",
        "objects": len(rows),
        "starlink": len(starlink),
        "medianEpoch": epochs[len(epochs) // 2] + "Z",
        "files": {"core": "elements/core.csv", "starlink": "elements/starlink.csv", "catalog": "catalog.csv"},
    }
    write_json(os.path.join(out, "latest.json"), latest)
    log(f"built {day}: {len(core):,} objects + {len(starlink):,} Starlink, {len(catalog):,} catalog rows")
    return latest


# ---------------------------------------------------------------- publish (the Pi, nightly)

def read_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def write_json(path, value):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(value, f, indent=1)
        f.write("\n")


def git(*args, check=True):
    return subprocess.run(["git", "-C", ROOT, *args], check=check, capture_output=True, text=True).stdout.strip()


def push(force=False):
    git("push", "-q", *(["--force-with-lease"] if force else []), "origin", "HEAD:main")
    # latest.json is the one file phones read at @main; everything else they read at the commit it names.
    try:
        purge = urllib.request.Request(f"https://purge.jsdelivr.net/gh/{REPO}@main/latest.json", headers=UA)
        urllib.request.urlopen(purge, timeout=30).read()
    except Exception as error:  # noqa: BLE001 — jsDelivr refreshes @main within 12 h by itself
        log(f"jsDelivr purge failed: {error}")


def staged_files():
    """Only the list itself may ever leave this machine: never keys, the work folder or code changed by hand."""
    staged = git("diff", "--cached", "--name-only").splitlines()
    stray = [p for p in staged if not p.startswith("elements/") and p not in PUBLISHED]
    if stray:
        raise SystemExit(f"refusing to publish files outside the satellite list: {', '.join(stray)}")
    return staged


def publish(day):
    """Two commits: the day's files, then latest.json naming that commit."""
    git("add", "-A", "elements", "catalog.csv")
    if staged_files():
        git("commit", "-q", "-m", f"Satellites for {day}")
    path = os.path.join(ROOT, "latest.json")
    latest = read_json(path)
    latest["commit"] = git("rev-parse", "HEAD")
    write_json(path, latest)
    git("add", "latest.json")
    staged_files()
    git("commit", "-q", "-m", f"latest.json: {day}")
    push()
    log(f"published {day} at {latest['commit'][:7]}")


def prune_history(keep_days=KEEP_DAYS, now=None):
    """Keeps the repo small: every day's files are ~3 MB of text, so history older than `keep_days` is folded into one
    starting commit and the recent commits are replayed on top (then force-pushed). Phones never hold a commit older than
    a day (`latest.json` is re-read every 6 h), and `nightly` publishes a fresh latest.json right after this, so nothing
    they read goes missing. Returns True when history was rewritten."""
    now = now or datetime.now(timezone.utc)
    cutoff = int((now - timedelta(days=keep_days)).timestamp())
    first = git("rev-list", "--max-parents=0", "HEAD").splitlines()[-1]
    first_time = int(git("show", "-s", "--format=%ct", first))
    if first_time >= cutoff - 7 * 86_400:  # weekly, not nightly: a rewrite is more work than a day of history
        return False
    # The newest commit at or before the cutoff becomes the new root, holding its own tree.
    base = git("rev-list", "-1", f"--before=@{cutoff}", "HEAD")
    if not base or base == git("rev-parse", "HEAD"):
        return False
    base_day = git("show", "-s", "--format=%cs", base)
    root = git("commit-tree", f"{base}^{{tree}}", "-m", f"Satellite list history before {base_day}, folded")
    git("rebase", "-q", "--onto", root, base, "HEAD")
    git("branch", "-f", "main", "HEAD")
    git("checkout", "-q", "main")
    git("reflog", "expire", "--expire=now", "--all")
    git("gc", "-q", "--prune=now")
    push(force=True)
    log(f"folded history before {base_day}; {git('rev-list', '--count', 'HEAD')} commits kept")
    return True


def nightly(day=None):
    """What the Pi runs: one fetch, one build, one publish. Safe to run again the same day (it does nothing), and a
    refusal from CelesTrak ends the run without a retry — the next day's run asks again."""
    day = day or datetime.now(timezone.utc).date()
    git("fetch", "-q", "origin")
    git("rebase", "-q", "--autostash", "origin/main")  # code updates pushed from elsewhere; next run uses them
    if int(git("rev-list", "--count", "origin/main..HEAD") or 0):
        push()  # last run committed but couldn't push
    latest = read_json(os.path.join(ROOT, "latest.json")) or {}
    if latest.get("date") == day.isoformat() and latest.get("commit"):
        log(f"{day} is already published")
        return
    src = os.path.join(WORK, day.isoformat())
    refused = os.path.join(src, "refused")
    if os.path.exists(refused):
        log(f"{day}: CelesTrak refused earlier today; not asking again until tomorrow")
        return
    try:
        fetch(src)
    except Refused as error:
        log(f"{error}; not asking again today")
        os.makedirs(src, exist_ok=True)
        open(refused, "w").close()
        return
    except Unreachable as error:
        log(f"{error}; the next run today will try again")
        return
    build(src, ROOT, day)
    prune_history()
    publish(day)
    for name in os.listdir(WORK):
        try:
            if date.fromisoformat(name) < day - timedelta(days=7):
                for f in os.listdir(os.path.join(WORK, name)):
                    os.remove(os.path.join(WORK, name, f))
                os.rmdir(os.path.join(WORK, name))
        except ValueError:
            continue


def main(argv):
    command = argv[1] if len(argv) > 1 else ""
    today = datetime.now(timezone.utc).date().isoformat()
    if command == "nightly":
        nightly(date.fromisoformat(argv[2]) if len(argv) > 2 else None)
    elif command == "fetch":
        fetch(argv[2] if len(argv) > 2 else os.path.join(WORK, today))
    elif command == "build":
        build(argv[2] if len(argv) > 2 else os.path.join(WORK, today), argv[3] if len(argv) > 3 else ROOT)
    elif command == "prune":
        prune_history()
    else:
        print(__doc__)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
