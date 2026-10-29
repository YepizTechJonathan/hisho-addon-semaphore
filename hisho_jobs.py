"""Hisho's own jobs in Semaphore: one "Hisho" project that Hisho makes and keeps in step (loaded by provider.py).

What it makes, once, in the project "Hisho":

* a key store entry with an SSH key Hisho generates for Semaphore (the private half goes only into Semaphore's key
  store; Hisho keeps the public half in ``Addons/semaphore.yaml`` so it can put it on machines), and a "none" key
  for the playbook repository;
* a repository "Hisho playbooks" at a local path inside Semaphore's container (``/hisho/ansible``: Hisho's own
  install mounts this add-on's ``ansible/`` folder there read-only; no GitHub needed);
* an environment "Hisho" (the maintenance gate's variables are kept in it by the provider);
* an inventory "Hisho machines" generated from Hisho's machine list (Fleet.yaml machines and the Core machine,
  Servers entries, Proxmox entries), rewritten on every sync; only the inventory Hisho made is ever touched;
* the job list in ``jobs()`` (always-on ones, plus Forgejo / secrets / Proxmox ones when those are on).

Rules: it adds what is missing and never makes a second copy (a job with the same name, or another project's job
running the same Hisho unit, is left alone and reported). It remembers what it set (a hash of each job as Semaphore
stored it, and each schedule's time) in ``Addons/semaphore.yaml`` ``hisho_setup:``; a job or schedule someone
changed since is never overwritten, only reported. It never runs a job. The one thing it deletes: a per-machine job
it made (``Proxmox Update - <server> - Manual Guarded``) when that server is removed from Hisho, and only if nobody
edited it (an edited one is kept and reported).
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import shutil
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

import yaml

PROJECT = "Hisho"
INVENTORY = "Hisho machines"
REPOSITORY = "Hisho playbooks"
ENVIRONMENT = "Hisho"
KEY_NAME = "Hisho: Semaphore's SSH key"
NONE_KEY = "Hisho: no key (local playbooks)"
PLAYBOOKS = "/hisho/ansible"           # inside Semaphore's container (Hisho's install mounts ansible/ there)
CONFIG = "semaphore"                   # state lives in Addons/semaphore.yaml under hisho_setup:
PER_MACHINE = "proxmox-host:"          # job keys that belong to one server and go when it goes
HASHED = ("name", "playbook", "inventory_id", "repository_id", "environment_id", "arguments", "description", "app")
UNIT = "playbooks/hisho-unit.yml"
KEY_RETRY = 30 * 60                    # a machine that couldn't take the key is tried again after this

Api = Callable[..., Any]               # api(method, path, body=None)
_key_tried: dict[str, float] = {}


# ── what Hisho wants ───────────────────────────────────────────────────────────────────────────────

def _job(key: str, name: str, playbook: str, extra: dict[str, Any] | None = None,
         when: tuple[int, int | None, int | None] | None = None, about: str = "", needs: str = "") -> dict[str, Any]:
    return {"key": key, "name": name, "playbook": playbook, "extra": dict(extra or {}), "when": when,
            "about": about, "needs": needs}


def _unit(key: str, name: str, unit: str, when=None, about: str = "", needs: str = "") -> dict[str, Any]:
    return _job(key, name, UNIT, {"hisho_unit": unit}, when, about, needs)


def _name(text: Any) -> str:
    return re.sub(r"[^a-z0-9-]+", "-", str(text).lower()).strip("-")


def features(fleet_data: dict[str, Any], enabled: dict[str, Any], proxmox_entries: dict[str, Any]) -> dict[str, Any]:
    """Which conditional jobs apply: Forgejo on, a second secrets backend, Proxmox entries."""
    backends = list(((fleet_data.get("secrets") or {}).get("backends")) or [])
    return {"forgejo": "forgejo" in enabled, "secrets_mirror": len(backends) >= 2,
            "proxmox": sorted(_name(e) for e, v in (proxmox_entries or {}).items() if (v or {}).get("url"))
            if "proxmox" in enabled else []}


def jobs(feat: dict[str, Any], public_key: str) -> list[dict[str, Any]]:
    """Hisho's job list. ``when`` is (minute, hour or None for hourly, weekday 0=Sunday or None) in the owner's
    time zone; None = a button only."""
    key_vars = {"hisho_semaphore_key": public_key}
    out = [
        _unit("self-update", "Hisho Core Self-Update", "hisho-self-update", (40, None, None),
              "The Core machine updates its Hisho code (hisho self-update), hourly at :40."),
        _unit("code-sync", "Hisho Code Sync", "hisho-code-sync", (20, None, None),
              "Clones missing project code and fast-forwards clean checkouts (hisho project sync), hourly at :20."),
        _unit("security-review", "Weekly Security Review", "hisho-security-review", (30, 6, 1),
              "Read-only security checks on every machine (hisho security review), Mondays 6:30 AM."),
        _job("patch-dry-run", "OS Patch Dry Run", "playbooks/patch-dry-run.yml", None, (0, 5, 6),
             "What an upgrade would install on each machine; installs nothing. Saturdays 5:00 AM."),
        _job("pending-updates", "Pending Updates Report", "playbooks/pending-updates-report.yml", None, (0, 6, None),
             "How many updates wait on each machine. Daily 6:00 AM."),
        _job("failed-services", "Failed Services Report", "playbooks/failed-services-report.yml", None, (5, 6, None),
             "Failed systemd units on each machine. Daily 6:05 AM."),
        _job("disk-usage", "Disk Usage Report", "playbooks/disk-usage-report.yml", None, (10, 6, None),
             "How full each machine's disks are. Daily 6:10 AM."),
        _job("authorized-keys", "Authorized Keys - Manual Guarded", "playbooks/authorized-keys.yml",
             {"confirm_authorized_keys": True, **key_vars}, None,
             "Adds Semaphore's key to every machine's authorized_keys (Proxmox has its own button). Removes nothing."),
    ]
    if feat.get("forgejo"):
        out += [
            _unit("forgejo-backup", "Hisho Forgejo Backup", "hisho-forgejo-backup", (20, 20, None),
                  "Nightly Forgejo + Semaphore database dumps, 8:20 PM.", "forgejo"),
            _unit("forgejo-copy", "Hisho Forgejo Copy", "hisho-forgejo-copy", (2, None, None),
                  "Copies Forgejo backups to the workstation, hourly at :02.", "forgejo"),
            _unit("forgejo-mirrors", "Hisho Forgejo Mirrors", "hisho-forgejo-mirrors", (10, 21, None),
                  "Mirrors new add-on repositories into Forgejo, nightly 9:10 PM.", "forgejo"),
            _unit("forgejo-upgrade", "Forgejo Upgrade - Manual Guarded", "hisho-forgejo-upgrade", None,
                  "Forgejo to Hisho's pinned versions now (backup, checks, rollback).", "forgejo"),
            _unit("addon-mirror", "Hisho Add-on Mirror", "hisho-addon-mirror", (6, None, None),
                  "Refreshes the add-on mirrors (hisho addon mirror), hourly at :06.", "forgejo"),
        ]
    if feat.get("secrets_mirror"):
        out.append(_unit("secrets-mirror", "Hisho Secrets Mirror", "hisho-secrets-mirror", (40, 20, None),
                         "Copies new or changed secrets to the second secrets store (never deletes), 8:40 PM.",
                         "secrets"))
    hosts = list(feat.get("proxmox") or [])
    if hosts:
        out += [
            _unit("proxmox-updates", "Proxmox Updates - Weekly Guarded", "hisho-proxmox-update", (30, 23, 0),
                  "Sundays 11:30 PM: every Proxmox server to its newest point release, one at a time, stopping at "
                  "the first failure (hisho proxmox-update).", "proxmox"),
            _unit("proxmox-update-check", "Proxmox Update Check", "hisho-proxmox-update-check", (15, 6, None),
                  "6:15 AM: how many updates wait on each awake Proxmox server (wakes none, installs nothing).",
                  "proxmox"),
            _job("proxmox-root-keys", "Proxmox Root Keys - Manual Guarded", "playbooks/proxmox-root-keys.yml",
                 {"confirm_proxmox_root_keys": True, **key_vars}, None,
                 "Adds Semaphore's key to root on every Proxmox server (/etc/pve/priv/authorized_keys). Removes nothing.",
                 "proxmox"),
        ]
        for h in hosts:
            out.append(_unit(f"{PER_MACHINE}{h}", f"Proxmox Update - {h} - Manual Guarded", f"hisho-proxmox-update@{h}",
                             None, f"Updates {h} now, the same guarded way as the weekly run "
                                   f"(hisho proxmox-update run {h}).", "proxmox"))
    return out


def cron_for(when: tuple[int, int | None, int | None], owner_tz: str, sem_tz: str,
             today: datetime | None = None) -> str:
    """The cron line for Semaphore, whose schedules run in ``sem_tz``, of a time in the owner's ``owner_tz``."""
    minute, hour, dow = when
    if hour is None:
        return f"{minute} * * * *"
    if owner_tz == sem_tz:
        return f"{minute} {hour} * * {'*' if dow is None else dow}"
    base = (today or datetime.now(ZoneInfo(owner_tz))).astimezone(ZoneInfo(owner_tz))
    local = base.replace(hour=hour, minute=minute, second=0, microsecond=0)
    there = local.astimezone(ZoneInfo(sem_tz))
    shift = (there.date() - local.date()).days
    day = "*" if dow is None else str((dow + shift) % 7)
    return f"{there.minute} {there.hour} * * {day}"


