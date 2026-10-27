"""Semaphore add-on tests. Run with Hisho's venv at the repository's top:
~/code/hisho/.venv/bin/python -m pytest -q"""
import json
import subprocess
from pathlib import Path

import pytest
import yaml

from hisho import addon_store, addons, fleet_power, maintenance  # noqa: F401
from hisho.addon_testing import AUTH, TOKEN, Recorder

HERE = Path(__file__).resolve().parents[1]


def ctx_for(addon):
    return addons.Context(addon, resolve=lambda name: f"value-of-{name}")


def test_manifest_is_valid_and_readme_matches(addon):
    assert addons.validate_manifest(addon.manifest) == []
    assert addon_store.readme_problems(HERE) == []


def test_the_semaphore_integration_lists_jobs_flags_failures_and_runs_one(fleet, monkeypatch):
    import io, json as _json
    state = {"inventories": [{"id": 1, "name": "Dev Inventory", "ssh_key_id": 3, "become_key_id": None}]}
    (fleet / "System/Config/Inventory.yaml").write_text(yaml.safe_dump({
        "vars": {"ansible_port": 22},
        "environments": {"dev": {"groups": {"nas_hosts": {"truenas-01": {"ansible_host": "192.0.2.21"}},
                                            "dev_servers": {"hermes-dev-01": {"ansible_host": "198.51.100.7"}}}}},
        "retired": ["hermes-dev-01"]}))
    sem = addons.get("semaphore")
    addons.set_entry(sem, "home", {"url": "198.51.100.5", "token": "HISHO_SEM_TOKEN"})
    calls = []

    class Resp(io.BytesIO):
        def __enter__(self): return self
        def __exit__(self, *a): return False

    def urlopen(req, timeout=0):
        path = req.full_url.split("/api", 1)[1]
        calls.append((req.get_method(), path, _json.loads(req.data) if req.data else None))
        assert req.full_url.startswith("http://198.51.100.5:3000/api") and req.headers["Authorization"] == "Bearer tok"
        data = {"/projects": [{"id": 1, "name": "infra"}],
                "/project/1/templates": [{"id": 4, "name": "Update Pangolin", "playbook": "pangolin-update.yml"},
                                         {"id": 5, "name": "Patch offsite", "playbook": "patch.yml"}],
                "/project/1/tasks/last": [{"id": 90, "template_id": 5, "status": "error", "end": "2026-10-18T10:00"},
                                          {"id": 80, "template_id": 5, "status": "success"}],
                "/project/1/tasks": {"id": 91},
                "/project/1/environment": [{"id": 1, "name": "Default", "project_id": 1, "json": "{}", "env": "{}"}],
                "/project/1/environment/1": None,
                "/project/1/inventory": state["inventories"],
                "/project/1/inventory/7": None,
                "/project/1/templates/4": None, "/project/1/templates/5": None}.get(path) if path != "/project/1/inventory" or req.get_method() == "GET" else {"id": 7}
        return Resp(_json.dumps(data).encode())
    monkeypatch.setattr("urllib.request.urlopen", urlopen)
    ctx = addons.Context(sem, resolve=lambda n: "tok")
    maintenance.turn_on(fleet, "amp-game-01")
    s = addons.summarize(sem, ctx)
    assert s["headline"] == "2 jobs · 1 failed last time" and s["problems"][0]["title"] == "'Patch offsite' failed" and s["problems"][0]["quiet"]
    put = next(c for c in calls if c[0] == "PUT")
    assert put[1] == "/project/1/environment/1"
    assert json.loads(put[2]["json"]) == {"hisho_maintenance": ["amp-game-01"], "hisho_maintenance_all": False}
    n_put = sum(1 for c in calls if c[0] == "PUT")
    addons.summarize(sem, ctx)
    assert sum(1 for c in calls if c[0] == "PUT") == n_put + 1  # the stub keeps answering "{}"
    [sec] = addons.details(sem, ctx)
    assert [(i["name"], i["state"]) for i in sec["items"]] == [("Machine list: Semaphore's own", "off"),
                                                                ("Patch offsite", "down"), ("Update Pangolin", "off")]
    # use Hisho's machine list: creates "Hisho dev" with the old inventory's SSH key, moves the jobs
    res = addons.act(sem, "home", "1-inventory", "use-hisho-inventory", ctx)
    assert res["ok"], res
    post = next(c for c in calls if c[0] == "POST" and c[1] == "/project/1/inventory")
    assert post[2]["name"] == "Hisho dev" and post[2]["ssh_key_id"] == 3
    assert "truenas-01" in post[2]["inventory"] and "hermes-dev-01" not in post[2]["inventory"]
    res = addons.act(sem, "home", "1-4", "run", ctx)
    assert res["ok"] and calls[-1] == ("POST", "/project/1/tasks", {"template_id": 4})
    assert addons.act(sem, "home", "1-x", "run", ctx)["ok"] is False


