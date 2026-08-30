"""Executable ownership and framework-independence architecture checks."""

import ast
from pathlib import Path

import pytest

from fulfillflow.orders.schemas import OrderFilters
from fulfillflow.shipments.schemas import ShipmentListFilters

PACKAGE_ROOT = Path("src/fulfillflow")
BUSINESS_MODULES = ("orders", "shipments", "carriers", "tracking")
FORBIDDEN_TRANSACTION_SYMBOLS = frozenset({"commit", "rollback"})
PUBLIC_FACADE_PATHS = tuple(PACKAGE_ROOT / module / "public.py" for module in BUSINESS_MODULES)
PUBLIC_PARTICIPANTS = (
    (
        PACKAGE_ROOT / "orders" / "public.py",
        "OrdersPublic",
        PACKAGE_ROOT / "orders" / "service.py",
    ),
    (
        PACKAGE_ROOT / "shipments" / "public.py",
        "ShipmentsPublic",
        PACKAGE_ROOT / "shipments" / "service.py",
    ),
    (
        PACKAGE_ROOT / "carriers" / "public.py",
        "CarriersPublic",
        PACKAGE_ROOT / "carriers" / "public.py",
    ),
)


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            imported.add(node.module)
    return imported


def _assigned_names(target: ast.expr) -> set[str]:
    if isinstance(target, ast.Name):
        return {target.id}
    if isinstance(target, ast.Starred):
        return _assigned_names(target.value)
    if isinstance(target, (ast.List, ast.Tuple)):
        return {name for element in target.elts for name in _assigned_names(element)}
    return set()


def _import_binding(alias: ast.alias, *, from_import: bool) -> str:
    if alias.asname is not None:
        return alias.asname
    return alias.name if from_import else alias.name.split(".", maxsplit=1)[0]


def _top_level_non_class_bindings(tree: ast.Module) -> dict[str, int]:
    bindings: dict[str, int] = {}
    for node in tree.body:
        if isinstance(node, ast.Import):
            for alias in node.names:
                bindings[_import_binding(alias, from_import=False)] = node.lineno
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                bindings[_import_binding(alias, from_import=True)] = node.lineno
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            bindings[node.name] = node.lineno
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                for name in _assigned_names(target):
                    bindings[name] = node.lineno
        elif isinstance(node, (ast.AnnAssign, ast.AugAssign)):
            for name in _assigned_names(node.target):
                bindings[name] = node.lineno
    return bindings


def _transaction_boundary_violations(
    source: str,
    *,
    expected_public_classes: frozenset[str] = frozenset(),
    scan_entire_module: bool = False,
) -> list[str]:
    """Find direct transaction controls and invalid public-participant definitions."""
    tree = ast.parse(source)
    violations: list[str] = []
    class_definitions = {
        name: [node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == name]
        for name in expected_public_classes
    }
    non_class_bindings = _top_level_non_class_bindings(tree)

    for name, definitions in class_definitions.items():
        if len(definitions) != 1:
            violations.append(
                f"{name} must have exactly one top-level ClassDef; found {len(definitions)}"
            )
        if name in non_class_bindings:
            violations.append(
                f"{name} is rebound by a non-class definition at line {non_class_bindings[name]}"
            )

    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                referenced_name = alias.name.rsplit(".", maxsplit=1)[-1]
                bound_name = _import_binding(
                    alias,
                    from_import=isinstance(node, ast.ImportFrom),
                )
                if (
                    referenced_name in FORBIDDEN_TRANSACTION_SYMBOLS
                    or bound_name in FORBIDDEN_TRANSACTION_SYMBOLS
                ):
                    violations.append(
                        f"forbidden transaction import {alias.name!r} at line {node.lineno}"
                    )

    attribute_roots: list[ast.AST]
    if scan_entire_module:
        attribute_roots = [tree]
    else:
        attribute_roots = [
            definition for definitions in class_definitions.values() for definition in definitions
        ]
    for root in attribute_roots:
        for node in ast.walk(root):
            if isinstance(node, ast.Attribute) and node.attr in FORBIDDEN_TRANSACTION_SYMBOLS:
                violations.append(
                    f"forbidden transaction attribute {node.attr!r} at line {node.lineno}"
                )
    return violations


