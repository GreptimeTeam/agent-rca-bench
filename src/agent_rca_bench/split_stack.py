from __future__ import annotations

import subprocess
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

import httpx

PROMETHEUS_IMAGE = (
    "prom/prometheus@sha256:5ce7540c3c00ef4ab0c9d2c995c6a5b9c421f44b4a115d97a2c7af3b1c21cbb0"
)
LOKI_IMAGE = "grafana/loki@sha256:87f0a067673756a3cede1bcbf0c74875f7df9b09fddb53e399d0c576f756cfcc"
TEMPO_IMAGE = (
    "grafana/tempo@sha256:cda87c212d8c584dc0b89e337e7ed648a5100feb657e5d528480ee4fa03dbbe3"
)


class SplitStackError(RuntimeError):
    pass


@dataclass(frozen=True)
class SplitStackImages:
    """The images to run. A formal case takes these from the bound protocol."""

    prometheus: str = PROMETHEUS_IMAGE
    loki: str = LOKI_IMAGE
    tempo: str = TEMPO_IMAGE


class ManagedSplitStack:
    def __init__(
        self,
        run_dir: Path,
        *,
        images: SplitStackImages | None = None,
        startup_timeout: float = 120.0,
    ) -> None:
        if run_dir.exists():
            raise SplitStackError(f"exclusive split-stack directory already exists: {run_dir}")
        images = images or SplitStackImages()
        self.run_dir = run_dir
        self.images = images
        self.startup_timeout = startup_timeout
        self.prometheus_port: int | None = None
        self.loki_port: int | None = None
        self.tempo_http_port: int | None = None
        self.tempo_otlp_http_port: int | None = None
        suffix = uuid.uuid4().hex[:12]
        self._names = {
            "prometheus": f"observability-rca-prometheus-{suffix}",
            "loki": f"observability-rca-loki-{suffix}",
            "tempo": f"observability-rca-tempo-{suffix}",
        }
        self._started: list[str] = []

    @property
    def prometheus_endpoint(self) -> str:
        return f"http://127.0.0.1:{self._required_port('prometheus')}"

    @property
    def loki_endpoint(self) -> str:
        return f"http://127.0.0.1:{self._required_port('loki')}"

    @property
    def tempo_endpoint(self) -> str:
        return f"http://127.0.0.1:{self._required_port('tempo_http')}"

    @property
    def tempo_otlp_endpoint(self) -> str:
        return f"http://127.0.0.1:{self._required_port('tempo_otlp_http')}"

    def start(self) -> None:
        self.run_dir.mkdir(parents=True)
        self._write_configs()
        try:
            self._start_prometheus()
            self._start_loki()
            self._start_tempo()
            self._wait_ready("prometheus", f"{self.prometheus_endpoint}/-/ready")
            self._wait_ready("loki", f"{self.loki_endpoint}/ready")
            self._wait_ready("tempo", f"{self.tempo_endpoint}/ready")
        except Exception:
            self.stop()
            raise

    def stop(self) -> None:
        for service in reversed(self._started):
            logs = subprocess.run(
                ["docker", "logs", self._names[service]],
                check=False,
                capture_output=True,
                text=True,
            )
            (self.run_dir / service / "container.log").write_text(logs.stdout + logs.stderr)
            subprocess.run(
                ["docker", "rm", "--force", self._names[service]],
                check=False,
                capture_output=True,
            )
        self._started.clear()

    def __enter__(self) -> ManagedSplitStack:
        self.start()
        return self

    def __exit__(self, *_: object) -> None:
        self.stop()

    def metadata(self) -> dict[str, object]:
        return {
            "loopback_only": True,
            "images": {
                "prometheus": self.images.prometheus,
                "loki": self.images.loki,
                "tempo": self.images.tempo,
            },
            "ports": {
                "prometheus": self._required_port("prometheus"),
                "loki": self._required_port("loki"),
                "tempo_http": self._required_port("tempo_http"),
                "tempo_otlp_http": self._required_port("tempo_otlp_http"),
            },
        }

    def _required_port(self, service: str) -> int:
        port = getattr(self, f"{service}_port")
        if not isinstance(port, int):
            raise SplitStackError(f"{service} has no published port")
        return port

    def _start_prometheus(self) -> None:
        data = self.run_dir / "prometheus" / "data"
        data.mkdir(parents=True)
        data.chmod(0o777)
        published = self._docker_run(
            "prometheus",
            self.images.prometheus,
            container_ports=(9090,),
            volumes=(
                (
                    self.run_dir / "prometheus" / "prometheus.yml",
                    "/etc/prometheus/prometheus.yml",
                    True,
                ),
                (data, "/prometheus", False),
            ),
            args=(
                "--config.file=/etc/prometheus/prometheus.yml",
                "--storage.tsdb.path=/prometheus",
                "--storage.tsdb.retention.time=10y",
                "--web.enable-otlp-receiver",
                "--web.enable-remote-write-receiver",
                "--web.listen-address=0.0.0.0:9090",
            ),
        )
        self.prometheus_port = published[9090]

    def _start_loki(self) -> None:
        data = self.run_dir / "loki" / "data"
        data.mkdir(parents=True)
        data.chmod(0o777)
        published = self._docker_run(
            "loki",
            self.images.loki,
            container_ports=(3100,),
            volumes=(
                (self.run_dir / "loki" / "loki.yml", "/etc/loki/local-config.yaml", True),
                (data, "/loki", False),
            ),
            args=("-config.file=/etc/loki/local-config.yaml",),
        )
        self.loki_port = published[3100]

    def _start_tempo(self) -> None:
        data = self.run_dir / "tempo" / "data"
        data.mkdir(parents=True)
        data.chmod(0o777)
        published = self._docker_run(
            "tempo",
            self.images.tempo,
            container_ports=(3200, 4318),
            volumes=(
                (self.run_dir / "tempo" / "tempo.yml", "/etc/tempo/tempo.yml", True),
                (data, "/var/tempo", False),
            ),
            args=("-config.file=/etc/tempo/tempo.yml",),
        )
        self.tempo_http_port = published[3200]
        self.tempo_otlp_http_port = published[4318]

    def _docker_run(
        self,
        service: str,
        image: str,
        *,
        container_ports: tuple[int, ...],
        volumes: tuple[tuple[Path, str, bool], ...],
        args: tuple[str, ...],
    ) -> dict[int, int]:
        # A whole case arrives as one burst, so Tempo cuts many small blocks and
        # a trace-by-id lookup opens a bloom filter per block. The default 1024
        # descriptors are not enough for that, and the shortfall surfaces as an
        # opaque 500 from the store rather than as a benchmark error.
        command = [
            "docker",
            "run",
            "--detach",
            "--name",
            self._names[service],
            "--ulimit",
            "nofile=65536:65536",
        ]
        for container in container_ports:
            command.extend(("--publish", f"127.0.0.1::{container}"))
        for host, container, read_only in volumes:
            mount = f"{host.resolve()}:{container}"
            if read_only:
                mount += ":ro"
            command.extend(("--volume", mount))
        command.extend((image, *args))
        result = subprocess.run(command, check=False, capture_output=True, text=True)
        if result.returncode != 0:
            subprocess.run(
                ["docker", "rm", "--force", self._names[service]],
                check=False,
                capture_output=True,
            )
            raise SplitStackError(
                f"failed to start {service}: {(result.stderr or result.stdout).strip()}"
            )
        self._started.append(service)
        published = {}
        for container in container_ports:
            result = subprocess.run(
                ["docker", "port", self._names[service], f"{container}/tcp"],
                check=False,
                capture_output=True,
                text=True,
            )
            address = result.stdout.strip()
            if result.returncode != 0 or not address:
                raise SplitStackError(
                    f"failed to inspect {service} port {container}: "
                    f"{(result.stderr or result.stdout).strip()}"
                )
            try:
                published[container] = int(address.rsplit(":", 1)[1])
            except (IndexError, ValueError) as error:
                raise SplitStackError(
                    f"invalid published port for {service} port {container}: {address}"
                ) from error
        return published

    def _wait_ready(self, service: str, url: str) -> None:
        deadline = time.monotonic() + self.startup_timeout
        while time.monotonic() < deadline:
            state = subprocess.run(
                [
                    "docker",
                    "inspect",
                    "--format",
                    "{{.State.Running}}",
                    self._names[service],
                ],
                check=False,
                capture_output=True,
                text=True,
            )
            if state.returncode != 0 or state.stdout.strip() != "true":
                logs = subprocess.run(
                    ["docker", "logs", self._names[service]],
                    check=False,
                    capture_output=True,
                    text=True,
                )
                raise SplitStackError(
                    f"{service} exited before readiness: {(logs.stderr or logs.stdout)[-4000:]}"
                )
            try:
                response = httpx.get(url, timeout=1.0, trust_env=False)
                if response.is_success:
                    return
            except httpx.HTTPError:
                pass
            time.sleep(0.25)
        logs = subprocess.run(
            ["docker", "logs", self._names[service]],
            check=False,
            capture_output=True,
            text=True,
        )
        raise SplitStackError(
            f"{service} did not become ready: {(logs.stderr or logs.stdout)[-4000:]}"
        )

    def _write_configs(self) -> None:
        prometheus = self.run_dir / "prometheus"
        loki = self.run_dir / "loki"
        tempo = self.run_dir / "tempo"
        for directory in (prometheus, loki, tempo):
            directory.mkdir(parents=True, exist_ok=True)
        (prometheus / "prometheus.yml").write_text(
            """global:
  scrape_interval: 1h
otlp:
  promote_all_resource_attributes: true
scrape_configs: []
"""
        )
        (loki / "loki.yml").write_text(
            """auth_enabled: false
server:
  http_listen_port: 3100
  # The storage audit reads a whole case back in one query, which is far larger
  # than the 4 MiB default gRPC frame between querier and frontend. The agent
  # never issues a query this size; its results are capped at max_items.
  grpc_server_max_recv_msg_size: 209715200
  grpc_server_max_send_msg_size: 209715200
common:
  path_prefix: /loki
  replication_factor: 1
  ring:
    kvstore:
      store: inmemory
  storage:
    filesystem:
      chunks_directory: /loki/chunks
      rules_directory: /loki/rules
schema_config:
  configs:
    - from: 2020-01-01
      store: tsdb
      object_store: filesystem
      schema: v13
      index:
        prefix: index_
        period: 24h
ingester:
  flush_check_period: 1s
  chunk_idle_period: 1s
  # Loki accepts out-of-order entries within max_chunk_age/2. The source logs
  # are not strictly ordered inside a stream, and a short chunk age silently
  # drops the late ones: the push still answers 204. Visibility comes from
  # chunk_idle_period instead, which flushes a stream a second after its last
  # write.
  max_chunk_age: 2h
  chunk_retain_period: 0s
  wal:
    dir: /loki/wal
    disk_full_threshold: 0
limits_config:
  # Automatic stream sharding is reactive and exposes `__stream_shard__` to
  # queries. The benchmark requires the agent-visible labels to come from the
  # source rather than Loki's ingestion timing.
  shard_streams:
    enabled: false
  ingestion_rate_strategy: local
  ingestion_rate_mb: 1000
  ingestion_burst_size_mb: 1000
  max_global_streams_per_user: 0
  max_streams_per_user: 0
  max_entries_limit_per_query: 1000000
  reject_old_samples: false
  allow_structured_metadata: true
  query_timeout: 10m
  # Loki otherwise invents `service_name: unknown_service` for a record that has
  # none, and derives `detected_level` from the line. The benchmark measures
  # whether a model can establish identity from telemetry, so the store must
  # hold the source's fields and no fabricated ones.
  discover_service_name: []
  discover_log_levels: false
analytics:
  reporting_enabled: false
"""
        )
        (tempo / "tempo.yml").write_text(
            """server:
  http_listen_port: 3200
distributor:
  receivers:
    otlp:
      protocols:
        http:
          endpoint: 0.0.0.0:4318
live_store:
  flush_check_period: 1s
  max_block_duration: 2s
  complete_block_timeout: 2s
  wal:
    ingestion_time_range_slack: 87600h
query_frontend:
  query_end_cutoff: 1s
  search:
    query_backend_after: 5s
storage:
  trace:
    backend: local
    blocklist_poll: 1s
    wal:
      path: /var/tempo/wal
    local:
      path: /var/tempo/blocks
usage_report:
  reporting_enabled: false
"""
        )
