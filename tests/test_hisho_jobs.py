"""Hisho's own jobs in Semaphore (hisho_jobs.py), against a Semaphore that lives only in this test (no network)."""
import io
import json
import re
import subprocess
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
import yaml

from hisho import addons

HERE = Path(__file__).resolve().parents[1]
URL = "http://198.51.100.9:3000"   # TEST-NET-2: nothing real answers here
PUB = "ssh-ed25519 AAAATESTKEY semaphore-hisho@core1"
FLEET = {
    "machines": ["core1", "desk", "worker1"],
    "core": {"host": "core1", "url": "http://100.64.0.10:8787"},
    "workstation": "desk",
    "ssh": {"core1": "owner@100.64.0.10", "desk": "owner@100.64.0.11", "worker1": "owner@100.64.0.12"},
    "timezone": "America/Los_Angeles",
    "secrets": {"backends": ["infisical"]},
    "addons": {"enabled": {"semaphore": {}}},
}


class FakeSemaphore:
    """Semaphore's REST API, in memory: projects and their keys, repositories, environments, inventories,
    templates and schedules, plus the calls made."""
    KINDS = ("keys", "repositories", "environment", "inventory", "templates", "schedules")

    def __init__(self):
        self.projects, self.calls, self.next = [], [], 100
        self.store: dict[tuple[int, str], list[dict]] = {}

    def _id(self):
        self.next += 1
        return self.next

    def handle(self, method, path, body):
        self.calls.append((method, path, body))
        if path == "/projects":
            if method == "POST":
                p = {"id": self._id(), **body}
                self.projects.append(p)
                return p
            return self.projects
        m = re.fullmatch(r"/project/(\d+)/([a-z]+)(?:/(\w+))?", path)
        assert m, path
        pid, kind, oid = int(m.group(1)), m.group(2), m.group(3)
        if kind == "tasks":
            return []
        assert kind in self.KINDS, path
        items = self.store.setdefault((pid, kind), [])
        if oid is None:
            if method == "POST":
                item = {"id": self._id(), **body}
                if kind == "keys":            # Semaphore never hands a private key back
                    item.pop("ssh", None)
                items.append(item)
                return item
            return [dict(i) for i in items]
        item = next((i for i in items if str(i["id"]) == oid), None)
        if method == "GET":
            return dict(item) if item else None
        if method == "PUT":
            item.clear()
            item.update(body)
            return None
        if method == "DELETE":
            items.remove(item)
            return None
        raise AssertionError(method)

    def all(self, kind, name="Hisho"):
        pid = next(p["id"] for p in self.projects if p["name"] == name)
        return self.store.get((pid, kind), [])

    def writes(self):
        return [c for c in self.calls if c[0] != "GET"]


class Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


@pytest.fixture
def sem(fleet, monkeypatch, stand_in):
    (fleet / "System/Config/Fleet.yaml").write_text(yaml.safe_dump(FLEET, sort_keys=False))
    fake = FakeSemaphore()

    def urlopen(req, timeout=0):
        assert req.full_url.startswith(URL + "/api"), req.full_url   # only the fake, never a real Semaphore
        path = req.full_url.split("/api", 1)[1]
        out = fake.handle(req.get_method(), path, json.loads(req.data) if req.data else None)
        return Resp(json.dumps(out).encode() if out is not None else b"")
    monkeypatch.setattr("urllib.request.urlopen", urlopen)
    ran = []

    def run(self, cmd, timeout=30, env=None):
        ran.append(list(cmd))
        if cmd[0] == "ssh-keygen":
            path = cmd[cmd.index("-f") + 1]
            Path(path).write_text("-----BEGIN OPENSSH PRIVATE KEY-----\nTEST\n-----END OPENSSH PRIVATE KEY-----\n")
            Path(path + ".pub").write_text(PUB + "\n")
        return subprocess.CompletedProcess(cmd, 0, "", "")
    monkeypatch.setattr(addons.Context, "run", run)
    monkeypatch.setattr(addons.Context, "secret", lambda self, name: "tok")
    monkeypatch.setattr(addons, "is_local", lambda host: host == "core1")
    fake.ran = ran
    fake.stand_in = stand_in
    fake.root = fleet
    addon = addons.get("semaphore")
    addons.set_entry(addon, "main", {"url": URL, "token": "HISHO_SEM_TOKEN"})
    fake.addon = addon
    addons.provider(addon).hisho_jobs._key_tried.clear()
    return fake


