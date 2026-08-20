"""Structural tests for the published Home Assistant blueprints."""

from pathlib import Path

import yaml

REPOSITORY_ROOT = Path(__file__).parents[1]


class _BlueprintLoader(yaml.SafeLoader):
    """Load Home Assistant's !input tag without resolving an instance."""


def _input(loader: _BlueprintLoader, node: yaml.Node) -> dict[str, str]:
    return {"__input__": loader.construct_scalar(node)}


_BlueprintLoader.add_constructor("!input", _input)


def _load(path: str) -> dict:
    with (REPOSITORY_ROOT / path).open(encoding="utf-8") as file:
        return yaml.load(file, Loader=_BlueprintLoader)


def test_scanner_automation_blueprint_has_portable_event_contract() -> None:
    blueprint = _load(
        "blueprints/automation/grocy_stock_manager/barcode_scanner_event.yaml"
    )

    assert blueprint["blueprint"]["domain"] == "automation"
    assert blueprint["blueprint"]["source_url"].startswith(
        "https://github.com/Bibbleq/ha-grocy-stock-manager/"
    )
    scanner_inputs = blueprint["blueprint"]["input"]["scanner"]["input"]
    assert scanner_inputs["processor_script"]["selector"]["entity"]
    assert blueprint["mode"] == "queued"


def test_processing_blueprint_delegates_household_ui_to_callback() -> None:
    blueprint = _load(
        "blueprints/script/grocy_stock_manager/process_barcode.yaml"
    )

    assert blueprint["blueprint"]["domain"] == "script"
    callback = blueprint["blueprint"]["input"]["callbacks"]["input"][
        "on_update"
    ]
    assert callback["selector"] == {"action": None}
    assert blueprint["mode"] == "queued"

    raw = (
        REPOSITORY_ROOT
        / "blueprints/script/grocy_stock_manager/process_barcode.yaml"
    ).read_text(encoding="utf-8")
    assert "input_text.garage_" not in raw
    assert "script.garage_" not in raw
    assert "notify." not in raw


def test_processing_blueprint_orders_catalogue_before_ai_job() -> None:
    raw = (
        REPOSITORY_ROOT
        / "blueprints/script/grocy_stock_manager/process_barcode.yaml"
    ).read_text(encoding="utf-8")

    catalogue = raw.index("action: !input deterministic_lookup_script")
    candidate_job = raw.index("Queue trusted deterministic candidate without AI")
    ai_job = raw.index("agent_id: !input identification_agent")
    assert catalogue < candidate_job < ai_job