def test_jobs_listed_in_the_vault_are_added_once_and_kept_in_step(fleet, monkeypatch):
    import io, json as _json
    sem = addons.get("semaphore")
    addons.set_entry(sem, "home", {"url": "198.51.100.5", "token": "HISHO_SEM_TOKEN"})
    addons.config_path("semaphore-templates").write_text(yaml.safe_dump({"templates": [{
        "project": "infra", "name": "Forgejo Upgrade - Manual Guarded", "like": "Update Pangolin",
        "playbook": "ansible/playbooks/forgejo-upgrade.yml", "inventory": "Hisho dev",
        "description": "Upgrades Forgejo", "extra_vars": {"confirm_forgejo_upgrade": True}}]}))
    templates = [{"id": 4, "name": "Update Pangolin", "playbook": "pangolin-update.yml", "repository_id": 2,
                  "environment_id": 3, "app": "ansible", "inventory_id": 8,
                  "arguments": '["-e", "confirm_pangolin_update=true"]', "survey_vars": [{"name": "x"}]}]
    calls = []

    class Resp(io.BytesIO):
        def __enter__(self): return self
        def __exit__(self, *a): return False

    def urlopen(req, timeout=0):
        path, method = req.full_url.split("/api", 1)[1], req.get_method()
        body = _json.loads(req.data) if req.data else None
        calls.append((method, path, body))
        if method == "POST" and path == "/project/1/templates":
            templates.append({**body, "id": 9})
        if method == "PUT" and path == "/project/1/templates/9":
            templates[-1] = body
        data = {"/projects": [{"id": 1, "name": "infra"}], "/project/1/templates": templates,
                "/project/1/inventory": [{"id": 7, "name": "Hisho dev"}, {"id": 8, "name": "Hisho prod"}]}.get(path)
        return Resp(_json.dumps(data if method == "GET" else None).encode())
    monkeypatch.setattr("urllib.request.urlopen", urlopen)
    ctx = addons.Context(sem, resolve=lambda n: "tok")
    entry = addons.entries(sem)["home"]
    import importlib.util
    spec = importlib.util.spec_from_file_location("sem_provider", HERE / "provider.py")
    provider = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(provider)

    assert provider.sync_templates("home", entry, ctx) == ["added 'Forgejo Upgrade - Manual Guarded' to infra"]
    [post] = [c for c in calls if c[0] == "POST"]
    assert post[2]["playbook"] == "ansible/playbooks/forgejo-upgrade.yml" and post[2]["inventory_id"] == 7
    assert post[2]["repository_id"] == 2 and post[2]["environment_id"] == 3 and post[2]["app"] == "ansible"
    assert _json.loads(post[2]["arguments"]) == ["--extra-vars", '{"confirm_forgejo_upgrade": true}']
    assert post[2]["survey_vars"] == [] and post[2]["autorun"] is False   # nothing copied that could confirm Pangolin
    assert not any(c[1].endswith("/tasks") for c in calls)                 # never runs a job
    assert provider.sync_templates("home", entry, ctx) == []               # in step: nothing to do
    templates[-1]["playbook"] = "changed-by-hand.yml"
    assert provider.sync_templates("home", entry, ctx) == ["updated 'Forgejo Upgrade - Manual Guarded' in infra"]
    assert templates[-1]["playbook"] == "ansible/playbooks/forgejo-upgrade.yml"
    assert not any(c[0] == "DELETE" for c in calls)
    # a bad list shows up as a problem on the card instead of breaking it
    addons.config_path("semaphore-templates").write_text(yaml.safe_dump({"templates": [{
        "project": "infra", "name": "Broken", "like": "No such job", "playbook": "x.yml", "inventory": "Hisho dev"}]}))
    with pytest.raises(RuntimeError, match="No such job"):
        provider.sync_templates("home", entry, ctx)