def installed(fake):
    """Install in Hisho's record (companion.py writes it after installing Semaphore)."""
    path = fake.root / "System/Config/Fleet.yaml"
    data = yaml.safe_load(path.read_text())
    data["addons"]["companions"] = {"semaphore": {"state": "installed", "host": "core1", "dir": "~/semaphore",
                                                  "entry": {"url": URL}}}
    path.write_text(yaml.safe_dump(data, sort_keys=False))


def enable(fake, *names):
    path = fake.root / "System/Config/Fleet.yaml"
    data = yaml.safe_load(path.read_text())
    for n in names:
        data["addons"]["enabled"][n] = {}
    path.write_text(yaml.safe_dump(data, sort_keys=False))


def templates(fake):
    return {t["name"]: t for t in fake.all("templates")}


def inventory(fake):
    [inv] = fake.all("inventory")
    return yaml.safe_load(inv["inventory"])["all"]["children"]


def mod():
    return addons.provider(addons.get("semaphore"))


def state():
    return addons.read_config("semaphore")["hisho_setup"]["main"]


ALWAYS = {"Hisho Core Self-Update", "Hisho Code Sync", "Weekly Security Review", "OS Patch Dry Run",
          "Pending Updates Report", "Failed Services Report", "Disk Usage Report", "Authorized Keys - Manual Guarded",
          "Hisho Secret Notes"}   # the last one because the test fleet keeps secrets in Infisical


def test_secret_notes_only_with_a_secrets_store():
    hj = mod_jobs()
    names = lambda backends: {j["name"] for j in hj.jobs(hj.features({"secrets": {"backends": backends}}, {}, {}), PUB)}  # noqa: E731
    assert "Hisho Secret Notes" not in names(["local"])
    assert "Hisho Secret Notes" in names(["infisical"]) and "Hisho Secret Notes" in names(["bws"])


def test_install_in_hisho_sets_up_the_project_and_a_second_run_changes_nothing(sem):
    installed(sem)
    assert addons.on_change("companion-installed", {"addon": "semaphore"}, wait=True) == []
    [project] = sem.projects
    assert project["name"] == "Hisho"
    keys = sem.all("keys")
    assert {k["type"] for k in keys} == {"ssh", "none"}
    posted = next(c for c in sem.calls if c[:2] == ("POST", f"/project/{project['id']}/keys") and c[2]["type"] == "ssh")
    assert "PRIVATE KEY" in posted[2]["ssh"]["private_key"]        # only into Semaphore's key store
    saved = (sem.root / "System/Config/Addons/semaphore.yaml").read_text()
    assert "PRIVATE" not in saved and PUB in saved                 # Hisho keeps the public half only
    [repo] = sem.all("repositories")
    assert repo["git_url"] == "/hisho/ansible" and repo["git_branch"] == ""
    assert repo["ssh_key_id"] == next(k["id"] for k in keys if k["type"] == "none")
    inv = inventory(sem)
    assert list(inv["hisho_core"]["hosts"]) == ["core1"]
    assert inv["hisho_fleet"]["hosts"]["desk"]["ansible_host"] == "100.64.0.11"
    assert set(inv["hisho_fleet"]["hosts"]) == {"core1", "desk", "worker1"}
    t = templates(sem)
    assert set(t) == ALWAYS
    for tpl in t.values():
        assert tpl["repository_id"] == repo["id"] and tpl["inventory_id"] == sem.all("inventory")[0]["id"]
        assert (HERE / "ansible" / tpl["playbook"]).is_file()   # every job's playbook ships with the add-on
    args = json.loads(json.loads(t["Hisho Core Self-Update"]["arguments"])[1])
    assert args == {"hisho_unit": "hisho-self-update"}
    keyvars = json.loads(json.loads(t["Authorized Keys - Manual Guarded"]["arguments"])[1])
    assert keyvars == {"confirm_authorized_keys": True, "hisho_semaphore_key": PUB}
    crons = {s["name"]: s["cron_format"] for s in sem.all("schedules")}
    assert crons == {"Hisho Core Self-Update": "40 * * * *", "Hisho Code Sync": "20 * * * *",
                     "Weekly Security Review": "30 6 * * 1", "OS Patch Dry Run": "0 5 * * 6",
                     "Pending Updates Report": "0 6 * * *", "Failed Services Report": "5 6 * * *",
                     "Disk Usage Report": "10 6 * * *", "Hisho Secret Notes": "50 * * * *"}
    assert all(s["active"] for s in sem.all("schedules"))
    # Semaphore's key went on every machine: this one locally, the others over their SSH alias, one line added
    placed = [c for c in sem.ran if c[0] in ("sh", "ssh")]
    assert [c[0] if c[0] == "sh" else c[-2] for c in placed] == ["sh", "desk", "worker1"]
    assert all("grep -qxF" in c[-1] and ">>" in c[-1] and "rm " not in c[-1] for c in placed)
    assert state()["keyed"] == ["machine:core1", "machine:desk", "machine:worker1"]
    # nothing was run
    assert not any("/tasks" in c[1] and c[0] == "POST" for c in sem.calls)
    # the schedules run in the owner's zone: Hisho's install sets it on the entry
    assert addons.entries(sem.addon)["main"]["timezone"] == "America/Los_Angeles"
    # again (the regular sync): nothing written, no ssh
    n_writes, n_ran = len(sem.writes()), len(sem.ran)
    s = addons.summarize(sem.addon)
    assert s["state"] == "ok", s
    # (the maintenance gate's variables go into the environment once; that is the provider's older sync)
    assert [c for c in sem.writes()[n_writes:] if "/environment/" not in c[1]] == []
    n_writes = len(sem.writes())
    addons.summarize(sem.addon)
    assert sem.writes()[n_writes:] == []
    assert len(sem.ran) == n_ran
    card = next(d for d in addons.details(sem.addon) if d["title"] == "main · Hisho's jobs")
    assert card["facts"][0] == "9 Hisho jobs set up"


