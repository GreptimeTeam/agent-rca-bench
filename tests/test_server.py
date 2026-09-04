import json

import pytest

import agent_rca_bench.greptimedb.server as server_module
from agent_rca_bench.greptimedb.server import inspect_checkout, write_json


def test_inspect_checkout_selects_bound_release_binary(tmp_path, monkeypatch) -> None:
    (tmp_path / "Cargo.toml").write_text("[workspace]\n")
    binary = tmp_path / "target" / "release" / "greptime"
    binary.parent.mkdir(parents=True)
    binary.write_text("#!/bin/sh\necho release-binary\n")
    binary.chmod(0o755)

    def fake_run(repo, *args):
        values = {
            ("git", "status", "--porcelain", "--untracked-files=no"): "",
            ("git", "branch", "--show-current"): "semantic-graph",
            ("git", "rev-parse", "HEAD"): "revision",
        }
        return values[args]

    monkeypatch.setattr(server_module, "_run", fake_run)

    metadata = inspect_checkout(tmp_path, build_profile="release")

    assert metadata["build_profile"] == "release"
    assert metadata["binary"] == str(binary)
    assert metadata["binary_version"] == "release-binary"


def test_write_json_does_not_replace_report_with_unserializable_value(tmp_path) -> None:
    report = tmp_path / "report.json"
    write_json(report, {"complete": False})

    with pytest.raises(TypeError):
        write_json(report, {"invalid": object()})

    assert json.loads(report.read_text()) == {"complete": False}
    assert list(tmp_path.iterdir()) == [report]
