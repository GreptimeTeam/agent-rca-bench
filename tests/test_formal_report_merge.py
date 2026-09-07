import copy
import json
from collections import Counter
from pathlib import Path

import pytest

from agent_rca_bench.formal_report import validate_formal_measurement_report
from agent_rca_bench.formal_report_merge import merge_formal_reports
from agent_rca_bench.formal_report_view import (
    build_report_view_model,
    render_formal_measurement_report,
)

ARTIFACTS = Path(__file__).resolve().parents[1] / "artifacts/measurement"
REPORTS = [
    ARTIFACTS / f"{stem}.json" for stem in ("agent-rca-v34", "agent-rca-v34-two-model-extension")
]
TRANSFERS = [path.with_name(f"{path.stem}-transfer.json") for path in REPORTS]
PUBLICATION = ARTIFACTS / "agent-rca-v34-six-model-publication.json"


@pytest.fixture(scope="module")
def merged_report():
    return merge_formal_reports(REPORTS, TRANSFERS, PUBLICATION)


def test_merge_preserves_models_and_independent_correction_scopes(merged_report):
    for path in REPORTS:
        source = json.loads(path.read_text())
        for model, values in source["model_reports"].items():
            assert merged_report["model_reports"][model] == values
    assert merged_report["execution"]["completed_cells"] == 696
    view = build_report_view_model(merged_report)
    storage = next(item for item in view["verdicts"] if item["goal"] == "storage_shape")
    assert storage["endpoints"]["family_size"] is None
    assert storage["endpoints"]["significant"] == 2
    assert "2 of 12" in storage["tally_text"]["en"]
    assert "m = 8, m = 4" in storage["tally_text"]["en"]
    assert storage["endpoints"]["family_sizes"] == [8, 4]
    assert [group["family_size"] for group in view["inference_groups"]] == [8, 4]
    assert [group["protocol_revision"] for group in view["inference_groups"]] == [
        group["protocol_revision"] for group in merged_report["inference_cohorts"]
    ]
    assert (
        len([text for text in merged_report["limitations"] if "endpoint eligibility" in text]) == 1
    )
    assert any("3-13 cases" in text for text in merged_report["limitations"])
    assert "token更少" not in json.dumps(view, ensure_ascii=False)
    assert "results are claude" not in view["narrative"]["en"]["conclusion"]
    assert {item["multiplicity_family_size"] for item in view["charts"]["delta_strips"]} == {4, 8}
    assert [item["goal"] for item in view["verdicts"]] == [
        "storage_shape",
        "semantic_layer",
        "model_ranking",
    ]
    assert [item["model"] for item in view["charts"]["diagnosis_slope"]["ranked_series"]] == [
        "claude-fable-5-1",
        "gemini-3.8-flash",
        "qwen3.8-max-0902",
        "gpt-5.6-sol",
        "deepseek-v4-pro",
        "glm-5.3",
    ]
    summaries = view["narrative"]["en"]["family_summaries"]["storage_shape"]
    assert len(summaries) == 2
    assert all("Exact sign p" not in text and len(text) < 160 for text in summaries)
    narrative = json.dumps(view["narrative"], ensure_ascii=False)
    assert "11/12" in narrative
    assert "35/35" in narrative
    assert "On every eligible case" not in narrative
    assert "返回的行数全部下降" not in narrative
    reversal = view["narrative"]["zh"]["takeaways"]["fault_dependent"]["support"]
    assert reversal[0].startswith("服务/依赖故障")
    assert reversal[1].startswith("节点故障")
    assert "Graph 112、Raw 106" in reversal[0]
    assert "Graph 12、Raw 24" in reversal[1]
    assert "不能单独归因" in reversal[2]
    invalid = copy.deepcopy(merged_report)
    invalid["inference_cohorts"][0]["models"] += invalid["inference_cohorts"][1]["models"]
    invalid["inference_cohorts"].pop()
    with pytest.raises(ValueError, match="multiplicity scope"):
        validate_formal_measurement_report(invalid)


def test_merged_totals_match_source_runs(merged_report):
    correct = Counter()
    inputs = Counter()
    for path in TRANSFERS:
        for item in json.loads(path.read_text())["runs"]:
            treatment = item["visibility"]
            correct[treatment] += item["run"]["evaluation"]["diagnosis_correct"] is True
            inputs[treatment] += item["run"]["usage"]["provider_visible_input_tokens"]
    view = build_report_view_model(merged_report)
    assert view["charts"]["headline"]["rows"][0]["values"] == dict(correct)
    assert dict(correct) == {"split_pillars": 97, "raw": 130, "semantic_graph": 124}
    for treatment, total in inputs.items():
        assert (
            merged_report["usage_by_treatment"]["by_treatment"][treatment][
                "provider_visible_input_tokens"
            ]
            == total
        )


