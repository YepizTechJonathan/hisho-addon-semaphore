"""Install in Hisho: Semaphore's companion block is valid and runs Hisho's own Semaphore install."""
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parents[1]


def test_the_companion_block_is_valid_and_never_installs_over_an_existing_semaphore():
    from hisho import addons, companion
    m = yaml.safe_load((HERE / "addon.yaml").read_text())
    assert addons.validate_manifest(m) == []
    c = m["companion"]
    assert c["detect"]["containers"] == ["semaphore"] and c["detect"]["entry"] == "url"
    assert companion.problems(addons.Addon("semaphore", HERE, m)) == []
    assert "deploy/semaphore/install.sh" in (HERE / c["install"]).read_text()
