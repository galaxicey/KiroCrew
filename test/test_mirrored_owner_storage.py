"""A mirrored name's owner has ONE storage location, in every module that mirrors one.

``sys.modules`` is where a module is stored. A module that mirrors a surface and
also holds a mapping to the resolved owner MODULE has a second storage location
for it, and the two can disagree: purging an owner and importing it again -- an
idiom this suite uses in twenty files -- leaves such a mapping reading and
forwarding writes to the discarded module while a direct importer holds the fresh
one. A test patching a control through the mirror then passes while exercising an
object nobody is running, and a later test in the same worker reads a value that
disagrees with its own owner, in another file, under some shard splits and not
others, with nothing pointing back at the cause.

The rule, stated once:

    a module that defines a module-level ``__getattr__``, or swaps its own
    ``__class__`` to forward attribute writes, MUST NOT hold a mapping whose
    values are module objects. It keeps the owner's dotted NAME and resolves it
    per use with ``importlib.import_module``, which answers from ``sys.modules``
    and waits on that module's import lock while its body runs.

These tests find the modules the rule binds BY THEIR SHAPE, read off the source
tree, rather than from a list of names. A module that begins mirroring a surface
is therefore covered on the commit that introduces it, with no edit here. A list
would be a second thing to remember, which is the same failure the rule is about.

``test_the_shape_detector_answers_both_ways`` is what keeps the parametrized
cases below honest: they are generated from the detector, so a detector that
matched nothing would leave every case silently absent instead of failing.
"""

from __future__ import annotations

import ast
import importlib
import sys
from pathlib import Path
from types import ModuleType

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"


def _module_name(path: Path) -> str:
    """Return the dotted import name of the file at *path*."""
    parts = list(path.relative_to(SRC).with_suffix("").parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def mirrors_a_surface(tree: ast.Module) -> bool:
    """True when the module resolves or forwards attributes on another's behalf.

    Two spellings do that, and both are visible at module level: a ``__getattr__``
    function, which Python calls for a name the module does not hold, and an
    assignment to some object's ``__class__``, which is how a module installs a
    ``ModuleType`` subclass over itself to intercept writes.
    """
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == "__getattr__":
            return True
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Attribute) and target.attr == "__class__":
                    return True
    return False


def owner_module_tables(tree: ast.Module) -> list[str]:
    """Return the names bound to a mapping whose VALUES are module objects.

    Two spellings reach that too: an annotation declaring ``dict[..., ModuleType]``,
    and a dict comprehension whose value expression is a bare name -- a name bound
    to an imported module, since a comprehension over modules is how such a table
    gets built. A mapping of dotted names is neither, because its values are
    strings and its annotation says so.
    """
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.AnnAssign):
            targets: list[ast.expr] = [node.target]
            annotation = ast.unparse(node.annotation)
            value: ast.expr | None = node.value
        elif isinstance(node, ast.Assign):
            targets = list(node.targets)
            annotation = ""
            value = node.value
        else:
            continue
        names = [t.id for t in targets if isinstance(t, ast.Name)]
        if not names:
            continue
        declares_modules = annotation.startswith("dict") and "ModuleType" in annotation
        builds_modules = isinstance(value, ast.DictComp) and isinstance(value.value, ast.Name)
        if declares_modules or builds_modules:
            found.extend(names)
    return sorted(set(found))


def _mirroring_modules() -> list[tuple[str, Path]]:
    rows: list[tuple[str, Path]] = []
    for path in sorted(SRC.rglob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):  # pragma: no cover - not this rule's job
            continue
        if mirrors_a_surface(tree):
            rows.append((_module_name(path), path))
    return rows


MIRRORING = _mirroring_modules()

#: One case per mirroring module, so a failure names the module in its own id.
MIRRORING_IDS = [name for name, _path in MIRRORING]


_DETECTOR_CASES: tuple[tuple[str, bool, str], ...] = (
    ("def __getattr__(name):\n    return 1\n", True, "a module-level __getattr__"),
    (
        "import sys\nsys.modules[__name__].__class__ = X\n",
        True,
        "installing a ModuleType subclass over itself",
    ),
    ("class C:\n    def __getattr__(self):\n        return 1\n", False, "a CLASS __getattr__"),
    ("X = 1\n", False, "an ordinary module"),
)