def test_cost_chart_uses_model_cohort_rate_and_transfer_only_eligibility(merged_report):
    view = build_report_view_model(merged_report)
    series = {item["model"]: item for item in view["charts"]["cost_bars"]["series"]}
    qwen = series["qwen3.8-max-0902"]
    assert qwen["estimable"] is True
    for treatment, native in {
        "split_pillars": 126.449341,
        "raw": 94.183764,
        "semantic_graph": 97.519404,
    }.items():
        assert qwen["native"][treatment] == pytest.approx(native)
        assert qwen["values"][treatment] == round(native / 6.7787, 6)
    assert series["glm-5.3"]["exchange_rate"]["units_per_usd"] == 6.7179
    assert qwen["exchange_rate"]["units_per_usd"] == 6.7787
    assert set(merged_report["exchange_rates_by_model"]["gemini-3.8-flash"]) == {"USD"}
    assert set(merged_report["exchange_rates_by_model"]["qwen3.8-max-0902"]) == {"CNY"}
    gemini = series["gemini-3.8-flash"]
    assert gemini["estimable"] is True
    assert gemini["undiscounted"] is True
    assert "without cache discounts" in gemini["estimate_note"]["en"]
    expected = Counter()
    for item in json.loads(TRANSFERS[1].read_text())["runs"]:
        if item["model"] == "gemini-3.8-flash":
            usage = item["run"]["usage"]
            expected[item["visibility"]] += (
                usage["provider_visible_input_tokens"] * 0.75 + usage["output_tokens"] * 3.75
            ) / 1_000_000
    assert gemini["values"] == {key: round(value, 6) for key, value in expected.items()}
    assert merged_report["costs"]["cross_currency_total"] is None
    assert merged_report["costs"]["models"]["qwen3.8-max-0902"]["estimated_cost"] is None
    assert all(
        value is None
        for value in merged_report["usage_by_treatment"]["estimated_cost_usd"].values()
    )
    cost = next(row for row in view["charts"]["headline"]["rows"] if row["id"] == "cost")
    assert cost["estimable"] is True
    assert cost["converted_from"] == []
    assert "gemini-3.8-flash" in cost["estimate_note"]["en"]
    assert "without cache discounts" in cost["estimate_note"]["en"]
    assert cost["excluded_models"] == []
    assert {entry["units_per_usd"] for entry in cost["conversions"]} == {6.7179, 6.7787}
    for treatment in expected:
        assert cost["values"][treatment] == pytest.approx(
            sum(row["values"][treatment] for row in series.values()), abs=0.00001
        )


def test_merge_rejects_overlapping_rosters_and_wrong_source_binding():
    with pytest.raises(ValueError, match="overlap"):
        merge_formal_reports([REPORTS[0], REPORTS[0]], [TRANSFERS[0], TRANSFERS[0]], PUBLICATION)
    with pytest.raises(ValueError, match="binding"):
        merge_formal_reports(REPORTS, list(reversed(TRANSFERS)), PUBLICATION)


def test_partial_cost_is_not_presented_as_a_cohort_total():
    view = build_report_view_model(json.loads(REPORTS[1].read_text()))
    cost = next(row for row in view["charts"]["headline"]["rows"] if row["id"] == "cost")
    assert cost["estimable"] is False
    assert all(value is None for value in cost["values"].values())
    assert "1 model remains" in cost["unavailable_text"]["en"]
    assert "no all-model total" in cost["unavailable_text"]["en"]
    assert (
        next(
            item
            for item in view["charts"]["cost_bars"]["series"]
            if item["model"] == "qwen3.8-max-0902"
        )["estimable"]
        is True
    )


def test_merge_preserves_public_execution_deviations(merged_report):
    source = json.loads(TRANSFERS[1].read_text())
    overrides = {
        item["cell_index"]: item["provider_concurrency_limit"]
        for item in source["runs"]
        if "provider_concurrency_limit" in item
    }
    assert overrides == {index: 4 for index in (11, 35, 153, 154, 155, 165, 166, 167)}
    assert {
        item["cell_index"]: item["actual_provider_concurrency_limit"]
        for item in merged_report["execution_deviations"]
    } == overrides
    assert all(
        item["frozen_provider_concurrency_limit"] == 2
        for item in merged_report["execution_deviations"]
    )
    view = build_report_view_model(merged_report)
    assert "8 runs used" in view["execution_deviation_note"]["en"]


def test_merged_html_reproduces_and_has_utc_fallback(merged_report, tmp_path):
    first, second = tmp_path / "first.html", tmp_path / "second.html"
    render_formal_measurement_report(merged_report, first)
    render_formal_measurement_report(json.loads(json.dumps(merged_report, sort_keys=True)), second)
    assert first.read_bytes() == second.read_bytes()
    document = first.read_text()
    for key in ("measurement_updated_at", "report_generated_at"):
        assert merged_report["publication"][key] in document
    assert "2 of 12" in document
