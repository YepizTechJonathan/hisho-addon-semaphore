# Semaphore (Ansible / Terraform) (Hisho add-on)

## What it does

Shows your Semaphore UI servers (Ansible and Terraform runner): each project's task templates and
how their last run went, with a button that runs a template (it always asks first). It also adds
the jobs listed in your notes to Semaphore and keeps them in step, so a new job is one entry in
`~/Hisho/System/Config/Addons/semaphore-templates.yaml`; it never runs or deletes them. Updates and
maintenance stay in your playbooks; Hisho is the place you see, add and start them.

## What it needs

- You need:
  - A Semaphore UI server (Ansible and Terraform runner) you can reach from the Core machine
  - A Semaphore API token
- Runs on: the Core machine, inside Hisho Core
- One entry per Semaphore server, each with:
  - `url` (str, required)
  - `token` (password, kept as a local secret, required)
  - `timezone` (str, optional)
  - `note` (str, optional)

## How to install

On the dashboard: **Add-ons > Store > Semaphore (Ansible / Terraform) > Install**. Or from a terminal:

```bash
hisho addon install semaphore
```

Hisho takes the newest version from this repository and turns it on inside Hisho Core. Updates show as **Update
available** on its Store card; **Roll back** returns to the version before.

## How to use

Its card shows failed or late jobs. Its page (**Add-ons > Semaphore**) lists each project's templates and their last run, with **run** (asks first). New jobs go in `~/Hisho/System/Config/Addons/semaphore-templates.yaml`; Hisho adds them to Semaphore and keeps them in step, never deleting one.

No Semaphore yet? Its page has **Install in Hisho**: Hisho runs Semaphore UI with PostgreSQL in Docker on the Core machine (Kihon, or Souko when Souko is the only machine), on your tailnet at port 4443, with schedules in your time zone, and fills in the address. Then give the entry its API token (the install made one: `bash ~/code/hisho/deploy/semaphore/install.sh --save-secrets` stores it). The button only shows when no Semaphore is running there.

## Settings

One entry per Semaphore server: its IP (http and port 3000 assumed, or a full URL) and an API token.
Make the token in Semaphore: your user menu > API Tokens. Entries are kept in
`~/Hisho/System/Config/Addons/semaphore.yaml`.

## Adding a job

```yaml
# ~/Hisho/System/Config/Addons/semaphore-templates.yaml
templates:
  - project: Internal Infrastructure            # Semaphore project name
    name: Forgejo Upgrade - Manual Guarded
    like: Pangolin Stack Update - Manual Guarded  # repository and environment copied from this job
    playbook: ansible/playbooks/forgejo-upgrade.yml
    inventory: Hisho dev
    description: Upgrades Forgejo; backup, checks, rollback.
    extra_vars: {confirm_forgejo_upgrade: true}   # pressing Run is the OK
    # schedule: "20 20 * * *"   # optional cron line, in the Semaphore's time zone
watch_units: [hisho-forgejo-wake, hisho-idle-sweep, hisho-ha-publish]
```

With `schedule:` Hisho adds the job's schedule and keeps its time in step; whether it is on stays
Semaphore's, so pausing it there is how you roll back.

Hisho adds it on its next refresh (about a minute). Editing the entry updates the job; removing the
entry leaves the job in Semaphore.

## Needs you

Each refresh (about a minute) checks every Semaphore entry:

- **Failed run**: a job whose last run failed is one item, with a link to the run's log. It clears on the
  next good run. The job's row on this page gets a **retry** button, except guarded jobs (name says
  Guarded, or it confirms itself with a `confirm_*` extra variable): those you open and run yourself.
- **Missed run**: an active schedule that should have started its job more than 15 minutes ago and
  didn't. The entry's `timezone` must match the Semaphore's own (`SEMAPHORE_SCHEDULE_TIMEZONE`).
- **Semaphore down**: no answer for 5 minutes is one item for that Semaphore.
- **Hisho's fast loops**: units listed under `watch_units:` in `semaphore-templates.yaml` (systemd user
  units on the Core machine) with no successful run for 15 minutes.

The phone gets only what you can act on: a nightly backup or secrets mirror that failed or missed its
run, and a job that runs more often than daily failing twice in a row. Everything else waits quietly.

## How to remove

**Add-ons > Store > Semaphore (Ansible / Terraform) > Remove**, or `hisho addon remove semaphore`. Its card and page go away; your settings stay in `~/Hisho/System/Config/Addons/semaphore.yaml`, so installing it again picks up where you left off.

## License

Source-available under the [PolyForm Shield License 1.0.0](LICENSE.md): free to use, change and share,
for any purpose except offering a product that competes with Hisho (reselling it, for example).