_TABLE_CASES: tuple[tuple[str, list[str], str], ...] = (
    ("from types import ModuleType\n_O: dict[str, ModuleType] = {}\n", ["_O"], "declared"),
    ("_O = {n: mod for mod in MODS for n in mod.__all__}\n", ["_O"], "a comprehension of modules"),
    ("_O = {n: mod.__name__ for mod in MODS for n in mod.__all__}\n", [], "dotted names"),
    ("_O: dict[str, str] = {}\n", [], "a declared name table"),
)


def test_the_shape_detector_answers_both_ways() -> None:
    """The detector the cases below are generated from, pinned on synthetic sources.

    A detector matching nothing would make every parametrized case vanish rather
    than fail, so it is measured directly, on inputs it must accept and inputs it
    must reject.
    """
    for source, expected, why in _DETECTOR_CASES:
        assert mirrors_a_surface(ast.parse(source)) is expected, why
    for source, expected_tables, why in _TABLE_CASES:
        assert owner_module_tables(ast.parse(source)) == expected_tables, why


def test_the_scan_finds_the_modules_that_mirror_a_surface() -> None:
    """The scan reaches the source tree at all, so an empty result is a broken scan."""
    assert MIRRORING, f"no mirroring module found under {SRC}, so every case below is absent"


@pytest.mark.parametrize(("name", "path"), MIRRORING, ids=MIRRORING_IDS)
def test_no_mirroring_module_stores_a_resolved_owner(name: str, path: Path) -> None:
    """The rule, read off the source of every module the scan finds."""
    tables = owner_module_tables(ast.parse(path.read_text(encoding="utf-8")))
    assert tables == [], (
        f"{name} holds {tables}, a mapping to resolved owner MODULES and so a second "
        "storage location beside sys.modules. Hold the owner's dotted NAME and resolve "
        "it per use with importlib.import_module."
    )


@pytest.mark.parametrize(("name", "path"), MIRRORING, ids=MIRRORING_IDS)
def test_a_mirroring_module_resolves_its_owners_through_the_import_system(
    name: str, path: Path
) -> None:
    """The other half of the rule: resolution asks the import system every time."""
    source = path.read_text(encoding="utf-8")
    assert "importlib.import_module(" in source, (
        f"{name} mirrors a surface but never calls importlib.import_module, so its "
        "owners are not being resolved from sys.modules"
    )


@pytest.mark.parametrize(("name", "path"), MIRRORING, ids=MIRRORING_IDS)
def test_no_live_mapping_holds_a_module_object(name: str, path: Path) -> None:
    """The same rule measured on the imported module, which also catches memoisation.

    A table built from dotted names but memoising the module it resolved satisfies
    the source cases and fails here, so the two are not redundant.
    """
    try:
        module = importlib.import_module(name)
    except Exception as exc:  # pragma: no cover - importability is another test's subject
        pytest.skip(f"{name} does not import here: {type(exc).__name__}")
    offenders = {
        attr: sorted(key for key, val in value.items() if isinstance(val, ModuleType))
        for attr, value in vars(module).items()
        if isinstance(value, dict) and any(isinstance(val, ModuleType) for val in value.values())
    }
    assert offenders == {}, (
        f"{name} holds resolved owner modules at runtime, so a purged owner stays "
        "invisible through it: " + repr({attr: keys[:3] for attr, keys in offenders.items()})
    )


_ABSENT = object()


