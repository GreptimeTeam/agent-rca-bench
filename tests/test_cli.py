import json

from semantic_rca_bench.cli import _batch_output, _parser, _run_orders
from semantic_rca_bench.contracts import Visibility


def test_run_orders_rotate_every_level_through_each_position() -> None:
    levels = list(Visibility)

    orders = _run_orders(levels, repetitions=3, seed=7)

    assert len(orders) == 3
    assert all(set(order) == set(levels) for order in orders)
    for position in range(3):
        assert {order[position] for order in orders} == set(levels)


def test_batch_output_uses_case_identity(tmp_path) -> None:
    source = tmp_path / "smoke.json"
    source.write_text(
        json.dumps(
            {
                "case": {"source_case": "re2ob_checkoutservice_cpu_1"},
                "ground_truth": {"component": "checkoutservice", "fault_type": "cpu"},
            }
        )
    )

    assert _batch_output(source, tmp_path) == tmp_path / "v11-re2ob-checkoutservice-cpu-1.json"


def test_case_role_defaults_to_development_and_accepts_measurement() -> None:
    default = _parser().parse_args(["run", "--report", "source.json"])
    measurement = _parser().parse_args(
        ["run", "--report", "source.json", "--case-role", "measurement"]
    )

    assert default.case_role == "development"
    assert measurement.case_role == "measurement"