def test_a_connected_semaphore_gets_nothing_until_the_button(sem):
    addons.summarize(sem.addon)
    addons.on_change("entry-set", {"addon": "proxmox", "entry": "pve-a"}, wait=True)
    assert sem.projects == [] and sem.writes() == [] and not any(c[0] == "ssh-keygen" for c in sem.ran)
    card = next(d for d in addons.details(sem.addon) if d["title"] == "main · Hisho's jobs")
    [item] = card["items"]
    assert item["actions"][0]["id"] == "add-hisho-jobs" and item["actions"][0]["confirm"]
    res = addons.act(sem.addon, "main", "hisho-jobs", "add-hisho-jobs")
    assert res["ok"] and res["message"].startswith("9 Hisho jobs set up"), res
    assert set(templates(sem)) == ALWAYS
    assert "timezone" not in addons.entries(sem.addon)["main"]   # a connected one keeps its own (UTC here)
    crons = {s["name"]: s["cron_format"] for s in sem.all("schedules")}
    assert crons["Disk Usage Report"] in ("10 13 * * *", "10 14 * * *")   # 6:10 AM Pacific, in UTC


def test_an_edited_job_is_left_alone_and_reported(sem):
    installed(sem)
    m = mod()
    m.reconcile("main", addons.entries(sem.addon)["main"], addons.Context(sem.addon), first=True)
    t = templates(sem)["Disk Usage Report"]
    t["description"] = "my own words"
    t["playbook"] = "playbooks/my-disk.yml"
    sched = next(s for s in sem.all("schedules") if s["name"] == "Hisho Code Sync")
    sched["cron_format"] = "25 * * * *"
    n = len(sem.writes())
    res = m.reconcile("main", addons.entries(sem.addon)["main"], addons.Context(sem.addon))
    assert templates(sem)["Disk Usage Report"]["playbook"] == "playbooks/my-disk.yml"
    assert sched["cron_format"] == "25 * * * *"
    assert [c for c in sem.writes()[n:]] == []
    assert res["state"]["last"]["edited"] == ["Hisho Code Sync (schedule)", "Disk Usage Report"]
    card = next(d for d in addons.details(sem.addon) if d["title"] == "main · Hisho's jobs")
    assert any("Edited, left alone: Hisho Code Sync (schedule), Disk Usage Report" in f for f in card["facts"])


