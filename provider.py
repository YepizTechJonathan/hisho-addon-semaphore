"""Semaphore UI integration for Hisho (see addon.yaml). Runs inside Core."""
from __future__ import annotations

import importlib.util
import json
import os
import re
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

_spec = importlib.util.spec_from_file_location("hisho_addon_semaphore_jobs", Path(__file__).with_name("hisho_jobs.py"))
hisho_jobs = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(hisho_jobs)

BAD = {"error", "failed", "stopped"}
GRACE = 15 * 60        # a scheduled run that hasn't started 15 min after its time is a missed run
DOWN_AFTER = 5 * 60    # Semaphore not answering for 5 min is one Needs-you item
LOOP_STALE = 15 * 60   # a watched Hisho loop with no successful run for 15 min is one item
_now = time.time       # tests move the clock
_down_since: dict[str, float] = {}           # entry -> first time it didn't answer
_first_seen: dict[tuple[str, int], float] = {}  # (entry, schedule id) -> when this Core first saw it
_loop_ok: dict[str, float] = {}              # unit -> last time it was seen succeeding


def base_url(value: str) -> str:
    text = str(value or "").strip().rstrip("/")
    if not text:
        raise ValueError("no address: give Semaphore's IP or name")
    if "://" not in text:
        host, _, port = text.partition(":")
        text = f"http://{host}:{port or 3000}"
    return text


