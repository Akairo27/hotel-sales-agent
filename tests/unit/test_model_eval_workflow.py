"""The manual model-eval workflow's safety properties (owner requirements,
2026-09-30), pinned so a later edit cannot quietly loosen them: it spends
real money and holds the OpenRouter key. Checked on the file's text -- no
YAML parser is a dependency of this repo."""

from __future__ import annotations

import re
from pathlib import Path

_WORKFLOW = (
    Path(__file__).resolve().parents[2] / ".github" / "workflows" / "model-eval.yml"
)
_SECRET = "secrets.OPENROUTER_EVAL_API_KEY"


def _text() -> str:
    return _WORKFLOW.read_text(encoding="utf-8")


def _trigger_block(text: str) -> str:
    match = re.search(r"^on:\n(.*?)^\S", text, re.MULTILINE | re.DOTALL)
    assert match is not None
    return match.group(1)


def test_the_eval_is_triggered_manually_only() -> None:
    trigger = _trigger_block(_text())
    assert "workflow_dispatch:" in trigger
    for automatic in ("push", "pull_request", "schedule", "workflow_run"):
        assert f"{automatic}:" not in trigger


def test_the_workflow_can_only_read_the_repository() -> None:
    assert "\npermissions:\n  contents: read\n" in _text()
    assert "write" not in _text().split("\npermissions:\n", 1)[1].split("\n\n", 1)[0]


def test_the_key_reaches_only_the_eval_step_and_is_never_echoed() -> None:
    text = _text()
    assert text.count(_SECRET) == 1
    steps = text.split("      - name: ")
    (eval_step,) = [step for step in steps if _SECRET in step]
    assert eval_step.startswith("Run the eval")
    assert "echo" not in text
    assert "set -x" not in text


def test_the_artifact_is_the_results_report_only() -> None:
    upload = _text().split("Upload the results", 1)[1]
    assert "path: model-eval-results.md" in upload


def test_the_settings_come_from_the_dispatch_and_default_to_production() -> None:
    """Owner-approved (2026-09-30): the prompt PR's run uses low, the
    production setting, alone; a comparison names its settings at dispatch.
    The input reaches the script through the environment, never pasted
    into it, and globbing is off."""
    text = _text()
    settings_input = text.split("      settings:\n", 1)[1].split("\n\n", 1)[0]
    assert 'default: "low"' in settings_input
    assert "SETTINGS: ${{ inputs.settings }}" in text
    run_script = text.split("Run the eval", 1)[1]
    assert "${{ inputs" not in run_script.split("run: |", 1)[1]
    assert "set -euf -o pipefail" in run_script
    assert '--reasoning-effort "$setting"' in run_script


def test_the_categories_come_from_the_dispatch_and_default_to_all() -> None:
    """Owner-approved 2026-10-01: re-run a subset (e.g. booking). Empty runs
    every scenario; the input reaches the script only through the
    environment, like the settings."""
    text = _text()
    categories_input = text.split("      categories:\n", 1)[1].split("\n\n", 1)[0]
    assert 'default: ""' in categories_input
    assert "CATEGORIES: ${{ inputs.categories }}" in text
    run_script = text.split("run: |", 1)[1]
    assert '--category "$category"' in run_script
    assert '"${category_args[@]}"' in run_script