# ── the machine list ───────────────────────────────────────────────────────────────────────────────

def _host_of(value: str) -> str:
    text = re.sub(r"^\w+://", "", str(value or "")).split("/")[0]
    return text.rsplit("@", 1)[-1].split(":")[0]


def machines(fleet: Any, fleet_data: dict[str, Any], servers: dict[str, Any], proxmox: dict[str, Any],
             me: str = "") -> dict[str, dict[str, dict[str, Any]]]:
    """``{group: {host: vars}}``: hisho_core (the Core machine), hisho_fleet (every Fleet.yaml machine),
    hisho_servers (Servers entries), proxmox_hosts (Proxmox entries, as root)."""
    me = me or os.environ.get("USER") or ""
    groups: dict[str, dict[str, dict[str, Any]]] = {"hisho_core": {}, "hisho_fleet": {}, "hisho_servers": {},
                                                     "proxmox_hosts": {}}

    def where(m: str) -> dict[str, Any]:
        target = str(fleet.ssh.get(m) or "")
        user = target.rsplit("@", 1)[0] if "@" in target else ""
        addr = _host_of(target)
        if not addr and m == fleet.core_host:   # a single-machine install: no ssh: line for itself
            tail = str((fleet_data.get("tailnet") or {}).get("suffix") or "").strip(".")
            url_host = _host_of(fleet.core_url)
            addr = f"{m}.{tail}" if tail else (url_host if url_host not in ("127.0.0.1", "localhost", "") else m)
            user = user or me
        out = {"ansible_host": addr or m, "hisho_name": m}
        if user:
            out["ansible_user"] = user
        return out

    for m in fleet.machines:
        groups["hisho_fleet"][m] = where(m)
    groups["hisho_core"][fleet.core_host] = groups["hisho_fleet"].get(fleet.core_host) or where(fleet.core_host)
    for name, e in sorted((servers or {}).items()):
        if not (e or {}).get("host"):
            continue
        hv = {"ansible_host": _host_of(e["host"]), "hisho_name": _name(name)}
        if e.get("user"):
            hv["ansible_user"] = str(e["user"])
        if e.get("port"):
            hv["ansible_port"] = int(e["port"])
        groups["hisho_servers"][_name(name)] = hv
    for name, e in sorted((proxmox or {}).items()):
        if (e or {}).get("url"):
            groups["proxmox_hosts"][_name(name)] = {"ansible_host": _host_of(e["url"]), "ansible_user": "root",
                                                    "hisho_name": _name(name)}
    return groups