def _one_reexported_pair(module: ModuleType) -> tuple[str, str] | None:
    """Return one ``(attribute, owner module name)`` pair *module* re-exports, or ``None``.

    Discovered rather than configured: each mirroring module spells its own table,
    so this reads the module's own dicts and accepts only a pair it can prove -- the
    owner imports, lives in this same top-level package, and really holds that
    attribute. The three spellings a value can take are a dotted module name, a bare
    submodule name relative to the mirroring module or its parent, and a
    ``(owner, symbol)`` pair; a module object is read for its own ``__name__``. An
    attribute holding a module is passed over, because the import system rebinds
    those itself.
    """
    root = module.__name__.split(".")[0]
    parent = module.__name__.rpartition(".")[0]
    # Snapshotted: importing a candidate below binds that submodule onto its parent,
    # which mutates this very namespace while it is being read.
    for attribute, table in list(vars(module).items()):
        if attribute.startswith("__") or not isinstance(table, dict):
            continue
        for key in sorted(k for k in table if isinstance(k, str) and not k.startswith("__")):
            value = table[key]
            if isinstance(value, ModuleType):
                candidates = [value.__name__]
            elif isinstance(value, tuple) and value and isinstance(value[0], str):
                candidates = [value[0], f"{module.__name__}.{value[0]}", f"{parent}.{value[0]}"]
            elif isinstance(value, str):
                candidates = [value, f"{module.__name__}.{value}", f"{parent}.{value}"]
            else:
                continue
            for candidate in candidates:
                if not candidate.startswith(f"{root}.") or candidate == module.__name__:
                    continue
                try:
                    owner = importlib.import_module(candidate)
                except Exception:
                    continue
                held = getattr(owner, key, _ABSENT)
                if held is _ABSENT or isinstance(held, ModuleType):
                    continue
                # A ``from __future__ import ...`` flag lands in every module that
                # uses it, so it appears owned while naming no control.
                if type(held).__module__ == "__future__":
                    continue
                return key, candidate
    return None


@pytest.mark.parametrize(("name", "path"), MIRRORING, ids=MIRRORING_IDS)
def test_a_read_through_the_mirror_resolves_the_owner_in_sys_modules(name: str, path: Path) -> None:
    """A read answers from the module ``sys.modules`` holds, not from an earlier one.

    Resolving only the OWNER through the import system while the VALUE stays bound
    in the mirroring module's own namespace is not half of this rule -- it is its
    own defect, and a worse one. A read then returns the binding made before a
    purge while a write resolves the module ``sys.modules`` now holds, and
    ``monkeypatch`` restores by reassignment: it reads the attribute to remember
    it, then assigns the remembered value back. Teardown therefore installs a
    pre-purge value into the fresh module, for the life of the worker.
    """
    try:
        module = importlib.import_module(name)
    except Exception as exc:  # pragma: no cover
        pytest.skip(f"{name} does not import here: {type(exc).__name__}")

    pair = _one_reexported_pair(module)
    if pair is None:
        pytest.skip(f"no re-exported scalar attribute discovered on {name}")
    attribute, owner_name = pair

    purged = sys.modules.pop(owner_name)
    original = getattr(purged, attribute)
    sentinel = "kiro-crew-fresh-owner-sentinel"
    try:
        fresh = importlib.import_module(owner_name)
        setattr(fresh, attribute, sentinel)
        assert getattr(module, attribute) == sentinel, (
            f"reading {name}.{attribute} did not answer from the {owner_name} that "
            "sys.modules holds, so a read and a write through this module name "
            "different objects and a patch fixture's teardown writes a pre-purge "
            "value into the fresh module"
        )
    finally:
        sys.modules[owner_name] = purged
        setattr(purged, attribute, original)


@pytest.mark.parametrize(("name", "path"), MIRRORING, ids=MIRRORING_IDS)
def test_a_purged_owner_is_seen_through_the_mirroring_module(name: str, path: Path) -> None:
    """The behaviour the rule exists for: a reimported owner is the one in use.

    Measured per module rather than argued, on whichever submodule of it is already
    imported, since that is the object a purge would replace.
    """
    try:
        module = importlib.import_module(name)
    except Exception as exc:  # pragma: no cover
        pytest.skip(f"{name} does not import here: {type(exc).__name__}")

    depth = name.count(".") + 1
    children = sorted(
        loaded
        for loaded in list(sys.modules)
        if loaded.startswith(f"{name}.") and loaded.count(".") == depth
    )
    if not children:
        pytest.skip(f"{name} has no imported submodule to purge")

    owner_name = children[0]
    purged = sys.modules.pop(owner_name)
    try:
        fresh = importlib.import_module(owner_name)
        assert sys.modules[owner_name] is fresh
        held = [
            attr
            for attr, value in list(vars(module).items())
            if isinstance(value, dict) and purged in value.values()
        ]
        assert held == [], (
            f"{name} keeps the discarded {owner_name} in {held} after a purge and "
            "reimport, so reads and writes through it reach a module nothing else sees"
        )
    finally:
        sys.modules[owner_name] = purged