def call(entry: dict[str, Any], ctx: Any, method: str, path: str, body: Any = None) -> Any:
    req = urllib.request.Request(base_url(entry["url"]) + "/api" + path, method=method,
                                 data=None if body is None else json.dumps(body).encode(),
                                 headers={"Authorization": f"Bearer {ctx.secret(entry['token'])}",
                                          "Content-Type": "application/json", "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            raise PermissionError("Semaphore refused the API token")
        raise RuntimeError(f"Semaphore answered {exc.code} on {path}")
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise RuntimeError(f"can't reach Semaphore at {base_url(entry['url'])} ({getattr(exc, 'reason', exc)})")
    return json.loads(raw) if raw else None


def _projects(entry: dict[str, Any], ctx: Any) -> list[dict[str, Any]]:
    out = []
    for p in call(entry, ctx, "GET", "/projects") or []:
        templates = call(entry, ctx, "GET", f"/project/{p['id']}/templates") or []
        tasks = call(entry, ctx, "GET", f"/project/{p['id']}/tasks/last") or []
        runs: dict[int, list[dict[str, Any]]] = {}
        for t in sorted(tasks, key=lambda t: -int(t.get("id") or 0)):  # newest first
            runs.setdefault(t.get("template_id"), []).append(t)
        schedules = call(entry, ctx, "GET", f"/project/{p['id']}/schedules") or []
        out.append({**p, "templates": templates, "runs": runs, "schedules": schedules,
                    "last": {tid: r[0] for tid, r in runs.items()}})
    return out


# -- schedules: when a cron line should have fired ---------------------------------------------

def _cron_field(spec: str, lo: int, hi: int) -> set[int]:
    out: set[int] = set()
    for part in spec.split(","):
        rng, _, step = part.partition("/")
        if rng == "*":
            a, b = lo, hi
        elif "-" in rng:
            a, b = (int(x) for x in rng.split("-", 1))
        else:
            a = b = int(rng)
            if step:
                b = hi
        if not (lo <= a <= b <= hi) or (step and int(step) < 1):
            raise ValueError(f"bad cron field {spec!r}")
        out.update(range(a, b + 1, int(step or 1)))
    return out


def parse_cron(expr: str) -> tuple[set[int], set[int], set[int], set[int], set[int], bool, bool]:
    """Semaphore's cron lines: minute hour day-of-month month day-of-week (0 or 7 = Sunday)."""
    f = str(expr or "").split()
    if len(f) != 5:
        raise ValueError(f"cron line {expr!r} needs 5 fields")
    dow = {d % 7 for d in _cron_field(f[4], 0, 7)}
    return (_cron_field(f[0], 0, 59), _cron_field(f[1], 0, 23), _cron_field(f[2], 1, 31),
            _cron_field(f[3], 1, 12), dow, f[2] != "*", f[4] != "*")


def last_fire(expr: str, before: datetime) -> datetime | None:
    """The latest time at or before ``before`` (an aware datetime, in the schedule's time zone)
    that the cron line fires; None if it doesn't fire in the 8 days before."""
    mins, hours, doms, months, dows, dom_set, dow_set = parse_cron(expr)
    t = before.replace(second=0, microsecond=0)
    for _ in range(8 * 24 * 60):
        if t.month in months and t.hour in hours and t.minute in mins:
            dom_ok, dow_ok = t.day in doms, t.isoweekday() % 7 in dows
            if (dom_ok or dow_ok) if (dom_set and dow_set) else (dom_ok and dow_ok):
                return t
        t -= timedelta(minutes=1)
    return None


def once_a_day_or_less(expr: str) -> bool:
    mins, hours, *_ = parse_cron(expr)
    return len(mins) == 1 and len(hours) == 1


def _when(task: dict[str, Any], key: str = "created") -> float | None:
    raw = str(task.get(key) or "")
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def guarded(template: dict[str, Any]) -> bool:
    """A job that changes things only when you press it (its name says Guarded, or it confirms
    itself with an extra variable): never retried from Hisho."""
    return "guarded" in str(template.get("name", "")).lower() or "confirm_" in str(template.get("arguments") or "")


def pressing(template: dict[str, Any], schedule: dict[str, Any] | None, runs: list[dict[str, Any]]) -> bool:
    """Phone push, per Alex's rule: a nightly backup or secrets mirror, or a job that runs
    more often than daily failing twice in a row. Everything else waits quietly in Needs you."""
    if not schedule:
        return False
    cron = str(schedule.get("cron_format") or "")
    if once_a_day_or_less(cron):
        return bool(re.search(r"backup|mirror|secret", str(template.get("name", "")), re.I))
    done = [r for r in runs if r.get("status") not in ("waiting", "running", "starting")]
    return len(done) >= 2 and all(r.get("status") in BAD for r in done[:2])


def schedule_problems(name: str, entry: dict[str, Any], project: dict[str, Any]) -> list[dict[str, Any]]:
    """Failed and missed runs of one project's jobs, each one Needs-you item that clears on the next good run."""
    tz = ZoneInfo(str(entry.get("timezone") or "UTC"))
    by_template = {t.get("id"): t for t in project["templates"]}
    sched_of = {s.get("template_id"): s for s in project["schedules"] if s.get("active")}
    base = base_url(entry["url"])
    out = []
    for tid, tpl in by_template.items():
        runs = project["runs"].get(tid) or []
        last, sched = (runs[0] if runs else None), sched_of.get(tid)
        if last and last.get("status") in BAD:
            when = str(last.get("end") or last.get("created") or "")[:16].replace("T", " ")
            out.append({"entry": name, "severity": "warn", "key": f"failed-{project['id']}-{tid}",
                        "title": f"'{tpl.get('name')}' failed", "quiet": not pressing(tpl, sched, runs),
                        "url": f"{base}/project/{project['id']}/templates/{tid}?t={last.get('id')}",
                        "detail": f"Semaphore {name}, project {project.get('name')}, run {last.get('id')} at {when}"
                                  + ("" if guarded(tpl) else "; Retry is on the Semaphore page") + "."})
        if not sched:
            continue
        now = _now()
        seen = _first_seen.setdefault((name, int(sched.get("id") or 0)), now)
        try:
            due = last_fire(str(sched.get("cron_format")), datetime.fromtimestamp(now - GRACE, tz))
        except ValueError as exc:
            out.append({"entry": name, "severity": "warn", "key": f"cron-{project['id']}-{tid}", "quiet": True,
                        "title": f"Schedule of '{tpl.get('name')}' can't be read", "detail": str(exc)})
            continue
        if due is None or due.timestamp() < seen:
            continue  # this Core hasn't watched it long enough to say it was missed
        started = max((_when(r) or 0 for r in runs), default=0)
        if started < due.timestamp() - 60:
            out.append({"entry": name, "severity": "warn", "key": f"missed-{project['id']}-{tid}",
                        "title": f"'{tpl.get('name')}' missed its {due.strftime('%-I:%M %p').lower()} run",
                        "quiet": not pressing(tpl, sched, [{"status": "error"}] * 2),
                        "detail": f"Semaphore {name}: schedule '{sched.get('name')}' ({sched.get('cron_format')}, "
                                  f"{tz.key}) should have started it by {(due + timedelta(seconds=GRACE)).strftime('%H:%M')}."})
    return out


def loop_problems(units: list[str], ctx: Any) -> list[dict[str, Any]]:
    """Hisho's fast loops (systemd user timers on the Core machine): one item if a unit has had no
    successful run for 15 min. Their history stays in the journal, not in Semaphore."""
    out = []
    env = {"XDG_RUNTIME_DIR": os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"}
    now = _now()
    for unit in units:
        svc = unit if unit.endswith(".service") else f"{unit}.service"
        try:
            res = ctx.run(["systemctl", "--user", "show", svc, "--timestamp=unix",
                           "-p", "LoadState", "-p", "Result", "-p", "ExecMainExitTimestamp"], timeout=10, env=env)
            props = dict(line.split("=", 1) for line in res.stdout.splitlines() if "=" in line)
        except Exception as exc:  # noqa: BLE001
            props = {"error": str(exc)}
        exited = float(props.get("ExecMainExitTimestamp", "").lstrip("@") or 0)
        if props.get("LoadState") == "loaded" and props.get("Result") == "success" and exited:
            _loop_ok[svc] = max(_loop_ok.get(svc, 0), exited)
        last_ok = _loop_ok.setdefault(svc, now)  # never seen working: give it 15 min from now
        if now - last_ok >= LOOP_STALE:
            why = props.get("error") or (f"{svc} is not installed" if props.get("LoadState") != "loaded"
                                         else f"last result {props.get('Result')}")
            out.append({"entry": "loops", "severity": "warn", "key": f"loop-{svc}", "quiet": True,
                        "title": f"{svc.removesuffix('.service')} has not run successfully for {int((now - last_ok) // 60)} min",
                        "detail": f"{why}; see journalctl --user -u {svc} on the Core machine."})
    return out


def maintenance_vars() -> dict[str, Any]:
    """What playbooks should leave alone, from Hisho's maintenance state: every machine in
    maintenance (fleet machines and any other name), and whether the whole fleet is."""
    import os
    from pathlib import Path
    from hisho import maintenance
    root = Path(os.environ.get("HISHO_ROOT") or "~/Hisho").expanduser()
    return {"hisho_maintenance": sorted(maintenance.held(root)),
            "hisho_maintenance_all": bool(maintenance.status(root).get("fleet"))}


def sync_maintenance(entry: dict[str, Any], ctx: Any, wanted: dict[str, Any] | None = None) -> int:
    """Write ``hisho_maintenance`` / ``hisho_maintenance_all`` into every Semaphore environment's
    extra variables (the gate task in each playbook reads them). Returns how many changed."""
    wanted = maintenance_vars() if wanted is None else wanted
    changed = 0
    for p in call(entry, ctx, "GET", "/projects") or []:
        for env in call(entry, ctx, "GET", f"/project/{p['id']}/environment") or []:
            try:
                extra = json.loads(env.get("json") or "{}") or {}
            except ValueError:
                continue
            if all(extra.get(k) == v for k, v in wanted.items()):
                continue
            extra.update(wanted)
            body = {k: env[k] for k in ("id", "name", "project_id", "env", "password") if k in env}
            body["json"] = json.dumps(extra)
            call(entry, ctx, "PUT", f"/project/{p['id']}/environment/{env['id']}", body)
            changed += 1
    return changed


MANAGED = "Hisho {env}"   # inventories Hisho writes; the env is dev (home network) or prod (outside)


def sync_inventory(entry: dict[str, Any], ctx: Any, built: dict[str, Any] | None = None) -> int:
    """Keep every Hisho-managed inventory ("Hisho dev", "Hisho prod") in step with Hisho's machine
    list (hisho.inventory). Returns how many changed. Projects opt in with the page's
    "use Hisho's machine list" button, which creates them and points the project's jobs at them."""
    from hisho import inventory
    built = inventory.build() if built is None else built
    changed = 0
    for p in call(entry, ctx, "GET", "/projects") or []:
        for inv in call(entry, ctx, "GET", f"/project/{p['id']}/inventory") or []:
            env = next((e for e in built if inv.get("name") == MANAGED.format(env=e)), None)
            if env is None:
                continue
            text = inventory.render(built[env])
            if inv.get("inventory") == text:
                continue
            call(entry, ctx, "PUT", f"/project/{p['id']}/inventory/{inv['id']}", {**inv, "inventory": text})
            changed += 1
    return changed


def adopt_inventory(entry: dict[str, Any], ctx: Any, project_id: int) -> dict[str, Any]:
    """Create "Hisho dev"/"Hisho prod" in a project (same SSH keys as its current inventories) and
    point its jobs at them: jobs on an inventory whose name has "prod" go to Hisho prod, the rest
    to Hisho dev. The old inventories are kept, so this can be undone in Semaphore."""
    from hisho import inventory
    built = inventory.build()
    invs = call(entry, ctx, "GET", f"/project/{project_id}/inventory") or []
    old = [i for i in invs if not str(i.get("name", "")).startswith("Hisho ")]
    if not old:
        raise RuntimeError("this project has no inventory to copy SSH keys from")
    ids: dict[str, int] = {}
    for env in built:
        name = MANAGED.format(env=env)
        cur = next((i for i in invs if i.get("name") == name), None)
        like = next((i for i in old if env in str(i.get("name", "")).lower()), old[0])
        body = {"name": name, "project_id": project_id, "type": "static-yaml",
                "inventory": inventory.render(built[env]),
                **{k: like[k] for k in ("ssh_key_id", "become_key_id") if like.get(k)}}
        if cur:
            call(entry, ctx, "PUT", f"/project/{project_id}/inventory/{cur['id']}", {**cur, **body})
            ids[env] = cur["id"]
        else:
            ids[env] = call(entry, ctx, "POST", f"/project/{project_id}/inventory", body)["id"]
    names = {i["id"]: str(i.get("name", "")).lower() for i in old}
    moved = 0
    for t in call(entry, ctx, "GET", f"/project/{project_id}/templates") or []:
        if t.get("inventory_id") not in names:
            continue
        env = "prod" if "prod" in names[t["inventory_id"]] and "prod" in ids else "dev"
        call(entry, ctx, "PUT", f"/project/{project_id}/templates/{t['id']}", {**t, "inventory_id": ids[env]})
        moved += 1
    return {"ok": True, "message": f"{moved} jobs now use Hisho's machine list ({', '.join(MANAGED.format(env=e) for e in ids)})"}


TEMPLATES_FILE = "semaphore-templates"   # ~/Hisho/System/Config/Addons/semaphore-templates.yaml
COPIED = ("repository_id", "environment_id", "app", "type", "view_id", "git_branch")
OWNED = ("playbook", "inventory_id", "description", "arguments")


def wanted_templates() -> list[dict[str, Any]]:
    """Job templates the owner wants in Semaphore, kept in the vault::

        templates:
          - project: Internal Infrastructure      # project name (or id)
            name: Forgejo Upgrade - Manual Guarded
            like: Pangolin Stack Update - Manual Guarded   # repository, environment, app copied from it
            playbook: ansible/playbooks/forgejo-upgrade.yml
            inventory: Hisho dev
            description: What it does
            extra_vars: {confirm_forgejo_upgrade: true}    # passed as --extra-vars; pressing Run is the OK
            entry: Terraform                       # optional: only this Semaphore entry
    """
    from hisho import addons
    data = addons.read_config(TEMPLATES_FILE)
    return [t for t in (data.get("templates") or []) if isinstance(t, dict) and t.get("name")]


def _template_body(spec: dict[str, Any], like: dict[str, Any], inventory_id: int, project_id: int) -> dict[str, Any]:
    extra = spec.get("extra_vars") or {}
    return {**{k: like[k] for k in COPIED if k in like}, "project_id": project_id, "name": str(spec["name"]),
            "playbook": str(spec["playbook"]), "inventory_id": inventory_id,
            "description": str(spec.get("description") or "Added by Hisho."),
            "arguments": json.dumps(["--extra-vars", json.dumps(extra)]) if extra else "[]",
            "survey_vars": [], "autorun": False}


def sync_templates(entry_name: str, entry: dict[str, Any], ctx: Any,
                   wanted: list[dict[str, Any]] | None = None) -> list[str]:
    """Create, or bring back in step, the job templates listed in the vault. Never runs a job and
    never deletes a template; a template with the same name is changed only in the fields the
    list sets. Returns what changed, one line each."""
    wanted = wanted_templates() if wanted is None else wanted
    wanted = [t for t in wanted if not t.get("entry") or str(t["entry"]) == entry_name]
    if not wanted:
        return []
    projects = call(entry, ctx, "GET", "/projects") or []
    done = []
    for spec in wanted:
        for key in ("project", "playbook", "inventory", "like"):
            if not spec.get(key):
                raise ValueError(f"job '{spec['name']}' has no {key}")
        project = next((p for p in projects if str(spec["project"]) in (str(p.get("name")), str(p.get("id")))), None)
        if project is None:
            raise RuntimeError(f"job '{spec['name']}': no project '{spec['project']}'")
        pid = project["id"]
        templates = call(entry, ctx, "GET", f"/project/{pid}/templates") or []
        like = next((t for t in templates if t.get("name") == spec["like"]), None)
        if like is None:
            raise RuntimeError(f"job '{spec['name']}': no job '{spec['like']}' to copy settings from")
        inv = next((i for i in call(entry, ctx, "GET", f"/project/{pid}/inventory") or []
                    if i.get("name") == spec["inventory"]), None)
        if inv is None:
            raise RuntimeError(f"job '{spec['name']}': no machine list '{spec['inventory']}'")
        if spec.get("schedule"):
            parse_cron(str(spec["schedule"]))  # a bad line is a problem on the card, not a broken job
        body = _template_body(spec, like, inv["id"], pid)
        cur = next((t for t in templates if t.get("name") == spec["name"]), None)
        if cur is None:
            cur = call(entry, ctx, "POST", f"/project/{pid}/templates", body) or next(
                (t for t in call(entry, ctx, "GET", f"/project/{pid}/templates") or [] if t.get("name") == spec["name"]), None)
            done.append(f"added '{spec['name']}' to {project.get('name')}")
        elif any(cur.get(k) != body[k] for k in OWNED):
            call(entry, ctx, "PUT", f"/project/{pid}/templates/{cur['id']}", {**cur, **{k: body[k] for k in OWNED}})
            done.append(f"updated '{spec['name']}' in {project.get('name')}")
        if spec.get("schedule") and cur and cur.get("id"):
            done += _sync_schedule(entry, ctx, pid, cur["id"], spec)
    return done


def _sync_schedule(entry: dict[str, Any], ctx: Any, pid: int, tid: int, spec: dict[str, Any]) -> list[str]:
    """One schedule per listed job with ``schedule:`` (a cron line in the Semaphore's own time zone).
    Hisho sets the time; whether it is on stays Semaphore's (pausing it there is the rollback)."""
    cron = str(spec["schedule"]).strip()
    cur = next((s for s in call(entry, ctx, "GET", f"/project/{pid}/schedules") or [] if s.get("template_id") == tid), None)
    if cur is None:
        call(entry, ctx, "POST", f"/project/{pid}/schedules", {"project_id": pid, "template_id": tid,
                                                                "name": str(spec["name"]), "cron_format": cron, "active": True})
        return [f"scheduled '{spec['name']}' at {cron}"]
    if cur.get("cron_format") != cron:
        call(entry, ctx, "PUT", f"/project/{pid}/schedules/{cur['id']}", {**cur, "cron_format": cron})
        return [f"moved '{spec['name']}' to {cron}"]
    return []


def summary(entries: dict[str, dict[str, Any]], ctx: Any) -> dict[str, Any]:
    problems, n_templates, n_failed = [], 0, 0
    for name, entry in entries.items():
        try:
            call(entry, ctx, "GET", "/projects")
            _down_since.pop(name, None)
        except Exception as exc:  # noqa: BLE001
            since = _down_since.setdefault(name, _now())
            if _now() - since >= DOWN_AFTER:
                problems.append({"entry": name, "severity": "warn", "key": "down", "quiet": True,
                                 "title": f"Semaphore {name} is not answering",
                                 "detail": f"for {int((_now() - since) // 60)} min: {str(exc)[:160]}. Its jobs aren't running or being watched."})
            continue
        for title, sync in ((f"Can't keep Hisho's own jobs in Semaphore {name}", lambda: auto_reconcile(name, entry, ctx)),
                            (f"Can't add Hisho's jobs to Semaphore {name}", lambda: sync_templates(name, entry, ctx)),
                            (f"Can't update Semaphore {name}'s machine list", lambda: sync_inventory(entry, ctx)),
                            (f"Can't tell Semaphore {name} about maintenance", lambda: sync_maintenance(entry, ctx))):
            try:
                sync()
            except Exception as exc:  # noqa: BLE001
                problems.append({"entry": name, "severity": "warn", "title": title, "detail": str(exc)[:200]})
        try:
            projects = _projects(entry, ctx)
        except Exception as exc:  # noqa: BLE001
            problems.append({"entry": name, "severity": "warn", "title": f"Can't read Semaphore {name}'s jobs",
                             "detail": str(exc)[:200]})
            continue
        for p in projects:
            n_templates += len(p["templates"])
            n_failed += sum(1 for t in p["templates"] if (p["last"].get(t.get("id")) or {}).get("status") in BAD)
            problems += schedule_problems(name, entry, p)
    units = [str(u) for u in (_watch_config().get("watch_units") or [])]
    problems += loop_problems(units, ctx) if units else []
    return {"state": "warn" if problems else "ok",
            "headline": f"{n_templates} jobs · {n_failed} failed last time", "problems": problems}


def _watch_config() -> dict[str, Any]:
    from hisho import addons
    return addons.read_config(TEMPLATES_FILE) or {}


def details(entries: dict[str, dict[str, Any]], ctx: Any) -> list[dict[str, Any]]:
    out = []
    for name, entry in entries.items():
        try:
            projects = _projects(entry, ctx)
        except Exception as exc:  # noqa: BLE001
            out.append({"title": name, "entry": name, "state": "down", "facts": [str(exc)[:200]], "items": []})
            continue
        out.append(hisho_jobs_card(name, entry, ctx))
        for p in projects:
            items = []
            for tpl in sorted(p["templates"], key=lambda t: str(t.get("name"))):
                t = p["last"].get(tpl["id"])
                status = (t or {}).get("status") or "never run"
                state = "down" if status in BAD else "warn" if status in ("waiting", "running", "starting") \
                    else "up" if status == "success" else "off"
                items.append({"id": f"{p['id']}-{tpl['id']}", "name": str(tpl.get("name")), "state": state,
                              "facts": [f"last run: {status}" + (f", {str(t.get('end') or t.get('created'))[:16]}" if t else ""),
                                        str(tpl.get("playbook") or tpl.get("app") or "")]
                                       + ([str(tpl["description"])] if tpl.get("description") else []),
                              "actions": ([{"id": "retry", "label": "retry",
                                            "confirm": f"Run '{tpl.get('name')}' again now?"}]
                                          if status in BAD and not guarded(tpl) else [])
                                         + [{"id": "run", "label": "run",
                                             "confirm": f"Run '{tpl.get('name')}' in Semaphore now?"}]})
            invs = call(entry, ctx, "GET", f"/project/{p['id']}/inventory") or []
            managed = [i for i in invs if str(i.get("name", "")).startswith("Hisho ")]
            items.insert(0, {"id": f"{p['id']}-inventory", "name": "Machine list: " + ("Hisho's (kept in step)" if managed else "Semaphore's own"),
                             "state": "up" if managed else "off",
                             "facts": ["Hisho writes " + ", ".join(i["name"] for i in managed) if managed else
                                       "Jobs use inventory files from the repo; machines you add to Hisho don't reach them"],
                             "actions": [] if managed else [{"id": "use-hisho-inventory", "label": "use Hisho's machine list",
                                                             "confirm": f"Create Hisho dev / Hisho prod in '{p.get('name')}' and point its jobs at them? The old inventories stay, so you can switch back in Semaphore."}]})
            out.append({"title": f"{name} · {p.get('name')}", "entry": name, "state": "ok",
                        "facts": [base_url(entry["url"]), f"{len(items) - 1} jobs"], "items": items})
    return out


def test(entry: dict[str, Any], ctx: Any) -> dict[str, Any]:
    try:
        projects = call(entry, ctx, "GET", "/projects") or []
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)[:200]}
    return {"ok": True, "message": f"Connected: {len(projects)} project(s): "
                                   + ", ".join(str(p.get('name')) for p in projects[:6])}


def act(entries: dict[str, dict[str, Any]], ctx: Any, entry: str, item: str, action: str) -> dict[str, Any]:
    if item == "hisho-jobs" and action == "add-hisho-jobs":
        res = reconcile(entry, entries[entry], ctx, first=True)
        n, last = len(res["state"].get("jobs") or {}), res["state"].get("last") or {}
        return {"ok": not res["problems"] or bool(res["state"].get("project_id")),
                "message": f"{n} Hisho jobs set up in Semaphore {entry}"
                           + (f"; {len(last.get('edited') or [])} edited ones left alone" if last.get("edited") else "")
                           + (f"; {len(last.get('skipped') or [])} skipped" if last.get("skipped") else "")
                           + (". " + "; ".join(res["problems"][:3]) if res["problems"] else "."),
                **({"error": "; ".join(res["problems"][:3])} if res["problems"] and not res["state"].get("project_id") else {})}
    if action == "use-hisho-inventory" and item.endswith("-inventory") and item.split("-")[0].isdigit():
        return adopt_inventory(entries[entry], ctx, int(item.split("-")[0]))
    if action not in ("run", "retry"):
        return {"ok": False, "error": f"unknown action {action}"}
    project, _, template = item.partition("-")
    if not (project.isdigit() and template.isdigit()):
        return {"ok": False, "error": "bad job id"}
    if action == "retry":
        tpl = call(entries[entry], ctx, "GET", f"/project/{project}/templates/{template}") or {}
        if guarded(tpl):
            return {"ok": False, "error": f"'{tpl.get('name')}' is a guarded job: open it and press run yourself"}
    task = call(entries[entry], ctx, "POST", f"/project/{project}/tasks", {"template_id": int(template)})
    return {"ok": True, "message": f"started Semaphore task {(task or {}).get('id')}; its result shows here when done"}


# ── Hisho's own jobs (hisho_jobs.py): a "Hisho" project Hisho makes and keeps in step ──────────────────

def installed_by_hisho(entry_name: str, entry: dict[str, Any]) -> dict[str, Any]:
    """Install in Hisho's record when this entry is the Semaphore Hisho installed (its ``main`` entry), else {}."""
    from hisho import addons
    rec = dict(((addons._fleet_data().get("addons") or {}).get("companions") or {}).get("semaphore") or {})
    if rec.get("state") != "installed" or entry_name != "main":
        return {}
    url = str((rec.get("entry") or {}).get("url") or "")
    return rec if not url or url == str(entry.get("url") or "") else {}


def _entries_of(name: str) -> dict[str, Any] | None:
    """Another add-on's entries, straight from its settings file; None when the file is there but unreadable
    (then nothing is removed on its account)."""
    from hisho import addons, fleet_config
    path = addons.config_path(name)
    if not path.exists():
        return {}
    try:
        data = fleet_config.load_yaml(path) or {}
    except Exception:  # noqa: BLE001
        return None
    return {str(k): dict(v or {}) for k, v in (data.get("entries") or {}).items()} if isinstance(data, dict) else None


def _server_ssh():
    from hisho import addons
    try:
        mod = addons.provider(addons.get("server"))
    except Exception:  # noqa: BLE001 -- no Servers add-on here
        return None
    return getattr(mod, "_ssh_args", None)


def reconcile(entry_name: str, entry: dict[str, Any], ctx: Any, *, first: bool = False,
              key_only: list[str] | None = None) -> dict[str, Any]:
    """Make, or bring back in step, Hisho's project in this Semaphore. ``first``: the owner's OK (Install in Hisho, or
    the card's button): keys go on every machine now. Returns {"state", "problems"}."""
    from hisho import addons, fleet_config
    state = hisho_jobs.load_state(entry_name)
    before = json.dumps(state, sort_keys=True, default=str)
    problems: list[str] = []
    api = lambda method, path, body=None: call(entry, ctx, method, path, body)  # noqa: E731
    fleet, data = ctx.fleet, addons._fleet_data()
    if fleet.source == "defaults" and fleet_config.fleet_path().exists():
        return {"state": state, "problems": ["Fleet.yaml can't be read; Hisho's jobs left as they are"]}
    servers = _entries_of("server") if "server" in addons.enabled() else {}
    proxmox = _entries_of("proxmox")
    readable = servers is not None and proxmox is not None
    servers, proxmox = servers or {}, proxmox or {}
    rec = installed_by_hisho(entry_name, entry)
    owner_tz = str(fleet.timezone or entry.get("timezone") or "UTC")
    sem_tz = str(entry.get("timezone") or (owner_tz if rec else "UTC"))
    if rec and not entry.get("timezone") and fleet.timezone:   # Hisho's install runs schedules in the owner's zone
        try:
            addons.set_entry(addons.get("semaphore"), entry_name, {"timezone": owner_tz}, merge=True)
        except Exception:  # noqa: BLE001
            pass
    if rec:
        dest = Path(str(rec.get("dir") or "~/semaphore")).expanduser() / "hisho-ansible"
        if dest.is_dir() and addons.is_local(str(rec.get("host") or fleet.core_host)):
            hisho_jobs.refresh_playbooks(Path(__file__).with_name("ansible"), dest)
    try:
        done = hisho_jobs.ensure_base(api, ctx, state, str(entry.get("playbooks") or hisho_jobs.PLAYBOOKS),
                                      f"semaphore-hisho@{fleet.core_host}")
        text = hisho_jobs.render_inventory(hisho_jobs.machines(fleet, data, servers, proxmox))
        done += hisho_jobs.sync_inventory(api, state, text)
        feat = hisho_jobs.features(data, addons.enabled(), proxmox)
        wanted = hisho_jobs.jobs(feat, str(state.get("public_key") or ""))
        crons = {j["key"]: hisho_jobs.cron_for(j["when"], owner_tz, sem_tz) for j in wanted if j["when"]}
        res = hisho_jobs.sync_jobs(api, state, wanted, crons, may_remove=readable)
    except Exception as exc:  # noqa: BLE001
        problems.append(str(exc)[:200])
        res, done = {"done": [], "edited": [], "skipped": []}, []
    if state.get("public_key"):
        targets = hisho_jobs.key_targets(fleet, servers, _server_ssh(), addons.is_local)
        problems_keys = hisho_jobs.place_key(ctx, state["public_key"], targets, state, only=key_only,
                                             now=_now(), force=first or bool(key_only))
    else:
        problems_keys = []
    state["last"] = {"done": done + res["done"], "edited": res["edited"], "skipped": res["skipped"],
                     "keys": problems_keys, "problems": problems}
    if json.dumps(state, sort_keys=True, default=str) != before:
        hisho_jobs.save_state(entry_name, state)
    return {"state": state, "problems": problems}


def auto_reconcile(entry_name: str, entry: dict[str, Any], ctx: Any) -> list[str]:
    """Every sync: keep Hisho's project in step where it was set up (Install in Hisho, or the card's button). A
    Semaphore that was only connected gets nothing until the button is pressed."""
    state = hisho_jobs.load_state(entry_name)
    first = not state.get("project_id")
    if first and not installed_by_hisho(entry_name, entry):
        return []
    res = reconcile(entry_name, entry, ctx, first=first)
    if res["problems"]:
        raise RuntimeError("; ".join(res["problems"]))
    return (res["state"].get("last") or {}).get("done") or []


def adopted(entries: dict[str, dict[str, Any]], ctx: Any, machine: str) -> dict[str, Any]:
    """`hisho adopt` / `hisho create` / Add a machine: the new machine gets Semaphore's key (one line added to its
    authorized_keys, nothing removed) and joins Hisho's inventory. When this machine can't reach Semaphore's token
    (adopting from the workstation), Core's next sync adds it to the inventory within a minute."""
    errors = []
    for name, entry in entries.items():
        state = hisho_jobs.load_state(name)
        if not state.get("project_id"):
            continue
        try:
            res = reconcile(name, entry, ctx, key_only=[f"machine:{machine}"])
            keys = (res["state"].get("last") or {}).get("keys") or []
            if f"machine:{machine}" not in (res["state"].get("keyed") or []):
                errors += keys or [f"Semaphore's key not on {machine}"]
        except Exception as exc:  # noqa: BLE001
            errors.append(f"Semaphore {name}: {str(exc)[:160]}")
    return {"ok": not errors, **({"error": "; ".join(errors)[:300]} if errors else {})}


def fleet_changed(entries: dict[str, dict[str, Any]], ctx: Any, event: str, info: dict[str, Any]) -> dict[str, Any]:
    """Core tells integrations when the machine list changes: ``machine-removed`` (hisho retire), ``entry-set`` /
    ``entry-removed`` (an add-on entry; Servers and Proxmox ones matter here) and ``companion-installed`` (Install in
    Hisho: Semaphore's own, which sets everything up at once)."""
    addon = str(info.get("addon") or "")
    if event == "companion-installed" and addon != "semaphore":
        return {"ok": True}
    if event in ("entry-set", "entry-removed") and addon not in ("server", "proxmox"):
        return {"ok": True}
    out = []
    for name, entry in entries.items():
        if event == "companion-installed":
            if not installed_by_hisho(name, entry):
                continue
        elif not hisho_jobs.load_state(name).get("project_id"):
            continue
        only = [f"server:{info.get('entry')}"] if event == "entry-set" and addon == "server" else None
        res = reconcile(name, entry, ctx, first=event == "companion-installed", key_only=only)
        out += res["problems"]
    return {"ok": not out, **({"error": "; ".join(out)[:300]} if out else {})}


def hisho_jobs_card(name: str, entry: dict[str, Any], ctx: Any) -> dict[str, Any]:
    state = hisho_jobs.load_state(name)
    last = state.get("last") or {}
    if not state.get("project_id"):
        auto = bool(installed_by_hisho(name, entry))
        return {"title": f"{name} · Hisho's jobs", "entry": name, "state": "off",
                "facts": ["Hisho sets these up by itself on its next check" if auto else
                          "Not set up: Hisho's own jobs (self-update, code sync, reports, and Forgejo / Proxmox ones "
                          "when those are on) can go in a Semaphore project of their own"],
                "items": [] if auto else [{
                    "id": "hisho-jobs", "name": "Hisho's jobs: not set up", "state": "off",
                    "facts": [f"Playbooks are read from {entry.get('playbooks') or hisho_jobs.PLAYBOOKS} inside "
                              "Semaphore's container (Hisho's own install mounts them there)"],
                    "actions": [{"id": "add-hisho-jobs", "label": "Add Hisho's jobs",
                                 "confirm": f"Make a '{hisho_jobs.PROJECT}' project in Semaphore {name} with Hisho's "
                                            "jobs, an inventory of Hisho's machines and an SSH key for Semaphore, and "
                                            "put that key on your machines (one line added, nothing removed)? Nothing "
                                            "runs now, and jobs you already have are not changed."}]}]}
    n = len(state.get("jobs") or {})
    facts = [f"{n} Hisho jobs set up", f"Semaphore's key on {len(state.get('keyed') or [])} machines"]
    facts += [f"Edited, left alone: {', '.join(last['edited'])}"] if last.get("edited") else []
    facts += [f"Skipped: {'; '.join(last['skipped'])}"] if last.get("skipped") else []
    facts += last.get("keys") or []
    return {"title": f"{name} · Hisho's jobs", "entry": name,
            "state": "warn" if last.get("problems") or last.get("keys") else "ok", "facts": facts[:8],
            "items": [{"id": "hisho-jobs", "name": f"{n} Hisho jobs set up", "state": "up",
                       "facts": [f"project {hisho_jobs.PROJECT}, inventory {hisho_jobs.INVENTORY} (kept in step with "
                                 "Hisho's machine list)"],
                       "actions": [{"id": "add-hisho-jobs", "label": "check again",
                                    "confirm": "Add any of Hisho's jobs that are missing and put Semaphore's key on "
                                               "machines that don't have it yet? Edited jobs stay as they are."}]}]}