def render_inventory(groups: dict[str, dict[str, dict[str, Any]]]) -> str:
    seen: set[str] = set()
    children: dict[str, Any] = {}
    for group, hosts in groups.items():
        rendered = {}
        for host, hv in hosts.items():
            rendered[host] = None if host in seen else dict(hv)
            seen.add(host)
        children[group] = {"hosts": rendered or {}}
    return ("# Made by Hisho from its machine list (Fleet.yaml machines, Servers and Proxmox entries).\n"
            "# Hisho rewrites it on every sync: add machines in Hisho, not here.\n"
            + yaml.safe_dump({"all": {"vars": {"ansible_ssh_common_args": "-o StrictHostKeyChecking=accept-new"},
                                      "children": children}}, sort_keys=False, default_flow_style=False))


# ── state ──────────────────────────────────────────────────────────────────────────────────────────

def load_state(entry_name: str) -> dict[str, Any]:
    from hisho import addons
    return dict((addons.read_config(CONFIG).get("hisho_setup") or {}).get(entry_name) or {})


def save_state(entry_name: str, state: dict[str, Any] | None) -> None:
    from hisho import addons
    config = addons.read_config(CONFIG)
    setup = dict(config.get("hisho_setup") or {})
    if state is None:
        setup.pop(entry_name, None)
    else:
        setup[entry_name] = state
    config["hisho_setup"] = setup
    addons.write_config(CONFIG, config)