def test_never_a_second_copy(sem):
    """A job with the same name in the Hisho project, or another project already running the same Hisho unit."""
    other = sem.handle("POST", "/projects", {"name": "Infra"})
    sem.handle("POST", f"/project/{other['id']}/templates", {
        "name": "Hisho Core Self-Update (old)", "playbook": "ansible/playbooks/hisho-unit.yml",
        "arguments": json.dumps(["--extra-vars", json.dumps({"hisho_unit": "hisho-self-update"})])})
    mine = sem.handle("POST", "/projects", {"name": "Hisho"})
    sem.handle("POST", f"/project/{mine['id']}/templates", {"name": "Disk Usage Report", "playbook": "mine.yml"})
    res = addons.act(sem.addon, "main", "hisho-jobs", "add-hisho-jobs")
    assert res["ok"]
    names = [t["name"] for t in sem.all("templates")]
    assert names.count("Disk Usage Report") == 1 and "Hisho Core Self-Update" not in names
    last = state()["last"]
    assert any("Hisho Core Self-Update (old)' in Infra already runs hisho-self-update" in s for s in last["skipped"])
    assert any(s.startswith("Disk Usage Report: a job with this name") for s in last["skipped"])
    assert len(sem.projects) == 2   # the existing "Hisho" project was used, not a second one


def test_forgejo_secrets_and_proxmox_jobs_only_when_those_are_on(sem):
    installed(sem)
    m = mod()
    m.reconcile("main", addons.entries(sem.addon)["main"], addons.Context(sem.addon), first=True)
    assert set(templates(sem)) == ALWAYS
    enable(sem, "forgejo")
    data = yaml.safe_load((sem.root / "System/Config/Fleet.yaml").read_text())
    data["secrets"]["backends"] = ["infisical", "bws"]
    (sem.root / "System/Config/Fleet.yaml").write_text(yaml.safe_dump(data))
    m.reconcile("main", addons.entries(sem.addon)["main"], addons.Context(sem.addon))
    assert set(templates(sem)) - ALWAYS == {"Hisho Forgejo Backup", "Hisho Forgejo Copy", "Hisho Forgejo Mirrors",
                                            "Forgejo Upgrade - Manual Guarded", "Hisho Add-on Mirror",
                                            "Hisho Secrets Mirror"}
    px = sem.stand_in("proxmox", kind="integration", entry_schema={"url": "str"})
    enable(sem, "proxmox")
    addons.set_entry(px, "pve-a", {"url": "https://192.0.2.30:8006"})   # event: its button at once
    t = templates(sem)
    assert {"Proxmox Updates - Weekly Guarded", "Proxmox Update Check", "Proxmox Root Keys - Manual Guarded",
            "Proxmox Update - pve-a - Manual Guarded"} <= set(t)
    args = json.loads(json.loads(t["Proxmox Update - pve-a - Manual Guarded"]["arguments"])[1])
    assert args == {"hisho_unit": "hisho-proxmox-update@pve-a"}
    crons = {s["name"]: s["cron_format"] for s in sem.all("schedules")}
    assert crons["Proxmox Updates - Weekly Guarded"] == "30 23 * * 0"
    assert crons["Hisho Forgejo Backup"] == "20 20 * * *"
    assert "Proxmox Update - pve-a - Manual Guarded" not in crons   # a button only
    assert inventory(sem)["proxmox_hosts"]["hosts"]["pve-a"] == {"ansible_host": "192.0.2.30", "ansible_user": "root",
                                                                 "hisho_name": "pve-a"}
    # turning Forgejo off again removes nothing (only a removed machine's own job goes)
    data = yaml.safe_load((sem.root / "System/Config/Fleet.yaml").read_text())
    data["addons"]["enabled"].pop("forgejo")
    (sem.root / "System/Config/Fleet.yaml").write_text(yaml.safe_dump(data))
    m.reconcile("main", addons.entries(sem.addon)["main"], addons.Context(sem.addon))
    assert "Hisho Forgejo Backup" in templates(sem)