def _provider():
    import importlib.util
    spec = importlib.util.spec_from_file_location("sem_provider_03", HERE / "provider.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_cron_lines_give_the_last_time_they_should_have_fired():
    from datetime import datetime
    from zoneinfo import ZoneInfo
    p, la = _provider(), ZoneInfo("America/Los_Angeles")
    at = datetime(2026, 10, 24, 21, 5, tzinfo=la)                        # a Saturday
    assert p.last_fire("20 20 * * *", at) == datetime(2026, 10, 24, 20, 20, tzinfo=la)
    assert p.last_fire("15 5,11,17,23 * * *", at) == datetime(2026, 10, 24, 17, 15, tzinfo=la)
    assert p.last_fire("0 23 * * 0", at) == datetime(2026, 10, 18, 23, 0, tzinfo=la)   # last Sunday
    assert p.last_fire("30 20 * * 6", at) == datetime(2026, 10, 24, 20, 30, tzinfo=la)
    assert p.last_fire("*/10 * * * *", at).minute == 0
    assert p.once_a_day_or_less("40 20 * * *") and not p.once_a_day_or_less("0 * * * *")
    with pytest.raises(ValueError):
        p.parse_cron("61 * * * *")
    with pytest.raises(ValueError):
        p.parse_cron("0 20 * *")


def _project(templates, tasks, schedules):
    runs = {}
    for t in sorted(tasks, key=lambda t: -t["id"]):
        runs.setdefault(t["template_id"], []).append(t)
    return {"id": 1, "name": "infra", "templates": templates, "schedules": schedules, "runs": runs,
            "last": {k: v[0] for k, v in runs.items()}}


def test_failed_and_missed_runs_become_items_and_only_pressing_ones_push(monkeypatch):
    from datetime import datetime, timezone
    p = _provider()
    entry = {"url": "https://kihon.example:4443", "timezone": "UTC"}
    now = datetime(2026, 10, 24, 4, 0, tzinfo=timezone.utc).timestamp()
    monkeypatch.setattr(p, "_now", lambda: now - 3 * 86400)   # this Core has watched for 3 days
    tpls = [{"id": 10, "name": "TrueNAS Control Plane Backup"}, {"id": 12, "name": "Docker Unhealthy Containers Report"},
            {"id": 29, "name": "Pangolin Stack Update - Manual Guarded"}, {"id": 13, "name": "Public URL Health Check"}]
    scheds = [{"id": 1, "template_id": 10, "cron_format": "0 3 * * *", "active": True, "name": "Daily backup"},
              {"id": 3, "template_id": 12, "cron_format": "0 * * * *", "active": True, "name": "Hourly"},
              {"id": 4, "template_id": 13, "cron_format": "15 */6 * * *", "active": True, "name": "6h"}]
    tasks = [{"id": 5, "template_id": 10, "status": "error", "created": "2026-10-24T03:00:02Z", "end": "2026-10-24T03:01:00Z"},
             {"id": 6, "template_id": 12, "status": "error", "created": "2026-10-24T03:00:01Z"},
             {"id": 7, "template_id": 29, "status": "failed", "created": "2026-10-23T10:00:00Z"},
             {"id": 8, "template_id": 13, "status": "success", "created": "2026-10-23T12:15:00Z"}]
    p.schedule_problems("Kihon", entry, _project(tpls, tasks, scheds))      # first look: starts the watch
    monkeypatch.setattr(p, "_now", lambda: now)
    got = {i["key"]: i for i in p.schedule_problems("Kihon", entry, _project(tpls, tasks, scheds))}
    assert got["failed-1-10"]["quiet"] is False                             # nightly backup failed: phone
    assert got["failed-1-10"]["url"] == "https://kihon.example:4443/project/1/templates/10?t=5"
    assert got["failed-1-12"]["quiet"] is True                              # hourly, failed once: quietly
    assert got["failed-1-29"]["quiet"] is True and "Retry" not in got["failed-1-29"]["detail"]  # guarded, never retried
    assert got["missed-1-13"]["title"] == "'Public URL Health Check' missed its 12:15 am run"   # due 00:15, last ran 12:15 pm
    tasks.append({"id": 9, "template_id": 12, "status": "error", "created": "2026-10-24T03:45:00Z"})
    got = {i["key"]: i for i in p.schedule_problems("Kihon", entry, _project(tpls, tasks, scheds))}
    assert got["failed-1-12"]["quiet"] is False and "missed-1-12" not in got   # twice in a row: phone
    tasks.append({"id": 11, "template_id": 12, "status": "success", "created": "2026-10-24T03:50:00Z"})
    tasks.append({"id": 12, "template_id": 10, "status": "success", "created": "2026-10-24T03:55:00Z"})
    got = {i["key"] for i in p.schedule_problems("Kihon", entry, _project(tpls, tasks, scheds))}
    assert "failed-1-12" not in got and "failed-1-10" not in got             # the next good run clears it


def test_a_new_schedule_is_not_called_missed_before_it_was_due_once(monkeypatch):
    from datetime import datetime, timezone
    p = _provider()
    now = datetime(2026, 10, 24, 4, 0, tzinfo=timezone.utc).timestamp()
    monkeypatch.setattr(p, "_now", lambda: now)
    proj = _project([{"id": 10, "name": "Backup"}], [], [{"id": 1, "template_id": 10, "cron_format": "0 3 * * *", "active": True}])
    assert p.schedule_problems("new", {"url": "x", "timezone": "UTC"}, proj) == []
    proj["schedules"][0]["active"] = False
    monkeypatch.setattr(p, "_now", lambda: now + 2 * 86400)
    assert p.schedule_problems("new", {"url": "x", "timezone": "UTC"}, proj) == []   # paused: not watched


def test_semaphore_down_is_one_quiet_item_after_five_minutes(fleet, monkeypatch):
    import urllib.error
    p = _provider()
    clock = [1000.0]
    monkeypatch.setattr(p, "_now", lambda: clock[0])
    monkeypatch.setattr(p, "_watch_config", lambda: {})

    def urlopen(req, timeout=0):
        raise urllib.error.URLError("refused")
    monkeypatch.setattr("urllib.request.urlopen", urlopen)
    ctx = addons.Context(addons.get("semaphore"), resolve=lambda n: "tok")
    entries = {"Kihon": {"url": "198.51.100.5", "token": "HISHO_SEM_TOKEN"}}
    assert p.summary(entries, ctx)["problems"] == []
    clock[0] += 301
    [item] = p.summary(entries, ctx)["problems"]
    assert item["title"] == "Semaphore Kihon is not answering" and item["quiet"] and item["key"] == "down"


def test_listed_jobs_get_their_schedule_and_a_paused_one_stays_paused(fleet, monkeypatch):
    import io, json as _json
    p = _provider()
    templates = [{"id": 4, "name": "Like"}, {"id": 9, "name": "Add-on mirror", "inventory_id": 7, "arguments": "[]",
                                                "playbook": "ansible/playbooks/hisho/addon-mirror.yml", "description": "Added by Hisho."}]
    schedules, calls = [], []

    class Resp(io.BytesIO):
        def __enter__(self): return self
        def __exit__(self, *a): return False

    def urlopen(req, timeout=0):
        path, method = req.full_url.split("/api", 1)[1], req.get_method()
        body = _json.loads(req.data) if req.data else None
        calls.append((method, path, body))
        if method == "POST" and path == "/project/1/schedules":
            schedules.append({**body, "id": 3})
        if method == "PUT" and path == "/project/1/schedules/3":
            schedules[0] = body
        data = {"/projects": [{"id": 1, "name": "infra"}], "/project/1/templates": templates,
                "/project/1/schedules": schedules, "/project/1/inventory": [{"id": 7, "name": "Hisho dev"}]}.get(path)
        return Resp(_json.dumps(data if method == "GET" else None).encode())
    monkeypatch.setattr("urllib.request.urlopen", urlopen)
    ctx = addons.Context(addons.get("semaphore"), resolve=lambda n: "tok")
    spec = {"project": "infra", "name": "Add-on mirror", "like": "Like", "playbook": "ansible/playbooks/hisho/addon-mirror.yml",
            "inventory": "Hisho dev", "description": "Added by Hisho.", "schedule": "5 * * * *"}
    entry = {"url": "198.51.100.5", "token": "HISHO_SEM_TOKEN"}
    assert "scheduled 'Add-on mirror' at 5 * * * *" in p.sync_templates("Kihon", entry, ctx, [spec])
    assert schedules[0]["template_id"] == 9 and schedules[0]["active"] is True
    schedules[0]["active"] = False                                     # Alex paused it in Semaphore
    assert p.sync_templates("Kihon", entry, ctx, [spec]) == []
    spec["schedule"] = "10 * * * *"
    assert p.sync_templates("Kihon", entry, ctx, [spec]) == ["moved 'Add-on mirror' to 10 * * * *"]
    assert schedules[0]["cron_format"] == "10 * * * *" and schedules[0]["active"] is False   # still paused
    with pytest.raises(ValueError):
        p.sync_templates("Kihon", entry, ctx, [{**spec, "schedule": "every night"}])


def test_watched_loops_are_quiet_until_fifteen_minutes_without_success(monkeypatch):
    import subprocess as sp
    p = _provider()
    clock = [10_000.0]
    monkeypatch.setattr(p, "_now", lambda: clock[0])
    state = {"Result": "success", "ExecMainExitTimestamp": "@9990"}

    class Ctx:
        def run(self, cmd, timeout=30, env=None):
            assert cmd[:3] == ["systemctl", "--user", "show"] and env["XDG_RUNTIME_DIR"]
            out = "LoadState=loaded\n" + "".join(f"{k}={v}\n" for k, v in state.items())
            return sp.CompletedProcess(cmd, 0, out, "")
    assert p.loop_problems(["hisho-idle-sweep"], Ctx()) == []
    state["Result"] = "exit-code"
    clock[0] += 14 * 60
    assert p.loop_problems(["hisho-idle-sweep"], Ctx()) == []
    clock[0] += 2 * 60
    [item] = p.loop_problems(["hisho-idle-sweep"], Ctx())
    assert item["title"] == "hisho-idle-sweep has not run successfully for 16 min" and item["quiet"]
    state.update(Result="success", ExecMainExitTimestamp=f"@{clock[0]}")
    assert p.loop_problems(["hisho-idle-sweep"], Ctx()) == []


def test_retry_runs_a_failed_job_but_never_a_guarded_one(fleet, monkeypatch):
    import io, json as _json
    sem = addons.get("semaphore")
    addons.set_entry(sem, "home", {"url": "198.51.100.5", "token": "HISHO_SEM_TOKEN"})
    calls = []

    class Resp(io.BytesIO):
        def __enter__(self): return self
        def __exit__(self, *a): return False

    def urlopen(req, timeout=0):
        path, method = req.full_url.split("/api", 1)[1], req.get_method()
        calls.append((method, path))
        data = {"/project/1/templates/5": {"id": 5, "name": "Patch offsite"},
                "/project/1/templates/6": {"id": 6, "name": "Pangolin Stack Update - Manual Guarded"},
                "/project/1/tasks": {"id": 92}}.get(path)
        return Resp(_json.dumps(data).encode())
    monkeypatch.setattr("urllib.request.urlopen", urlopen)
    ctx = addons.Context(sem, resolve=lambda n: "tok")
    assert addons.act(sem, "home", "1-5", "retry", ctx)["ok"] and calls[-1] == ("POST", "/project/1/tasks")
    res = addons.act(sem, "home", "1-6", "retry", ctx)
    assert res["ok"] is False and "guarded" in res["error"] and calls[-1] == ("GET", "/project/1/templates/6")
