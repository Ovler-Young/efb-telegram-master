from pathlib import Path

import pytest
from ruamel.yaml import YAML


WORKFLOW_PATH = Path(__file__).parents[2] / ".github" / "workflows" / "tests.yml"
INTEGRATION_CONDITION = (
    "github.event_name == 'pull_request_target' || "
    "(github.event_name == 'pull_request' && "
    "github.event.pull_request.head.repo.full_name == github.repository)"
)


def load_workflow() -> dict:
    yaml = YAML(typ="safe")
    return yaml.load(WORKFLOW_PATH)


def integration_enabled(event_name: str, head_repository: str, repository: str) -> bool:
    return event_name == "pull_request_target" or (
        event_name == "pull_request" and head_repository == repository
    )


def test_workflow_accepts_both_pull_request_events() -> None:
    workflow = load_workflow()

    assert "pull_request" in workflow["on"]
    assert "pull_request_target" in workflow["on"]


@pytest.mark.parametrize(
    ("event_name", "head_repository", "expected"),
    [
        ("pull_request_target", "external/fork", True),
        ("pull_request", "Ovler-Young/efb-telegram-master", True),
        ("pull_request", "external/fork", False),
        ("push", "Ovler-Young/efb-telegram-master", False),
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