def _facade_exposes_declared_participant(
    facade_path: Path,
    symbol: str,
    definition_path: Path,
) -> bool:
    tree = ast.parse(facade_path.read_text(encoding="utf-8"), filename=str(facade_path))
    bindings: list[ast.stmt] = []
    for node in tree.body:
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name == symbol:
                bindings.append(node)
        elif isinstance(node, ast.Import):
            if any(_import_binding(alias, from_import=False) == symbol for alias in node.names):
                bindings.append(node)
        elif isinstance(node, ast.ImportFrom):
            if any(_import_binding(alias, from_import=True) == symbol for alias in node.names):
                bindings.append(node)
        elif isinstance(node, ast.Assign):
            if any(symbol in _assigned_names(target) for target in node.targets):
                bindings.append(node)
        elif isinstance(node, (ast.AnnAssign, ast.AugAssign)):
            if symbol in _assigned_names(node.target):
                bindings.append(node)

    exports = {
        element.value
        for node in tree.body
        if isinstance(node, ast.Assign)
        and any("__all__" in _assigned_names(target) for target in node.targets)
        and isinstance(node.value, (ast.List, ast.Tuple))
        for element in node.value.elts
        if isinstance(element, ast.Constant) and isinstance(element.value, str)
    }
    if len(bindings) != 1 or symbol not in exports:
        return False

    binding = bindings[0]
    if facade_path == definition_path:
        return isinstance(binding, ast.ClassDef) and binding.name == symbol

    definition_module = ".".join(definition_path.with_suffix("").parts[1:])
    return (
        isinstance(binding, ast.ImportFrom)
        and binding.module == definition_module
        and any(alias.name == symbol and alias.asname is None for alias in binding.names)
    )


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


def test_repositories_and_public_participants_never_commit_or_rollback() -> None:
    for module in BUSINESS_MODULES:
        path = PACKAGE_ROOT / module / "repository.py"
        violations = _transaction_boundary_violations(
            path.read_text(encoding="utf-8"),
            scan_entire_module=True,
        )
        assert violations == [], f"{path}: {violations}"

    for path in PUBLIC_FACADE_PATHS:
        violations = _transaction_boundary_violations(
            path.read_text(encoding="utf-8"),
            scan_entire_module=True,
        )
        assert violations == [], f"{path}: {violations}"

    for facade_path, symbol, definition_path in PUBLIC_PARTICIPANTS:
        assert _facade_exposes_declared_participant(
            facade_path,
            symbol,
            definition_path,
        ), f"{facade_path} does not expose {symbol} from {definition_path}"
        violations = _transaction_boundary_violations(
            definition_path.read_text(encoding="utf-8"),
            expected_public_classes=frozenset({symbol}),
        )
        assert violations == [], f"{definition_path}:{symbol}: {violations}"


@pytest.mark.parametrize(
    ("source", "expected_fragment"),
    [
        pytest.param(
            "class OrdersPublic:\n"
            "    async def complete(self):\n"
            "        await self._session.commit()\n",
            "transaction attribute 'commit'",
            id="direct-commit-call",
        ),
        pytest.param(
            "class OrdersPublic:\n"
            "    async def complete(self):\n"
            "        commit_transaction = self._session.commit\n"
            "        await commit_transaction()\n",
            "transaction attribute 'commit'",
            id="captured-commit-attribute",
        ),
        pytest.param(
            "class OrdersParticipant:\n"
            "    async def complete(self):\n"
            "        await self._session.commit()\n"
            "\n"
            "OrdersPublic = OrdersParticipant\n",
            "must have exactly one top-level ClassDef",
            id="public-participant-assignment-alias",
        ),
        pytest.param(
            "from participants import OrdersParticipant as OrdersPublic\n",
            "must have exactly one top-level ClassDef",
            id="public-participant-import-reexport",
        ),
        pytest.param(
            "from transactions import commit as commit_transaction\n"
            "\n"
            "class OrdersPublic:\n"
            "    async def complete(self):\n"
            "        await self._session.flush()\n",
            "forbidden transaction import 'commit'",
            id="explicit-commit-import",
        ),
        pytest.param(
            "from transactions import operation as rollback\n"
            "\n"
            "class OrdersPublic:\n"
            "    async def complete(self):\n"
            "        await self._session.flush()\n",
            "forbidden transaction import 'operation'",
            id="rollback-import-alias",
        ),
    ],
)
def test_transaction_boundary_ast_rule_rejects_direct_mutations(
    source: str,
    expected_fragment: str,
) -> None:
    violations = _transaction_boundary_violations(
        source,
        expected_public_classes=frozenset({"OrdersPublic"}),
        scan_entire_module=True,
    )
    assert any(expected_fragment in violation for violation in violations), violations


def test_transaction_boundary_ast_rule_accepts_unrelated_operations() -> None:
    source = (
        "class OrdersPublic:\n"
        "    async def complete(self):\n"
        "        await self._session.flush()\n"
        "        result = self._repository.save\n"
        "        await result()\n"
    )

    assert (
        _transaction_boundary_violations(
            source,
            expected_public_classes=frozenset({"OrdersPublic"}),
            scan_entire_module=True,
        )
        == []
    )
