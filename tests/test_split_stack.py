import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import agent_rca_bench.split_stack as split_stack_module
from agent_rca_bench.split_stack import ManagedSplitStack, SplitStackError
from agent_rca_bench.transfer_protocol import SplitStackImages, load_transfer_protocol


def test_the_protocol_pins_every_split_stack_image_by_digest() -> None:
    protocol, _ = load_transfer_protocol()

    images = protocol.split_stack_images.model_dump()

    # The split arm's results are attributable only if the protocol records the
    # stores that produced them, exactly as it records greptimedb_revision.
    assert set(images) == {"prometheus", "loki", "tempo"}
    for name, image in images.items():
        assert "@sha256:" in image, name


def test_an_unpinned_image_is_rejected() -> None:
    images = SplitStackImages(
        prometheus="prom/prometheus:latest", loki="a@sha256:b", tempo="c@sha256:d"
    )

    with pytest.raises(ValueError, match="prometheus image is not pinned by digest"):
        images.require_digests()


def test_the_running_stack_uses_the_images_the_protocol_bound() -> None:
    protocol, _ = load_transfer_protocol()
    fixture = json.loads(Path("fixtures/reference/transfer-v34-protocol.json").read_text())

    # A digest changed in code but not in the fixture would otherwise run
    # unnoticed, because nothing else compares the two.
    assert fixture["split_stack_images"] == protocol.split_stack_images.model_dump()
    source = Path("src/agent_rca_bench/transfer_formal.py").read_text()
    assert "images=SplitStackImages(**protocol.split_stack_images.model_dump())" in source


def test_docker_assigns_the_loopback_host_port_atomically(tmp_path, monkeypatch) -> None:
    commands = []

    def run(command, **_kwargs):
        commands.append(command)
        if command[1] == "port":
            return SimpleNamespace(returncode=0, stdout="127.0.0.1:49178\n", stderr="")
        return SimpleNamespace(returncode=0, stdout="container-id\n", stderr="")

    monkeypatch.setattr(split_stack_module.subprocess, "run", run)
    stack = ManagedSplitStack(tmp_path / "split")

    stack._start_loki()

    docker_run = commands[0]
    publish_index = docker_run.index("--publish")
    assert docker_run[publish_index + 1] == "127.0.0.1::3100"
    assert stack.loki_endpoint == "http://127.0.0.1:49178"


def test_failed_docker_run_removes_its_created_container(tmp_path, monkeypatch) -> None:
    commands = []

    def run(command, **_kwargs):
        commands.append(command)
        if command[1] == "run":
            return SimpleNamespace(returncode=125, stdout="", stderr="port unavailable")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(split_stack_module.subprocess, "run", run)
    stack = ManagedSplitStack(tmp_path / "split")

    with pytest.raises(SplitStackError, match="port unavailable"):
        stack._start_loki()

    assert commands[-1] == ["docker", "rm", "--force", stack._names["loki"]]


def test_loki_disables_automatic_stream_sharding(tmp_path) -> None:
    stack = ManagedSplitStack(tmp_path / "split")

    stack._write_configs()

    config = (stack.run_dir / "loki" / "loki.yml").read_text()
    assert "shard_streams:\n    enabled: false" in config


def test_tempo_retains_the_historical_timestamps_it_accepts(tmp_path) -> None:
    stack = ManagedSplitStack(tmp_path / "split")
    stack._write_configs()

    config = (stack.run_dir / "tempo" / "tempo.yml").read_text()
    assert "ingestion_time_range_slack: 87600h" in config
    assert "overrides:\n  defaults:\n    compaction:" in config
    assert "block_retention: 87600h" in config