def fingerprint(t: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps({k: t.get(k) for k in HASHED}, sort_keys=True, default=str).encode()).hexdigest()[:16]


# ── the key ────────────────────────────────────────────────────────────────────────────────────────

def keygen(ctx: Any, comment: str) -> tuple[str, str]:
    """A new ed25519 key pair (private, public). The private half is returned once and kept nowhere on disk."""
    d = tempfile.mkdtemp(prefix="hisho-semaphore-key-")
    try:
        os.chmod(d, 0o700)
        path = os.path.join(d, "key")
        res = ctx.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", comment, "-f", path], timeout=30)
        if getattr(res, "returncode", 1):
            raise RuntimeError(f"ssh-keygen failed: {(res.stderr or '').strip()[:200]}")
        return Path(path).read_text(), Path(path + ".pub").read_text().strip()
    finally:
        shutil.rmtree(d, ignore_errors=True)


def _add_line_script(key: str) -> str:
    k = shlex.quote(key)
    return ("umask 077; mkdir -p ~/.ssh; touch ~/.ssh/authorized_keys; "
            f"grep -qxF {k} ~/.ssh/authorized_keys || printf '%s\\n' {k} >> ~/.ssh/authorized_keys")


def key_targets(fleet: Any, servers: dict[str, Any], server_ssh: Callable[[dict[str, Any]], list[str]] | None,
                local: Callable[[str], bool]) -> dict[str, list[str]]:
    """``{"machine:<m>" | "server:<entry>": command}`` that adds the key line on each (one line, nothing removed).
    Fleet machines by their SSH alias (Hisho's adopt writes one on Core and the workstation); this machine locally;
    Servers entries with the Servers add-on's own key and pinned host key."""
    out: dict[str, list[str]] = {}
    for m in fleet.machines:
        out[f"machine:{m}"] = (["sh", "-c"] if local(m) else
                               ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", m])
    if server_ssh:
        for name, e in (servers or {}).items():
            if (e or {}).get("host") and (e or {}).get("user"):
                try:
                    out[f"server:{name}"] = list(server_ssh(dict(e)))
                except Exception:  # noqa: BLE001 -- an entry it can't read is skipped, not a broken sync
                    continue
    return out


def place_key(ctx: Any, key: str, targets: dict[str, list[str]], state: dict[str, Any],
              only: list[str] | None = None, now: float = 0.0, force: bool = False) -> list[str]:
    """Put the key line on each target not done yet. Returns problems; successes are kept in state ``keyed``."""
    keyed = set(state.get("keyed") or [])
    problems = []
    for tid, argv in targets.items():
        if (only is not None and tid not in only) or (tid in keyed and not force):
            continue
        if not force and tid in _key_tried and now - _key_tried[tid] < KEY_RETRY:
            continue
        _key_tried[tid] = now
        try:
            res = ctx.run(argv + [_add_line_script(key)], timeout=40)
            ok = not getattr(res, "returncode", 1)
        except Exception as exc:  # noqa: BLE001
            ok, res = False, type("R", (), {"stderr": str(exc)})()
        if ok:
            keyed.add(tid)
        else:
            problems.append(f"Semaphore's key not on {tid.split(':', 1)[1]} yet: {str(getattr(res, 'stderr', '')).strip()[:120]}")
    state["keyed"] = sorted(keyed)
    return problems


# ── making it ──────────────────────────────────────────────────────────────────────────────────────

def _find(items: list[dict[str, Any]], **want: Any) -> dict[str, Any] | None:
    return next((i for i in items or [] if all(i.get(k) == v for k, v in want.items())), None)


def _unit_of(t: dict[str, Any]) -> str:
    m = re.search(r'hisho_unit\\?"\s*:\s*\\?"(hisho-[a-z0-9@-]+)', str(t.get("arguments") or ""))
    return m.group(1) if m else ""


def ensure_base(api: Api, ctx: Any, state: dict[str, Any], playbooks: str, comment: str) -> list[str]:
    """Project, keys, repository, environment and inventory: made once, found again by the ids kept in state."""
    done = []
    projects = api("GET", "/projects") or []
    project = _find(projects, id=state.get("project_id")) or _find(projects, name=PROJECT)
    if project is None:
        project = api("POST", "/projects", {"name": PROJECT, "alert": False}) or {}
        if not project.get("id"):
            project = _find(api("GET", "/projects") or [], name=PROJECT) or {}
        done.append(f"made the project {PROJECT}")
    pid = int(project["id"])
    state["project_id"] = pid
    keys = api("GET", f"/project/{pid}/keys") or []
    if not _find(keys, id=state.get("key_id")):
        private, public = keygen(ctx, comment)
        made = api("POST", f"/project/{pid}/keys", {"name": KEY_NAME, "type": "ssh", "project_id": pid,
                                                    "ssh": {"login": "", "passphrase": "", "private_key": private}})
        del private
        made = made or _find(api("GET", f"/project/{pid}/keys") or [], name=KEY_NAME) or {}
        state["key_id"], state["public_key"], state["keyed"] = made.get("id"), public, []
        done.append("made Semaphore's SSH key (key store)")
    if not _find(keys, id=state.get("none_key_id")):
        made = api("POST", f"/project/{pid}/keys", {"name": NONE_KEY, "type": "none", "project_id": pid}) \
            or _find(api("GET", f"/project/{pid}/keys") or [], name=NONE_KEY) or {}
        state["none_key_id"] = made.get("id")
    repos = api("GET", f"/project/{pid}/repositories") or []
    if not _find(repos, id=state.get("repository_id")):
        made = api("POST", f"/project/{pid}/repositories", {"name": REPOSITORY, "project_id": pid, "git_url": playbooks,
                                                            "git_branch": "", "ssh_key_id": state["none_key_id"]}) \
            or _find(api("GET", f"/project/{pid}/repositories") or [], name=REPOSITORY) or {}
        state["repository_id"] = made.get("id")
        done.append(f"added the repository {REPOSITORY} ({playbooks})")
    envs = api("GET", f"/project/{pid}/environment") or []
    if not _find(envs, id=state.get("environment_id")):
        made = api("POST", f"/project/{pid}/environment", {"name": ENVIRONMENT, "project_id": pid, "json": "{}",
                                                           "env": "{}"}) \
            or _find(api("GET", f"/project/{pid}/environment") or [], name=ENVIRONMENT) or {}
        state["environment_id"] = made.get("id")
    return done


def sync_inventory(api: Api, state: dict[str, Any], text: str) -> list[str]:
    """Only the inventory Hisho made (by the id it kept); made when missing."""
    pid = state["project_id"]
    invs = api("GET", f"/project/{pid}/inventory") or []
    cur = _find(invs, id=state.get("inventory_id"))
    body = {"name": INVENTORY, "project_id": pid, "type": "static-yaml", "inventory": text,
            "ssh_key_id": state.get("key_id")}
    if cur is None:
        made = api("POST", f"/project/{pid}/inventory", body) or _find(api("GET", f"/project/{pid}/inventory") or [],
                                                                        name=INVENTORY) or {}
        state["inventory_id"] = made.get("id")
        return [f"made the inventory {INVENTORY}"]
    if cur.get("inventory") != text:
        api("PUT", f"/project/{pid}/inventory/{cur['id']}", {**cur, "inventory": text})
        return [f"{INVENTORY} follows Hisho's machine list"]
    return []


def template_body(job: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
    pid = state["project_id"]
    return {"project_id": pid, "name": job["name"], "playbook": job["playbook"], "app": "ansible", "type": "",
            "inventory_id": state.get("inventory_id"), "repository_id": state.get("repository_id"),
            "environment_id": state.get("environment_id"),
            "description": f"Added by Hisho. {job['about']}".strip(),
            "arguments": json.dumps(["--extra-vars", json.dumps(job["extra"], sort_keys=True)]) if job["extra"] else "[]",
            "survey_vars": [], "autorun": False}


def sync_jobs(api: Api, state: dict[str, Any], wanted: list[dict[str, Any]], crons: dict[str, str],
              may_remove: bool = True) -> dict[str, list[str]]:
    """Add Hisho's missing jobs and schedules, bring unedited ones back in step, never touch edited ones, and remove
    a per-machine job whose machine is gone (only when unedited). Returns {"done", "edited", "skipped"}."""
    pid = state["project_id"]
    recorded: dict[str, dict[str, Any]] = dict(state.get("jobs") or {})
    templates = api("GET", f"/project/{pid}/templates") or []
    schedules = api("GET", f"/project/{pid}/schedules") or []
    by_id = {t.get("id"): t for t in templates}
    elsewhere: dict[str, str] = {}   # a Hisho unit some other project already runs: never a second copy
    for p in api("GET", "/projects") or []:
        if p.get("id") == pid:
            continue
        for t in api("GET", f"/project/{p['id']}/templates") or []:
            if _unit_of(t):
                elsewhere.setdefault(_unit_of(t), f"'{t.get('name')}' in {p.get('name')}")
    done, edited, skipped = [], [], []
    keys = {j["key"] for j in wanted}
    for job in wanted:
        body = template_body(job, state)
        rec = recorded.get(job["key"]) or {}
        cur = by_id.get(rec.get("id")) if rec else None
        if rec and cur is None:
            skipped.append(f"{job['name']}: deleted in Semaphore, not added back")
            continue
        if cur is None:
            same = _find(templates, name=job["name"])
            unit = job["extra"].get("hisho_unit", "")
            if same is not None:
                skipped.append(f"{job['name']}: a job with this name is already in {PROJECT} (not Hisho's), kept")
                continue
            if unit and unit in elsewhere:
                skipped.append(f"{job['name']}: {elsewhere[unit]} already runs {unit}")
                continue
            made = api("POST", f"/project/{pid}/templates", body) or {}
            tid = made.get("id") or (_find(api("GET", f"/project/{pid}/templates") or [], name=job["name"]) or {}).get("id")
            cur = (api("GET", f"/project/{pid}/templates/{tid}") if tid else None) or {**body, "id": tid}
            rec = {"id": tid, "hash": fingerprint(cur), "want": fingerprint(body)}   # as Semaphore stored it
            done.append(f"added {job['name']}")
        elif fingerprint(cur) != rec.get("hash"):
            edited.append(job["name"])
            recorded[job["key"]] = rec
            continue
        elif fingerprint(body) != rec.get("want"):   # Hisho's own definition changed (a newer add-on)
            api("PUT", f"/project/{pid}/templates/{cur['id']}", {**cur, **{k: body[k] for k in HASHED if k in body}})
            cur = api("GET", f"/project/{pid}/templates/{cur['id']}") or {**cur, **body}
            rec = {**rec, "hash": fingerprint(cur), "want": fingerprint(body)}
            done.append(f"brought {job['name']} in step")
        cron = crons.get(job["key"])
        if cron and rec.get("id"):
            sched = _find(schedules, id=rec.get("schedule_id")) if rec.get("schedule_id") else None
            if rec.get("schedule_id") and sched is None:
                skipped.append(f"{job['name']}: its schedule was deleted in Semaphore, not added back")
            elif sched is None:
                made = api("POST", f"/project/{pid}/schedules", {"project_id": pid, "template_id": rec["id"],
                                                                 "name": job["name"], "cron_format": cron,
                                                                 "active": True}) or {}
                rec["schedule_id"], rec["cron"] = made.get("id"), cron
                done.append(f"scheduled {job['name']} at {cron}")
            elif sched.get("cron_format") != rec.get("cron"):
                edited.append(f"{job['name']} (schedule)")
            elif sched.get("cron_format") != cron:
                api("PUT", f"/project/{pid}/schedules/{sched['id']}", {**sched, "cron_format": cron})
                rec["cron"] = cron
                done.append(f"moved {job['name']} to {cron}")
        recorded[job["key"]] = rec
    for key in [k for k in recorded if k.startswith(PER_MACHINE) and k not in keys]:
        if not may_remove:
            continue
        rec = recorded[key]
        cur = by_id.get(rec.get("id"))
        if cur is None:
            recorded.pop(key)
            continue
        if fingerprint(cur) != rec.get("hash"):
            edited.append(f"{cur.get('name')} (its server is gone from Hisho; kept because it was edited)")
            continue
        for s in schedules:
            if s.get("template_id") == cur["id"]:
                api("DELETE", f"/project/{pid}/schedules/{s['id']}")
        api("DELETE", f"/project/{pid}/templates/{cur['id']}")
        recorded.pop(key)
        done.append(f"removed {cur.get('name')} (its server is gone from Hisho)")
    state["jobs"] = recorded
    return {"done": done, "edited": edited, "skipped": skipped}


def refresh_playbooks(src: Path, dest: Path) -> bool:
    """Copy the add-on's ansible/ folder into the folder Hisho's install mounts into Semaphore (only that folder;
    only files that differ). Returns whether anything changed."""
    changed = False
    for f in sorted(p for p in src.rglob("*") if p.is_file() and p.suffix in (".yml", ".yaml")):
        target = dest / f.relative_to(src)
        data = f.read_bytes()
        if target.exists() and target.read_bytes() == data:
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        target.chmod(0o644)
        changed = True
    return changed
