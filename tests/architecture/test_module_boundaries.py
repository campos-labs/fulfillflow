"""Executable ownership and framework-independence architecture checks."""

import ast
from pathlib import Path

from fulfillflow.orders.schemas import OrderFilters
from fulfillflow.shipments.schemas import ShipmentListFilters

PACKAGE_ROOT = Path("src/fulfillflow")
BUSINESS_MODULES = ("orders", "shipments", "carriers", "tracking")


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            imported.add(node.module)
    return imported


def test_domain_modules_have_no_framework_or_infrastructure_imports() -> None:
    forbidden = ("fastapi", "sqlalchemy", "fulfillflow.db", "fulfillflow.api")
    for module in ("orders", "shipments", "tracking"):
        imports = _imports(PACKAGE_ROOT / module / "domain.py")
        assert not {imported for imported in imports if imported.startswith(forbidden)}


def test_business_internals_are_imported_only_by_their_owner() -> None:
    violations: list[str] = []
    for path in PACKAGE_ROOT.rglob("*.py"):
        relative = path.relative_to(PACKAGE_ROOT)
        owner = relative.parts[0]
        for imported in _imports(path):
            for module in BUSINESS_MODULES:
                private_prefixes = tuple(
                    f"fulfillflow.{module}.{name}"
                    for name in ("domain", "models", "repository", "service")
                )
                if imported.startswith(private_prefixes) and owner != module:
                    violations.append(f"{relative}: {imported}")
    assert violations == []


def test_public_filter_dtos_are_owned_by_public_schema_modules() -> None:
    expected_modules = {
        OrderFilters: "fulfillflow.orders.schemas",
        ShipmentListFilters: "fulfillflow.shipments.schemas",
    }

    for dto, expected_module in expected_modules.items():
        assert dto.__module__ == expected_module
        assert dto.__module__.rsplit(".", maxsplit=1)[-1] not in {
            "domain",
            "models",
            "repository",
            "service",
        }


def test_shipments_uses_only_business_public_interfaces() -> None:
    violations: list[str] = []
    for path in (PACKAGE_ROOT / "shipments").rglob("*.py"):
        for imported in _imports(path):
            if imported.startswith(
                ("fulfillflow.orders.", "fulfillflow.carriers.")
            ) and not imported.endswith(".public"):
                violations.append(f"{path.name}: {imported}")
    assert violations == []


def test_tracking_uses_only_business_public_interfaces() -> None:
    violations: list[str] = []
    for path in (PACKAGE_ROOT / "tracking").rglob("*.py"):
        for imported in _imports(path):
            if imported.startswith(
                (
                    "fulfillflow.carriers.",
                    "fulfillflow.shipments.",
                    "fulfillflow.notifications.",
                )
            ) and not imported.endswith(".public"):
                violations.append(f"{path.name}: {imported}")
    assert violations == []


def test_orders_and_carriers_do_not_depend_on_business_siblings() -> None:
    for module in ("orders", "carriers"):
        forbidden = {sibling for sibling in BUSINESS_MODULES if sibling != module}
        for path in (PACKAGE_ROOT / module).rglob("*.py"):
            imports = _imports(path)
            assert not {
                imported
                for imported in imports
                if any(imported.startswith(f"fulfillflow.{name}") for name in forbidden)
            }


def test_repositories_never_commit_or_rollback() -> None:
    forbidden_calls = {"commit", "rollback"}
    for module in BUSINESS_MODULES:
        path = PACKAGE_ROOT / module / "repository.py"
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        calls = {
            node.func.attr
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        }
        assert calls.isdisjoint(forbidden_calls)
