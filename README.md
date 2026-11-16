# Overhead satellite list

The orbital elements and catalog the [Overhead](https://overhead.gumdrop.space) app uses for its satellite view,
fetched **once a day by one machine** and published here so every phone reads this copy instead of asking the source.
Phones compute every satellite's position themselves (SGP4); nothing here is a live feed.

The data is US Space Force (18th Space Defense Squadron) tracking data, published by [CelesTrak](https://celestrak.org).
CelesTrak's usage policy allows one download per update per address; this job asks once a day (the elements and the
catalog, one request each), with a User-Agent that names it, and stops without retrying on anything but a 200.

## Files

- **`latest.json`**: `{"date", "commit", "source", "objects", "starlink", "medianEpoch", "files"}`. Read it at `@main`,
  then read everything else at `@<commit>`, so all files come from the same day. `medianEpoch` is the typical element
  set's epoch (a few sets carry predicted epochs a day ahead).
- **`elements/core.csv`**: every active object except Starlink, in CelesTrak's CSV GP format (OBJECT_NAME, OBJECT_ID,
  EPOCH, MEAN_MOTION, ECCENTRICITY, INCLINATION, RA_OF_ASC_NODE, ARG_OF_PERICENTER, MEAN_ANOMALY, …, NORAD_CAT_ID, …,
  BSTAR, …). About 5,500 rows, 0.3 MB gzipped. What every phone reads.
- **`elements/starlink.csv`**: the Starlink constellation, same format. About 11,000 rows, 0.5 MB gzipped. Read only by
  phones that choose to show Starlink.
- **`catalog.csv`**: for every object above, `NORAD_CAT_ID, OBJECT_TYPE, OWNER, LAUNCH_DATE, LAUNCH_SITE, PERIOD,
  INCLINATION, APOGEE, PERIGEE, RCS` from CelesTrak's SATCAT. Sorted by catalog number so the daily diff is small.

Through jsDelivr:

```
https://cdn.jsdelivr.net/gh/GumDropSoftware/overhead-satellites@main/latest.json
https://cdn.jsdelivr.net/gh/GumDropSoftware/overhead-satellites@<commit>/elements/core.csv
```

## History

Each day's files are about 3 MB of text, so history older than 30 days is folded into one starting commit once a
week (`prune_history`) and the recent commits replayed on top. That keeps the clone and GitHub's copy small. Phones
re-read `latest.json` every 6 hours and cache each commit's files, so the rewrite never takes away a file they need.

## Running it

```
python3 -I overhead_satellites.py nightly
```

Standard library and git only. One machine runs it from a `systemd` timer (`deploy/`: 04:40 UTC, with a 10:40 run that
only retries a day that couldn't be fetched, never a day CelesTrak refused), and only that machine publishes.

It is three parts so the fetch-and-build can move elsewhere (a Vercel cron, a GitHub Actions schedule) without
changing what phones read: `fetch(dir)` downloads, `build(src, out)` writes the files with `commit: null`, and only
`publish(day)` touches this checkout's git. Elsewhere, replace `publish` with a commit through the GitHub API and keep
the two-commit shape: the files first, then `latest.json` naming that commit.
