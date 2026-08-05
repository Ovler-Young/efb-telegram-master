from pathlib import Path

import pytest
from ruamel.yaml import YAML


WORKFLOW_PATH = Path(__file__).parents[2] / ".github" / "workflows" / "tests.yml"
INTEGRATION_CONDITION = (
    "github.event_name == 'pull_request' && "
    "github.event.pull_request.head.repo.full_name == github.repository"
)


def load_workflow() -> dict:
    yaml = YAML(typ="safe")
    return yaml.load(WORKFLOW_PATH)


def integration_enabled(event_name: str, head_repository: str, repository: str) -> bool:
    return event_name == "pull_request" and head_repository == repository


def test_workflow_does_not_accept_pull_request_target() -> None:
    workflow = load_workflow()

    assert "pull_request" in workflow["on"]
    assert "pull_request_target" not in workflow["on"]


@pytest.mark.parametrize(
    ("event_name", "head_repository", "expected"),
    [
        ("pull_request", "Ovler-Young/efb-telegram-master", True),
        ("pull_request", "external/fork", False),
        ("push", "Ovler-Young/efb-telegram-master", False),
        ("workflow_dispatch", "Ovler-Young/efb-telegram-master", False),
    ],
)
def test_credentialed_integration_event_gate(
    event_name: str,
    head_repository: str,
    expected: bool,
) -> None:
    workflow = load_workflow()

    assert workflow["jobs"]["integration"]["if"] == INTEGRATION_CONDITION
    assert integration_enabled(
        event_name,
        head_repository,
        "Ovler-Young/efb-telegram-master",
    ) is expected


def test_unit_job_has_no_credentialed_environment_or_secret_env() -> None:
    workflow = load_workflow()
    unit_job = workflow["jobs"]["unit"]

    assert "environment" not in unit_job
    assert "secrets." not in str(unit_job)


def test_unit_job_runs_for_push_and_all_pull_requests() -> None:
    workflow = load_workflow()
    unit_job = workflow["jobs"]["unit"]

    assert "push" in workflow["on"]
    assert "pull_request" in workflow["on"]
    assert "if" not in unit_job


def test_unit_checkout_uses_event_default_ref() -> None:
    workflow = load_workflow()
    checkout_step = workflow["jobs"]["unit"]["steps"][0]

    assert checkout_step["uses"] == "actions/checkout@v4"
    assert "with" not in checkout_step


def test_integration_checkout_uses_same_repo_pull_request_default_ref() -> None:
    workflow = load_workflow()
    checkout_step = workflow["jobs"]["integration"]["steps"][0]

    assert checkout_step["uses"] == "actions/checkout@v4"
    assert "with" not in checkout_step


def test_integration_keeps_baseline_auxiliary_secret_name() -> None:
    workflow = load_workflow()
    integration_step = next(
        step
        for step in workflow["jobs"]["integration"]["steps"]
        if step.get("name") == "Test integration suite"
    )

    assert integration_step["env"]["AUX_BOT_TOKEN_2"] == (
        "${{ secrets.TELEGRAM_AUX_BOT_TOKEN_2 }}"
    )