def test_removing_a_proxmox_entry_removes_its_unedited_job_and_keeps_an_edited_one(sem):
    installed(sem)
    px = sem.stand_in("proxmox", kind="integration", entry_schema={"url": "str"})
    enable(sem, "proxmox")
    addons.set_entry(px, "pve-a", {"url": "https://192.0.2.30:8006"})
    addons.set_entry(px, "pve-b", {"url": "https://192.0.2.31:8006"})
    mod().reconcile("main", addons.entries(sem.addon)["main"], addons.Context(sem.addon), first=True)
    assert {"Proxmox Update - pve-a - Manual Guarded", "Proxmox Update - pve-b - Manual Guarded"} <= set(templates(sem))
    templates(sem)["Proxmox Update - pve-b - Manual Guarded"]["description"] = "mine now"
    addons.set_entry(px, "pve-a", None)
    addons.set_entry(px, "pve-b", None)
    t = templates(sem)
    assert "Proxmox Update - pve-a - Manual Guarded" not in t
    assert t["Proxmox Update - pve-b - Manual Guarded"]["description"] == "mine now"
    assert ("DELETE", f"/project/{sem.projects[0]['id']}/templates/"
            f"{next(c for c in sem.calls if c[0] == 'DELETE')[1].split('/')[-1]}", None) in sem.calls
    assert sum(1 for c in sem.calls if c[0] == "DELETE") == 1   # the only deletion: pve-a's own job
    assert "proxmox_hosts" in inventory(sem) and inventory(sem)["proxmox_hosts"]["hosts"] == {}
    assert any("pve-b" in e and "kept because it was edited" in e for e in state()["last"]["edited"])
    assert set(ALWAYS) <= set(t)   # nothing else touched


def test_a_new_machine_joins_the_inventory_and_gets_the_key_and_a_retired_one_leaves(sem):
    installed(sem)
    mod().reconcile("main", addons.entries(sem.addon)["main"], addons.Context(sem.addon), first=True)
    path = sem.root / "System/Config/Fleet.yaml"
    data = yaml.safe_load(path.read_text())
    data["machines"].append("box3")
    data["ssh"]["box3"] = "owner@100.64.0.13"
    path.write_text(yaml.safe_dump(data))
    sem.ran.clear()
    assert addons.on_adopt("box3") == []          # hisho adopt / hisho create / Add a machine
    assert inventory(sem)["hisho_fleet"]["hosts"]["box3"]["ansible_host"] == "100.64.0.13"
    [cmd] = [c for c in sem.ran if c[0] == "ssh"]   # only the new machine, one line added
    assert cmd[-2] == "box3" and PUB in cmd[-1] and "grep -qxF" in cmd[-1]
    assert "machine:box3" in state()["keyed"]
    # retired: out of Fleet.yaml, then Core tells the add-ons (hisho retire)
    data["machines"].remove("worker1")
    data["ssh"].pop("worker1")
    path.write_text(yaml.safe_dump(data))
    n = len(sem.writes())
    assert addons.on_change("machine-removed", {"machine": "worker1"}, wait=True) == []
    assert "worker1" not in inventory(sem)["hisho_fleet"]["hosts"]
    assert [c[0] for c in sem.writes()[n:]] == ["PUT"]   # only Hisho's inventory changed, nothing deleted
    [inv] = sem.all("inventory")
    assert inv["id"] == state()["inventory_id"]


def test_a_new_servers_entry_joins_and_gets_the_key_with_the_servers_add_on_login(sem):
    installed(sem)
    srv = sem.stand_in("server", kind="integration", entry_schema={"host": "str", "user": "str"})
    (srv.path / "provider.py").write_text(
        (srv.path / "provider.py").read_text()
        + "\n\ndef _ssh_args(entry):\n    return ['ssh', '-i', 'servers-key', f\"{entry['user']}@{entry['host']}\"]\n")
    enable(sem, "server")
    mod().reconcile("main", addons.entries(sem.addon)["main"], addons.Context(sem.addon), first=True)
    sem.ran.clear()
    addons.set_entry(addons.get("server"), "web-1", {"host": "203.0.113.20", "user": "deploy"})
    assert inventory(sem)["hisho_servers"]["hosts"]["web-1"] == {"ansible_host": "203.0.113.20", "hisho_name": "web-1",
                                                                 "ansible_user": "deploy"}
    [cmd] = [c for c in sem.ran if c[0] == "ssh"]
    assert cmd[:4] == ["ssh", "-i", "servers-key", "deploy@203.0.113.20"] and PUB in cmd[-1]
    addons.set_entry(addons.get("server"), "web-1", None)
    assert inventory(sem)["hisho_servers"]["hosts"] == {}


