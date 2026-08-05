import ast
from pathlib import Path

RETIRED_OUTBOUND_IDENTIFIERS = frozenset(
    {
        "OutboundWorkflow",
        "OutboundTask",
        "OutboundRepository",
        "OutboundScheduler",
        "RunCondition",
        "TaskState",
        "SlotReservation",
        "ReservationOutcome",
    }
)


def _ast_identifiers(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    identifiers: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            identifiers.add(node.id)
        elif isinstance(node, ast.Attribute):
            identifiers.add(node.attr)
        elif isinstance(node, ast.alias):
            identifiers.add(node.name)
            if node.asname is not None:
                identifiers.add(node.asname)
        elif isinstance(node, ast.arg):
            identifiers.add(node.arg)
        elif isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            identifiers.add(node.name)
    return identifiers


def test_runtime_package_has_no_retired_outbound_identifiers():
    package_root = Path(__file__).parents[2] / "efb_telegram_master"
    found: dict[str, set[str]] = {}

    for source_path in package_root.rglob("*.py"):
        prohibited = RETIRED_OUTBOUND_IDENTIFIERS & _ast_identifiers(source_path)
        if prohibited:
            found[str(source_path.relative_to(package_root))] = prohibited

    assert found == {}
