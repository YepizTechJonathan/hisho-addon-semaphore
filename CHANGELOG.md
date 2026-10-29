# Changelog

## 0.3.3

- Every field on its setup form says in plain words what it is, and links to where to get it (addon.yaml `help:`, shown by Hisho's dashboard).

## 0.3.2

- Install in Hisho: with no Semaphore yet, its page installs one on the Core machine (Hisho's own install: Docker, PostgreSQL, tailnet port 4443, your time zone) and fills in the address; never over an existing Semaphore.

## 0.3.1

- README says what you need (addon.yaml you_need) and how to use it; the README check requires both.
- Neutral sample names in tests and comments.

## 0.3.0

- Needs you: a failed run (with a link to its log, cleared by the next good run), a missed scheduled run (15 min grace, in the entry's `timezone`), a Semaphore that hasn't answered for 5 min, and Hisho's fast loops listed in `watch_units:` with no successful run for 15 min.
- Phone push only for a nightly backup or secrets mirror that failed or missed, or a more-than-daily job failing twice in a row.
- **retry** on a failed job's row, never for guarded jobs.
- `schedule:` on a job in `semaphore-templates.yaml` adds its schedule and keeps the time in step (never turns a paused one back on).

## 0.2.0

- Adds the jobs listed in `~/Hisho/System/Config/Addons/semaphore-templates.yaml` to Semaphore and keeps them in step (copies repository/environment from a job you name, sets playbook, machine list, description and extra vars). Never runs or deletes a job.

## 0.1.0

- Moved out of Hisho into its own repository (hisho-addon-semaphore); installed from the add-on store.