def test_a_machine_that_cannot_take_the_key_is_retried_later_not_every_minute(sem, monkeypatch):
    installed(sem)
    m = mod()

    def run(self, cmd, timeout=30, env=None):
        sem.ran.append(list(cmd))
        if cmd[0] == "ssh-keygen":
            path = cmd[cmd.index("-f") + 1]
            Path(path).write_text("PRIVATE\n")
            Path(path + ".pub").write_text(PUB + "\n")
        bad = cmd[0] == "ssh" and "worker1" in cmd
        return subprocess.CompletedProcess(cmd, 255 if bad else 0, "", "timed out" if bad else "")
    monkeypatch.setattr(addons.Context, "run", run)
    m.reconcile("main", addons.entries(sem.addon)["main"], addons.Context(sem.addon), first=True)
    assert "machine:worker1" not in state()["keyed"]
    assert any("worker1" in k for k in state()["last"]["keys"])
    sem.ran.clear()
    addons.summarize(sem.addon)           # the next minute: not tried again yet
    assert not [c for c in sem.ran if c[0] == "ssh"]
    monkeypatch.setattr(m, "_now", lambda: 10 ** 10)
    addons.summarize(sem.addon)           # half an hour later: tried again
    assert [c[-2] for c in sem.ran if c[0] == "ssh"] == ["worker1"]


def test_owner_times_become_the_semaphores_zone():
    hj = mod_jobs()
    summer = datetime(2026, 7, 1, 12, tzinfo=ZoneInfo("America/Los_Angeles"))
    assert hj.cron_for((30, 23, 0), "America/Los_Angeles", "America/Los_Angeles") == "30 23 * * 0"
    assert hj.cron_for((30, 23, 0), "America/Los_Angeles", "UTC", summer) == "30 6 * * 1"   # Sunday PDT = Monday UTC
    assert hj.cron_for((40, None, None), "America/Los_Angeles", "UTC", summer) == "40 * * * *"


def test_a_single_machine_install_lists_just_this_machine():
    hj = mod_jobs()

    class F:
        machines, core_host, core_url, ssh = ("solo",), "solo", "http://127.0.0.1:8787", {}
    groups = hj.machines(F, {"tailnet": {"suffix": "example.ts.net"}}, {}, {}, me="owner")
    assert groups["hisho_core"] == {"solo": {"ansible_host": "solo.example.ts.net", "hisho_name": "solo",
                                             "ansible_user": "owner"}}
    assert list(groups["hisho_fleet"]) == ["solo"] and groups["hisho_servers"] == {} == groups["proxmox_hosts"]


def test_every_unit_a_job_runs_exists_in_hisho(sem):
    """Hisho's units live in its repo (deploy/systemd); a job naming one that isn't there would only ever fail."""
    import hisho
    units = Path(hisho.__file__).resolve().parents[2] / "deploy" / "systemd"
    if not units.is_dir():
        pytest.skip("not a Hisho checkout")
    hj = mod_jobs()
    for job in hj.jobs({"forgejo": True, "secrets_mirror": True, "secret_notes": True, "proxmox": ["pve-a"]}, PUB):
        unit = job["extra"].get("hisho_unit")
        if unit:
            name = re.sub(r"@.*", "@", unit) + ".service"
            assert (units / name).is_file(), name
            assert re.fullmatch(r"hisho-[a-z0-9-]+(@[a-z0-9-]+)?", unit)


def test_the_playbooks_stay_generic():
    text = "\n".join(p.read_text() for p in (HERE / "ansible").rglob("*.yml"))
    assert "hosts: kihon" not in text and "hisho_core" in text
    for p in (HERE / "ansible").rglob("*.yml"):
        assert yaml.safe_load(p.read_text()), p


def mod_jobs():
    import importlib.util
    spec = importlib.util.spec_from_file_location("hisho_jobs_t", HERE / "hisho_jobs.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m
