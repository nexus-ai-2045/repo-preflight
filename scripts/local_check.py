#!/usr/bin/env python3
"""差分と依存周辺を選択して手元・CIで同じ試験を回す。未知の対応関係は、計画とCIでは停止し、互換実行では全部の試験を回す。

identity / consistency の必須ゲートは選択せず維持する。
--plan-tests は副作用のない汎用選択、--run-selected-tests は選択済みpytestだけの入口。
未解決の全件実行は --run-selected-tests だけが行う。full / quick は既存呼出し用の互換名。
"""

from __future__ import annotations

import argparse
import ast
import fnmatch
import getpass
import hashlib
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import NamedTuple

_IDENTITY_PATH = Path(__file__).resolve().with_name("local_identity_guard.py")
_IDENTITY_KEY = (
    "_selection_identity_" + hashlib.sha256(_IDENTITY_PATH.read_bytes()).hexdigest()
)
if _IDENTITY_KEY not in sys.modules:
    _identity_spec = importlib.util.spec_from_file_location(
        _IDENTITY_KEY, _IDENTITY_PATH
    )
    local_identity_guard = importlib.util.module_from_spec(_identity_spec)
    sys.modules[_IDENTITY_KEY] = local_identity_guard
    _identity_spec.loader.exec_module(local_identity_guard)
else:
    local_identity_guard = sys.modules[_IDENTITY_KEY]

ROOT = Path(__file__).resolve().parents[1]
SUBPROCESS_CALLS = (
    "subprocess.run",
    "subprocess.Popen",
    "subprocess.call",
    "subprocess.check_call",
    "subprocess.check_output",
)
STDLIB_LOADERS = (
    "importlib.import_module",
    "importlib.util.spec_from_file_location",
    "runpy.run_path",
    "runpy.run_module",
    "__import__",
    "builtins.__import__",
)
DEFAULT_SELECTION = {
    "source_roots": ["scripts"],
    "test_roots": ["tests"],
    "test_patterns": ["test_*.py"],
    "code_suffixes": [".py"],
    "rules": [],
}


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True
    ).stdout


def changed_files(repo: Path, base: str) -> list[str]:
    """比較元との分岐点から作業ツリーまで（コミット済み・ステージ・未ステージ・未追跡）。"""
    merge_base = git(repo, "merge-base", base, "HEAD").strip()
    names = set(
        git(repo, "diff", "--no-renames", "--name-only", "-z", merge_base).split("\0")
    )
    names |= set(
        git(repo, "ls-files", "-z", "--others", "--exclude-standard").split("\0")
    )
    names.discard("")
    return sorted(names)


def _safe_relative(repo: Path, name: str) -> str:
    """Policy and changed paths never escape the target repository (including symlinks)."""
    path = Path(name)
    if (
        not name
        or path.is_absolute()
        or PureWindowsPath(name).drive
        or ".." in path.parts
        or "\\" in name
    ):
        raise ValueError("selection paths must be repository-relative")
    try:
        (repo / path).resolve().relative_to(repo.resolve())
    except ValueError as exc:
        raise ValueError("selection path escapes repository") from exc
    return path.as_posix()


def _selection_config(repo: Path, config_path: Path | None = None) -> dict:
    path = config_path or Path(".test-selection.json")
    if path.is_absolute():
        try:
            path = path.resolve().relative_to(repo.resolve())
        except ValueError as exc:
            raise ValueError(
                "selection config must belong to target repository"
            ) from exc
    safe = repo / _safe_relative(repo, path.as_posix())
    if config_path is None and not safe.exists():
        return {}
    config = json.loads(safe.read_text(encoding="utf-8"))
    if not isinstance(config, dict):
        raise ValueError("selection config must be an object")
    return config


def _root_files(repo: Path, root: str):
    path = repo / _safe_relative(repo, root)
    if path.is_file():
        yield path
        return
    omitted = {
        ".git",
        ".local",
        ".worktrees",
        ".venv",
        "node_modules",
        "__pycache__",
        ".pytest_cache",
    }
    for current, directories, files in os.walk(repo / root):
        directories[:] = sorted(name for name in directories if name not in omitted)
        for name in sorted(files):
            yield Path(current) / name


def _within_root(name: str, root: str) -> bool:
    prefix = Path(root).as_posix().rstrip("/")
    return prefix == "." or name == prefix or name.startswith(prefix + "/")


def _reference_path(value: str) -> PurePosixPath:
    """Static file identities use POSIX separators on every execution platform."""
    return PurePosixPath(value.replace("\\", "/"))


def _lexical_scopes(tree, package="", *, import_candidates=None):
    """One binding table per lexical scope; never borrow another function's locals."""
    scopes = {}
    tables = {}
    owners = {}
    enclosing = {}
    origins = {}
    # Pattern captures store identifiers in fields rather than Name(Store).
    # The empty tuple keeps this scanner compatible with Python 3.9 ASTs.
    capture_nodes = tuple(
        getattr(ast, kind)
        for kind in ("MatchAs", "MatchStar", "MatchMapping")
        if hasattr(ast, kind)
    )

    def visit_scope(
        scope,
        inherited,
        inherited_owners=None,
        inherited_origins=None,
        inherited_candidates=None,
    ):
        local = []
        children = []

        def walk(node):
            scopes[node] = scope
            if node is not scope and isinstance(
                node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)
            ):
                children.append(node)
                local.append(node)
                # Definition headers execute in the enclosing namespace. Only
                # the function/class body introduces the child namespace.
                headers = list(getattr(node, "decorator_list", []))
                if isinstance(
                    node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)
                ):
                    headers.extend(node.args.defaults)
                    headers.extend(
                        value for value in node.args.kw_defaults if value is not None
                    )
                    arguments = (
                        node.args.posonlyargs
                        + node.args.args
                        + node.args.kwonlyargs
                        + [node.args.vararg, node.args.kwarg]
                    )
                    headers.extend(
                        arg.annotation
                        for arg in arguments
                        if arg is not None and arg.annotation is not None
                    )
                    if getattr(node, "returns", None) is not None:
                        headers.append(node.returns)
                elif isinstance(node, ast.ClassDef):
                    headers.extend(node.bases)
                    headers.extend(keyword.value for keyword in node.keywords)
                for header in headers:
                    walk(header)
                return
            local.append(node)
            if node is scope and isinstance(
                node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
            ):
                for statement in node.body:
                    walk(statement)
            elif node is scope and isinstance(node, ast.Lambda):
                walk(node.body)
            else:
                for child in ast.iter_child_nodes(node):
                    walk(child)

        walk(scope)
        bindings = {}
        binding_origins = {}
        imports = {}
        local_candidates = {}

        def bind(name, expression, *, origin=True):
            bindings.setdefault(name, []).append(expression)
            if origin:
                binding_origins.setdefault(name, []).append(expression)

        if isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            for arg in scope.args.posonlyargs + scope.args.args + scope.args.kwonlyargs:
                bind(arg.arg, None)
            for arg in (scope.args.vararg, scope.args.kwarg):
                if arg:
                    bind(arg.arg, None)
        for node in local:
            if isinstance(node, (ast.Assign, ast.AnnAssign)):
                for target in (
                    node.targets if isinstance(node, ast.Assign) else [node.target]
                ):
                    for part in ast.walk(target):
                        if isinstance(part, ast.Name):
                            bind(
                                part.id,
                                node.value,
                                origin=isinstance(part.ctx, ast.Store),
                            )
            elif isinstance(node, (ast.AugAssign, ast.NamedExpr)) and isinstance(
                node.target, ast.Name
            ):
                bind(node.target.id, None)
            elif (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name)
                and node.func.attr
                in (
                    "append",
                    "extend",
                    "insert",
                    "pop",
                    "remove",
                    "clear",
                    "sort",
                    "reverse",
                )
            ):
                bind(node.func.value.id, None, origin=False)
            elif isinstance(node, ast.Import):
                for entry in node.names:
                    name = entry.asname or entry.name.split(".")[0]
                    bind(name, None)
                    imports[name] = (
                        entry.name if entry.asname else entry.name.split(".")[0]
                    )
                    local_candidates.setdefault(name, set()).add(imports[name])
            elif isinstance(node, ast.ImportFrom):
                for entry in node.names:
                    name = entry.asname or entry.name
                    bind(name, None)
                    prefix = node.module or ""
                    if node.level:
                        try:
                            prefix = importlib.util.resolve_name(
                                "." * node.level + prefix, package
                            )
                        except (ImportError, ValueError):
                            prefix = "__unknown_relative_package__"
                    imports[name] = prefix + "." + entry.name
                    local_candidates.setdefault(name, set()).add(imports[name])
            elif node is not scope and isinstance(
                node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
            ):
                bind(node.name, node)
            elif isinstance(node, capture_nodes):
                captured = getattr(node, "name", None) or getattr(node, "rest", None)
                if captured is not None:
                    bind(captured, None)
            elif isinstance(node, (ast.For, ast.AsyncFor)):
                for target in ast.walk(node.target):
                    if isinstance(target, ast.Name):
                        bind(target.id, None)
            elif isinstance(node, ast.ExceptHandler) and node.name:
                bind(node.name, None)
            elif isinstance(node, (ast.With, ast.AsyncWith)):
                for item in node.items:
                    if item.optional_vars:
                        for target in ast.walk(item.optional_vars):
                            if isinstance(target, ast.Name):
                                bind(target.id, None)
            elif isinstance(node, ast.comprehension):
                for target in ast.walk(node.target):
                    if isinstance(target, ast.Name):
                        bind(target.id, None)
            elif isinstance(node, (ast.Global, ast.Nonlocal)):
                for name in node.names:
                    bind(name, None)
                    bind(name, None)
        values, aliases, ambiguous = (
            dict(inherited[0]),
            dict(inherited[1]),
            set(inherited[2]),
        )
        scope_owners = dict(inherited_owners or {})
        for name, entries in bindings.items():
            scope_owners[name] = scope
            values.pop(name, None)
            aliases.pop(name, None)
            ambiguous.discard(name)
            if len(entries) == 1:
                if entries[0] is not None:
                    values[name] = entries[0]
                if name in imports:
                    aliases[name] = imports[name]
            else:
                ambiguous.add(name)
                if name in imports:
                    aliases[name] = imports[name]
            if name in inherited[1] and name not in imports:
                aliases[name] = inherited[1][name]
                ambiguous.add(name)
        candidates = {
            name: set(origins) for name, origins in (inherited_candidates or {}).items()
        }
        for name in bindings:
            if name in local_candidates:
                candidates[name] = local_candidates[name]
            elif name not in aliases:
                candidates.pop(name, None)
        if import_candidates is not None:
            import_candidates[scope] = candidates
        if any(
            isinstance(node, ast.ImportFrom)
            and any(entry.name == "*" for entry in node.names)
            for node in local
        ):
            # Exported names are not proved; a star import may overwrite any binding.
            ambiguous.update(values)
            ambiguous.update(aliases)
            values.clear()
        if isinstance(scope, ast.ClassDef):
            ambiguous.update(imports)
        tables[scope] = values, aliases, ambiguous
        origins[scope] = {**(inherited_origins or {}), **binding_origins}
        for declaration in local:
            if isinstance(declaration, (ast.Global, ast.Nonlocal)):
                inherited_bindings = (
                    origins.get(tree, {})
                    if isinstance(declaration, ast.Global)
                    else (inherited_origins or {})
                )
                for name in declaration.names:
                    origins[scope][name] = inherited_bindings.get(name, [])
        owners[scope] = scope_owners
        for child in children:
            enclosing[child] = scope
            # Class locals are not the lexical namespace of a method.
            visit_scope(
                child,
                inherited if isinstance(scope, ast.ClassDef) else tables[scope],
                inherited_owners if isinstance(scope, ast.ClassDef) else scope_owners,
                (
                    inherited_origins
                    if isinstance(scope, ast.ClassDef)
                    else origins[scope]
                ),
                inherited_candidates if isinstance(scope, ast.ClassDef) else candidates,
            )

    visit_scope(tree, ({}, {}, set()))
    # Declarations can mutate the original binding observed by enclosing readers.
    # Invalidate that origin and its inherited copies, not unrelated local names.
    uncertain_origins = set()
    for declaration in ast.walk(tree):
        if not isinstance(declaration, (ast.Global, ast.Nonlocal)):
            continue
        scope = scopes[declaration]
        for name in declaration.names:
            origin = (
                tree
                if isinstance(declaration, ast.Global)
                else owners.get(enclosing.get(scope), {}).get(name)
            )
            uncertain_origins.add((origin, name))
    # A list shared under another name can be mutated through that name. Keep
    # its Python-prefix provenance, but do not keep a resolved command value.
    for bindings in origins.values():
        for expressions in bindings.values():
            for expression in expressions:
                if not isinstance(expression, ast.Name):
                    continue
                origin = owners[scopes[expression]].get(expression.id)
                if any(
                    isinstance(candidate, ast.List)
                    for candidate in origins.get(origin, {}).get(expression.id, [])
                ):
                    uncertain_origins.add((origin, expression.id))
    for origin, name in uncertain_origins:
        for reader, bindings in owners.items():
            if bindings.get(name) is origin and origin is not None:
                tables[reader][0].pop(name, None)
                tables[reader][2].add(name)
    return scopes, tables, origins


def _loader_summaries(trees, tables_by_file):
    """Only a unique base / (formal + suffix) or base.with_name(formal + suffix)."""
    summaries = {}
    for path, tree in trees.items():
        scopes, tables, _ = tables_by_file[path]
        module_values, _, module_ambiguous = tables[tree]
        mutable_globals = {
            name
            for node in ast.walk(tree)
            if isinstance(node, ast.Global)
            for name in node.names
        }
        for function in tree.body:
            if (
                not isinstance(function, ast.FunctionDef)
                or function.name in module_ambiguous
            ):
                continue
            args = (
                function.args.posonlyargs
                + function.args.args
                + function.args.kwonlyargs
            )
            if len(args) != 1 or function.args.vararg or function.args.kwarg:
                continue
            formal = args[0].arg
            values, aliases, ambiguous = tables[function]
            if formal in ambiguous:
                continue

            def expand(node, seen=frozenset()):
                defined_values, _, defined_ambiguous = tables[
                    scopes.get(node, function)
                ]
                binding_key = (scopes.get(node, function), getattr(node, "id", None))
                if (
                    isinstance(node, ast.Name)
                    and node.id in defined_values
                    and node.id not in defined_ambiguous
                    and binding_key not in seen
                ):
                    return expand(defined_values[node.id], seen | {binding_key})
                return node

            def base_value(node, seen=frozenset()):
                if isinstance(node, ast.Name):
                    if node.id == "__file__":
                        return path
                    if (
                        node.id in module_values
                        and node.id not in mutable_globals
                        and values.get(node.id) is module_values[node.id]
                        and node.id not in seen
                    ):
                        return base_value(module_values[node.id], seen | {node.id})
                if isinstance(node, ast.Call):
                    if (
                        isinstance(node.func, ast.Name)
                        and aliases.get(node.func.id, node.func.id)
                        in ("Path", "pathlib.Path")
                        and len(node.args) == 1
                        and not node.keywords
                    ):
                        return base_value(node.args[0], seen)
                    if isinstance(node.func, ast.Attribute) and node.func.attr in (
                        "resolve",
                        "absolute",
                    ):
                        return base_value(node.func.value, seen)
                if isinstance(node, ast.Attribute) and node.attr == "parent":
                    base = base_value(node.value, seen)
                    return (
                        _reference_path(base).parent.as_posix()
                        if base is not None
                        else None
                    )
                return None

            candidates = []
            for call in ast.walk(function):
                if (
                    scopes.get(call) is not function
                    or not isinstance(call, ast.Call)
                    or len(call.args) < 2
                ):
                    continue
                if (
                    not isinstance(call.func, ast.Attribute)
                    or call.func.attr != "spec_from_file_location"
                ):
                    continue
                target = expand(call.args[1])
                mode = None
                if isinstance(target, ast.BinOp) and isinstance(target.op, ast.Div):
                    base, leaf, mode = target.left, target.right, "join"
                elif (
                    isinstance(target, ast.Call)
                    and isinstance(target.func, ast.Attribute)
                    and target.func.attr == "with_name"
                    and len(target.args) == 1
                ):
                    base, leaf, mode = target.func.value, target.args[0], "with_name"
                if mode is None:
                    continue
                leaf = expand(leaf)
                if not (
                    isinstance(leaf, ast.BinOp)
                    and isinstance(leaf.op, ast.Add)
                    and isinstance(leaf.left, ast.Name)
                    and leaf.left.id == formal
                    and isinstance(leaf.right, ast.Constant)
                    and leaf.right.value == ".py"
                ):
                    continue
                resolved = base_value(base)
                if resolved is not None:
                    candidates.append((call, formal, resolved, mode))
            if len(candidates) == 1:
                summaries[path, function.name] = candidates[0]
    return summaries


def _dependency_graph(repo: Path, roots: list[str], changed: list[str], suffixes=None):
    """Resolve Python syntax to file identities; opaque loaders remain explicit issues."""
    paths = {
        p.relative_to(repo).as_posix()
        for root in roots
        for p in _root_files(repo, root)
        if p.is_file() and _safe_relative(repo, p.relative_to(repo).as_posix())
    }
    paths.update(_safe_relative(repo, p) for p in changed)
    modules: dict[str, set[str]] = {}
    for path in paths:
        if not path.endswith(".py"):
            continue
        for root in ["."] + roots:
            if root != "." and not _within_root(path, root):
                continue
            module_root = (
                Path(root).parent
                if (repo / root).is_file() or root == path
                else Path(root)
            )
            relative = (
                Path(path) if root == "." else Path(path).relative_to(module_root)
            )
            parts = list(relative.with_suffix("").parts)
            if parts[-1] == "__init__":
                parts.pop()
            modules.setdefault(".".join(parts), set()).add(path)
    reverse: dict[str, set[str]] = {}
    issues: list[dict] = [
        {
            "file": path,
            "reason": "unsupported dependency language; owner mapping required",
        }
        for path in sorted(paths)
        if Path(path).suffix in (suffixes or [".py"]) and Path(path).suffix != ".py"
    ]
    trees = {}
    scopes_by_file = {}
    import_candidates_by_file = {}
    for path in sorted(paths):
        if path.endswith(".py") and (repo / path).is_file():
            try:
                trees[path] = ast.parse((repo / path).read_text(encoding="utf-8"))
                import_candidates_by_file[path] = {}
                scopes_by_file[path] = _lexical_scopes(
                    trees[path],
                    ".".join(Path(path).parent.parts),
                    import_candidates=import_candidates_by_file[path],
                )
            except (SyntaxError, UnicodeError):
                issues.append(
                    {
                        "file": path,
                        "reason": "Python syntax unavailable; owner mapping required",
                    }
                )
    summaries = _loader_summaries(trees, scopes_by_file)
    producers = (
        *STDLIB_LOADERS,
        *SUBPROCESS_CALLS,
    )

    def possible_producer(candidates, suffix, names):
        for origin in candidates:
            name = ".".join((origin, *suffix))
            if name in names:
                return True
        return False

    for consumer in sorted(paths):
        if not consumer.endswith(".py") or not (repo / consumer).is_file():
            continue
        current_line = 0

        def issue(reason):
            if current_line and " at line " not in reason:
                reason += f" at line {current_line}"
            item = {"file": consumer, "reason": reason}
            if item not in issues:
                issues.append(item)

        def edge(target):
            reverse.setdefault(target, set()).add(consumer)

        def module(name, *, execute=False):
            candidates = modules.get(name, set())
            if len(candidates) > 1:
                issue("ambiguous local module: " + name)
            for target in candidates:
                edge(target)
            if execute and (
                not candidates or any(p.endswith("/__init__.py") for p in candidates)
            ):
                # run_module/package -m execute __main__; a normal import does not.
                # The module index includes changed tombstones and namespace packages.
                main_candidates = modules.get(name + ".__main__", set())
                if len(main_candidates) > 1:
                    issue("ambiguous package entrypoint: " + name)
                for target in main_candidates:
                    edge(target)
            for i in range(1, len(name.split("."))):
                for target in modules.get(".".join(name.split(".")[:i]), set()):
                    if target.endswith("/__init__.py"):
                        edge(target)

        def filename(value, *, static_path=False):
            if not isinstance(value, str):
                return False
            if PureWindowsPath(value).drive:
                return False
            value = _reference_path(value).as_posix()
            if ".." in _reference_path(value).parts:
                # Arbitrary strings (including negative-test data) are not path
                # expressions. Only an evaluated path can establish containment.
                if not static_path:
                    return False
                try:
                    resolved = (repo / value).resolve()
                    value = resolved.relative_to(repo.resolve()).as_posix()
                except ValueError:
                    # A statically known external read is not a local producer.
                    return False
                except (OSError, RuntimeError):
                    issue("static file path cannot be resolved")
                    return False
            candidates = {value} & paths
            if not candidates and "/" not in value and _reference_path(value).suffix:
                candidates = {p for p in paths if Path(p).name == value}
            if len(candidates) > 1:
                issue("ambiguous file reference: " + value)
            for target in candidates:
                edge(target)
            return bool(candidates)

        def binding_producer(
            owner, symbol, suffix=(), seen=frozenset(), *, scope=None, names=producers
        ):
            if owner not in scopes_by_file:
                return False
            scope = trees[owner] if scope is None else scope
            key = owner, scope, symbol
            if key in seen:
                return False
            definition_scopes, tables, origins = scopes_by_file[owner]
            exported = tables[scope][1].get(symbol, "")
            expressions = origins[scope].get(symbol, [])
            if symbol == "__import__" and not expressions:
                exported = symbol
            candidates = (
                import_candidates_by_file[owner].get(scope, {}).get(symbol, set())
            )
            candidates = candidates | {exported}
            visited = seen | {key}
            if possible_producer(candidates, suffix, names) or any(
                loader_reexport(
                    ".".join((candidate, *suffix)), seen=visited, names=names
                )
                for candidate in candidates
                if candidate
            ):
                return True
            # Follow only stored Name/Attribute origins in their defining scope.
            # A possible producer requires mapping; nothing is imported or run.
            for expression in expressions:
                attributes = []
                while isinstance(expression, ast.Attribute):
                    attributes.insert(0, expression.attr)
                    expression = expression.value
                if isinstance(expression, ast.Name) and binding_producer(
                    owner,
                    expression.id,
                    (*attributes, *suffix),
                    visited,
                    scope=definition_scopes.get(expression, trees[owner]),
                    names=names,
                ):
                    return True
            return False

        def loader_reexport(name, *, seen=frozenset(), names=producers):
            parts = name.split(".")
            for position in range(1, len(parts)):
                for owner in modules.get(".".join(parts[:position]), set()):
                    if binding_producer(
                        owner, parts[position], parts[position + 1 :], seen, names=names
                    ):
                        return True
            return False

        if consumer not in trees:
            continue
        tree = trees[consumer]
        node_scopes, scope_tables, command_origins = scopes_by_file[consumer]
        candidate_scopes = import_candidates_by_file[consumer]
        # Docstrings are documentation, not executable file references.
        for node in ast.walk(tree):
            if isinstance(
                node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
            ):
                if (
                    node.body
                    and isinstance(node.body[0], ast.Expr)
                    and isinstance(node.body[0].value, ast.Constant)
                    and isinstance(node.body[0].value.value, str)
                ):
                    node.body = node.body[1:]
        values, aliases, ambiguous = scope_tables[tree]

        def set_scope(node):
            nonlocal values, aliases, ambiguous, current_line
            current_line = getattr(node, "lineno", 0)
            values, aliases, ambiguous = scope_tables[node_scopes.get(node, tree)]

        def bound_expression(node):
            if not isinstance(node, ast.Name):
                return None
            scope = node_scopes.get(node, tree)
            defined_values, _, defined_ambiguous = scope_tables[scope]
            if isinstance(scope, ast.ClassDef):
                if node.id in defined_values or node.id in defined_ambiguous:
                    issue("class body binding value requires owner mapping")
                return None
            return None if node.id in defined_ambiguous else defined_values.get(node.id)

        def resolved_expression(node):
            visited = set()
            while isinstance(node, ast.Name) and node not in visited:
                visited.add(node)
                assigned = bound_expression(node)
                if assigned is None:
                    break
                node = assigned
            return node

        def subprocess_argument(node):
            keywords = [kw.value for kw in node.keywords if kw.arg == "args"]
            if len(keywords) > 1 or (node.args and keywords):
                issue("ambiguous subprocess args")
                return None
            return node.args[0] if node.args else (keywords[0] if keywords else None)

        def dotted(node, seen=frozenset(), *, suffix=(), executable=False):
            scope = node_scopes.get(node, tree)
            defined_values, defined_aliases, defined_ambiguous = scope_tables[scope]
            if isinstance(node, ast.Name):
                if node.id in defined_ambiguous and binding_producer(
                    consumer,
                    node.id,
                    suffix,
                    scope=scope,
                    names=("sys.executable",) if executable else producers,
                ):
                    if executable:
                        return None
                    issue(
                        "ambiguous imported producer requires owner mapping: " + node.id
                    )
                assigned = bound_expression(node)
                binding_key = (node_scopes.get(node, tree), node.id)
                if (
                    node.id not in defined_aliases
                    and binding_key not in seen
                    and isinstance(assigned, (ast.Name, ast.Attribute))
                ):
                    return dotted(
                        assigned,
                        seen | {binding_key},
                        suffix=suffix,
                        executable=executable,
                    )
                return defined_aliases.get(node.id, node.id)
            if isinstance(node, ast.Attribute):
                base = dotted(
                    node.value, seen, suffix=(node.attr, *suffix), executable=executable
                )
                return None if base is None else base + "." + node.attr
            return ""

        embedded_nodes = set()

        def static_with_name(base, leaf):
            try:
                return _reference_path(base).with_name(leaf).as_posix()
            except ValueError:
                issue("invalid static path name requires owner mapping")
                return None

        def value(node, seen=frozenset()):
            defined_values, _, defined_ambiguous = scope_tables[
                node_scopes.get(node, tree)
            ]
            if isinstance(node, ast.Constant):
                return node.value
            if isinstance(node, ast.Name):
                if node.id == "__file__":
                    return None if node in embedded_nodes else consumer
                assigned = bound_expression(node)
                binding_key = (node_scopes.get(node, tree), node.id)
                if assigned is not None and binding_key not in seen:
                    if isinstance(assigned, ast.List):
                        return None
                    return value(assigned, seen | {binding_key})
            if isinstance(node, ast.Call):
                constructor = dotted(node.func)
                if constructor in ("pathlib.Path", "Path") and node.args:
                    if (
                        len(node.args) == 1
                        and not node.keywords
                        and not isinstance(node.args[0], ast.Starred)
                    ):
                        return value(node.args[0], seen)
                    parts = [value(argument, seen) for argument in node.args]
                    if node.keywords or not all(
                        isinstance(part, str) for part in parts
                    ):
                        issue(
                            "unresolved Path constructor arguments require owner mapping"
                        )
                        return None
                    return PurePosixPath(
                        *(_reference_path(part) for part in parts)
                    ).as_posix()
                if constructor == "str" and node.args:
                    return value(node.args[0], seen)
                if isinstance(node.func, ast.Attribute) and node.func.attr in (
                    "resolve",
                    "absolute",
                ):
                    return value(node.func.value, seen)
                if (
                    isinstance(node.func, ast.Attribute)
                    and node.func.attr == "with_name"
                    and node.args
                ):
                    base, leaf = value(node.func.value, seen), value(node.args[0], seen)
                    if isinstance(base, str) and isinstance(leaf, str):
                        return static_with_name(base, leaf)
            if isinstance(node, ast.Attribute) and node.attr == "parent":
                base = value(node.value, seen)
                if isinstance(base, str):
                    return _reference_path(base).parent.as_posix()
            if (
                isinstance(node, ast.Subscript)
                and isinstance(node.value, ast.Attribute)
                and node.value.attr == "parents"
            ):
                base, index = value(node.value.value, seen), value(node.slice, seen)
                if (
                    isinstance(base, str)
                    and isinstance(index, int)
                    and 0 <= index < len(_reference_path(base).parents)
                ):
                    return _reference_path(base).parents[index].as_posix()
            if isinstance(node, (ast.List, ast.Tuple)):
                return [value(item, seen) for item in node.elts]
            if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
                left, right = value(node.left, seen), value(node.right, seen)
                if isinstance(left, str) and isinstance(right, str):
                    return (_reference_path(left) / _reference_path(right)).as_posix()
            return None

        def known_python_command(node, seen=frozenset()):
            if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
                left = known_python_command(node.left, seen)
                if left is not False:
                    return left
                right = known_python_command(node.right, seen)
                return None if right is not False else False
            if isinstance(node, ast.Name):
                scope = node_scopes.get(node, tree)
                key = (scope, node.id)
                if key in seen:
                    return False
                origins = [
                    known_python_command(origin, seen | {key})
                    for origin in command_origins.get(scope, {}).get(node.id, [])
                    if origin is not None
                ]
                return None if None in origins else any(origins)
            if isinstance(node, (ast.List, ast.Tuple)) and node.elts:
                first = node.elts[0]
                if isinstance(first, ast.Starred):
                    return None
                executable = dotted(first, executable=True)
                if executable is None or loader_reexport(
                    executable, names=("sys.executable",)
                ):
                    return None
                literal = value(first)
                return executable == "sys.executable" or (
                    isinstance(literal, str)
                    and _reference_path(literal).name.startswith("python")
                )
            return False

        def subprocess_python_call(call, argument):
            if any(
                kw.arg == "shell"
                and not (isinstance(kw.value, ast.Constant) and not kw.value.value)
                for kw in call.keywords
            ):
                return None
            if any(
                kw.arg == "executable"
                and not (isinstance(kw.value, ast.Constant) and kw.value.value is None)
                for kw in call.keywords
            ):
                return None
            return known_python_command(argument)

        deferred_annotations = any(
            isinstance(statement, ast.ImportFrom)
            and statement.module == "__future__"
            and any(alias.name == "annotations" for alias in statement.names)
            for statement in tree.body
        )
        parents = {
            child: parent
            for parent in ast.walk(tree)
            for child in ast.iter_child_nodes(parent)
        }
        # Literal python -c programs are syntax, never executed during planning.
        nodes = list(ast.walk(tree))
        contextual_path_nodes = set()
        for node in list(nodes):
            set_scope(node)
            if (
                not isinstance(node, ast.Call)
                or dotted(node.func) not in SUBPROCESS_CALLS
            ):
                continue
            argument = subprocess_argument(node)
            if argument is None:
                continue
            command = value(argument)
            expression = resolved_expression(argument)
            python = subprocess_python_call(node, argument)
            if python is None:
                issue("ambiguous Python CLI executable")
                continue
            if python and any(kw.arg is None for kw in node.keywords):
                issue("opaque Python CLI keyword arguments")
                continue
            if (
                python
                and isinstance(command, list)
                and isinstance(expression, (ast.List, ast.Tuple))
            ):
                position = 1
                while position < len(command) and command[position] in (
                    "-I",
                    "-S",
                    "-B",
                    "-E",
                    "-s",
                    "-u",
                ):
                    position += 1
                if (
                    position < len(command)
                    and isinstance(command[position], str)
                    and not command[position].startswith("-")
                ):
                    target_expression = expression.elts[position]
                    visited = set()
                    while (
                        isinstance(target_expression, ast.Name)
                        and bound_expression(target_expression) is not None
                        and target_expression not in visited
                    ):
                        visited.add(target_expression)
                        target_expression = bound_expression(target_expression)
                    contextual_path_nodes.update(ast.walk(target_expression))
            if python and isinstance(command, list) and "-c" in command:
                index = command.index("-c") + 1
                if index < len(command) and isinstance(command[index], str):
                    try:
                        embedded_tree = ast.parse(command[index])
                        embedded_scopes, embedded_tables, embedded_origins = (
                            _lexical_scopes(
                                embedded_tree, import_candidates=candidate_scopes
                            )
                        )
                        node_scopes.update(embedded_scopes)
                        scope_tables.update(embedded_tables)
                        command_origins.update(embedded_origins)
                        parsed_nodes = list(ast.walk(embedded_tree))
                        embedded_nodes.update(parsed_nodes)
                        nodes.extend(parsed_nodes)
                        parents.update(
                            {
                                child: parent
                                for parent in ast.walk(embedded_tree)
                                for child in ast.iter_child_nodes(parent)
                            }
                        )
                        for keyword in node.keywords:
                            if keyword.arg == "cwd" and value(keyword.value) != ".":
                                issue(
                                    "embedded Python cwd outside supported repository context"
                                )
                    except SyntaxError:
                        issue("Python CLI syntax unavailable")
                else:
                    issue("opaque Python CLI code")
        for reference in nodes:
            if not isinstance(reference, (ast.Name, ast.Attribute)) or not isinstance(
                reference.ctx, ast.Load
            ):
                continue
            set_scope(reference)
            name = dotted(reference)
            parent = parents.get(reference)
            bound = (
                isinstance(parent, ast.Assign)
                and len(parent.targets) == 1
                and isinstance(parent.targets[0], ast.Name)
                and values.get(parent.targets[0].id) is reference
            )
            called = isinstance(parent, ast.Call) and parent.func is reference
            if name in ("globals", "locals", "builtins.globals", "builtins.locals") or (
                name in ("vars", "builtins.vars")
                and (
                    called
                    and (
                        len(parent.args) != 1
                        or isinstance(parent.args[0], ast.Starred)
                        or parent.keywords
                    )
                    or not called
                    and not bound
                )
            ):
                issue("lexical namespace reflection requires owner mapping: " + name)
            if loader_reexport(name):
                issue("loader re-export requires owner mapping")
            if name in producers and not (called or bound):
                # A deferred type reference is stored as text, not passed as a callable.
                annotation = reference
                container = parent
                if deferred_annotations and reference not in embedded_nodes:
                    while isinstance(container, ast.BinOp) and isinstance(
                        container.op, ast.BitOr
                    ):
                        annotation = container
                        container = parents.get(annotation)
                    if (
                        isinstance(container, ast.arg)
                        and container.annotation is annotation
                        or isinstance(
                            container, (ast.FunctionDef, ast.AsyncFunctionDef)
                        )
                        and container.returns is annotation
                        or isinstance(container, ast.AnnAssign)
                        and container.annotation is annotation
                    ):
                        continue
                issue("opaque loader reference escape: " + name)
            key = (consumer if reference not in embedded_nodes else "", name)
            if "." in name:
                module_name, function_name = name.rsplit(".", 1)
                owners = modules.get(module_name, set())
                if len(owners) == 1:
                    key = (next(iter(owners)), function_name)
            if key not in summaries:
                continue
            parent = parents.get(reference)
            if not (isinstance(parent, ast.Call) and parent.func is reference):
                issue(f"loader callback escape at line {reference.lineno}: " + name)
        # A composed path is one reference with its own origin, not independent
        # references to each string fragment interpreted at repository root.
        composed_paths = {}
        path_fragments = set(contextual_path_nodes)
        for expression in nodes:
            if expression in contextual_path_nodes:
                continue
            if (
                isinstance(expression, ast.BinOp)
                and isinstance(expression.op, ast.Div)
                or isinstance(expression, ast.Call)
                and (
                    dotted(expression.func) == "pathlib.Path"
                    or isinstance(expression.func, ast.Attribute)
                    and expression.func.attr == "with_name"
                )
            ):
                resolved = value(expression)
                if isinstance(resolved, str) and _reference_path(resolved).suffix:
                    composed_paths[expression] = resolved
                    path_fragments.update(
                        child
                        for child in ast.walk(expression)
                        if isinstance(child, ast.Constant)
                    )
        for node in nodes:
            set_scope(node)
            if (
                isinstance(node, ast.Name)
                and isinstance(node.ctx, ast.Load)
                and isinstance(node_scopes.get(node), ast.ClassDef)
                and node.id in ambiguous
            ):
                issue("class body binding value requires owner mapping")
            if isinstance(
                node, (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)
            ):
                if isinstance(node_scopes.get(node), ast.ClassDef):
                    # Class comprehensions have a separate lexical namespace,
                    # which this bounded extractor does not resolve.
                    issue("class comprehension requires owner mapping")
            if node in composed_paths:
                filename(composed_paths[node], static_path=True)
            if isinstance(node, ast.Import):
                for entry in node.names:
                    module(entry.name)
            elif isinstance(node, ast.ImportFrom):
                if node.level and node in embedded_nodes:
                    issue("embedded relative import package is unknown")
                    continue
                prefix = node.module or ""
                if node.level:
                    package = ".".join(Path(consumer).parent.parts)
                    try:
                        prefix = importlib.util.resolve_name(
                            "." * node.level + prefix, package
                        )
                    except (ImportError, ValueError):
                        issue("relative import package is unknown")
                        continue
                module(prefix)
                for entry in node.names:
                    if entry.name == "*" and prefix in (
                        "importlib",
                        "importlib.util",
                        "runpy",
                        "subprocess",
                    ):
                        issue("loader star import requires owner mapping")
                    module(prefix + "." + entry.name)
                    for owner in modules.get(prefix, set()):
                        if owner not in scopes_by_file:
                            continue
                        exports = scopes_by_file[owner][1][trees[owner]][1]
                        export_names = set(exports) | set(
                            scopes_by_file[owner][2][trees[owner]]
                        )
                        if entry.name == "*" and (
                            any(key[0] == owner for key in summaries)
                            or any(
                                binding_producer(owner, name)
                                or exports.get(name)
                                in ("importlib", "importlib.util", "runpy")
                                for name in export_names
                            )
                        ):
                            issue("loader star import requires owner mapping")
                        exported = exports.get(entry.name, "")
                        if binding_producer(owner, entry.name):
                            issue("loader re-export requires owner mapping")
                        if "." in exported:
                            origin, symbol = exported.rsplit(".", 1)
                            if any(
                                (source, symbol) in summaries
                                for source in modules.get(origin, set())
                            ):
                                issue("loader re-export requires owner mapping")
            elif (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and node not in path_fragments
            ):
                filename(node.value)
            elif isinstance(node, ast.Call):
                call = dotted(node.func)
                if call == "builtins.__import__":
                    call = "__import__"
                if isinstance(node.func, ast.Attribute) and node.func.attr in (
                    "read_text",
                    "read_bytes",
                    "open",
                ):
                    target_path = value(node.func.value)
                    if isinstance(target_path, str):
                        filename(target_path, static_path=True)
                elif call == "open" and node.args:
                    target_path = value(node.args[0])
                    if isinstance(target_path, str):
                        filename(target_path, static_path=True)
                if (
                    node in embedded_nodes
                    and call.endswith(
                        (
                            ".import_module",
                            ".run_path",
                            ".run_module",
                            ".spec_from_file_location",
                        )
                    )
                    and call
                    not in (
                        "importlib.import_module",
                        "runpy.run_path",
                        "runpy.run_module",
                        "importlib.util.spec_from_file_location",
                    )
                ):
                    issue("opaque embedded loader alias: " + call)
                    continue
                summary_key = (consumer if node not in embedded_nodes else "", call)
                if summary_key not in summaries and "." in call:
                    owner_module, function_name = call.rsplit(".", 1)
                    owners = modules.get(owner_module, set())
                    if len(owners) == 1:
                        summary_key = (next(iter(owners)), function_name)
                if summary_key in summaries:
                    _, formal, base, mode = summaries[summary_key]
                    root_name = node.func
                    while isinstance(root_name, ast.Attribute):
                        root_name = root_name.value
                    actual = (
                        node.args[0]
                        if len(node.args) == 1
                        else next(
                            (kw.value for kw in node.keywords if kw.arg == formal), None
                        )
                    )
                    target = value(actual)
                    if (
                        (
                            summary_key[0] == consumer
                            and "." not in call
                            and not isinstance(values.get(call), ast.FunctionDef)
                        )
                        or (
                            isinstance(root_name, ast.Name)
                            and root_name.id in ambiguous
                        )
                        or not isinstance(target, str)
                        or not target.isidentifier()
                    ):
                        issue(f"opaque loader call at line {node.lineno}: " + call)
                    else:
                        path = (
                            (_reference_path(base) / (target + ".py")).as_posix()
                            if mode == "join"
                            else static_with_name(base, target + ".py")
                        )
                        if not filename(path):
                            issue(
                                f"unresolved loader call at line {node.lineno}: " + call
                            )
                    continue
                if any(node is summary[0] for summary in summaries.values()):
                    continue

                if call == "exec" and node.args:
                    expression = node.args[0]
                    visited = set()
                    while (
                        isinstance(expression, ast.Name)
                        and bound_expression(expression) is not None
                        and expression.id not in visited
                    ):
                        visited.add(expression.id)
                        expression = bound_expression(expression)
                    if (
                        isinstance(expression, ast.Call)
                        and dotted(expression.func) == "compile"
                        and expression.args
                    ):
                        expression = expression.args[0]
                        while (
                            isinstance(expression, ast.Name)
                            and bound_expression(expression) is not None
                            and expression.id not in visited
                        ):
                            visited.add(expression.id)
                            expression = bound_expression(expression)
                        if (
                            isinstance(expression, ast.Call)
                            and isinstance(expression.func, ast.Attribute)
                            and expression.func.attr in ("read_bytes", "read_text")
                        ):
                            source = value(expression.func.value)
                            if isinstance(source, str) and filename(source):
                                continue
                index = 1 if call.endswith(".spec_from_file_location") else 0
                if call in (
                    "__import__",
                    "importlib.import_module",
                    "runpy.run_module",
                    "runpy.run_path",
                    "importlib.util.spec_from_file_location",
                    "exec",
                    "eval",
                ):
                    root = node.func
                    while isinstance(root, ast.Attribute):
                        root = root.value
                    if isinstance(root, ast.Name) and root.id in ambiguous:
                        issue(f"shadowed loader binding at line {node.lineno}: " + call)
                        continue
                    target = value(node.args[index]) if len(node.args) > index else None
                    if (
                        target is None
                        and call in ("importlib.import_module", "__import__")
                        and node.args
                        and isinstance(node.args[0], ast.Name)
                    ):
                        actual = node.args[0].id
                        ancestor = parents.get(node)
                        while ancestor is not None and not isinstance(
                            ancestor, (ast.For, ast.FunctionDef, ast.AsyncFunctionDef)
                        ):
                            ancestor = parents.get(ancestor)
                        if isinstance(ancestor, ast.For):
                            loop_target = ancestor.target
                            iterable = ancestor.iter
                            items = (
                                isinstance(iterable, ast.Call)
                                and isinstance(iterable.func, ast.Attribute)
                                and iterable.func.attr == "items"
                                and not iterable.args
                            )
                            if items:
                                iterable = iterable.func.value
                                if (
                                    isinstance(loop_target, (ast.Tuple, ast.List))
                                    and loop_target.elts
                                ):
                                    loop_target = loop_target.elts[0]
                            binding_name = (
                                iterable.id if isinstance(iterable, ast.Name) else None
                            )
                            literal = (
                                bound_expression(iterable) if binding_name else iterable
                            )
                            if (
                                isinstance(loop_target, ast.Name)
                                and loop_target.id == actual
                                and isinstance(literal, (ast.Dict, ast.Tuple))
                            ):
                                entries = (
                                    literal.keys
                                    if isinstance(literal, ast.Dict)
                                    else literal.elts
                                )
                                names = [
                                    entry.value
                                    for entry in entries
                                    if isinstance(entry, ast.Constant)
                                    and isinstance(entry.value, str)
                                ]
                                reassigned = any(
                                    isinstance(part, ast.Name)
                                    and part.id == actual
                                    and isinstance(part.ctx, ast.Store)
                                    for statement in ancestor.body
                                    for part in ast.walk(statement)
                                )
                                escaped = False
                                if isinstance(literal, ast.Dict) and binding_name:
                                    for reference in ast.walk(tree):
                                        if (
                                            isinstance(reference, ast.Name)
                                            and reference.id == binding_name
                                            and isinstance(reference.ctx, ast.Load)
                                        ):
                                            parent = parents.get(reference)
                                            if not (
                                                isinstance(parent, ast.For)
                                                and parent.iter is reference
                                                or isinstance(parent, ast.Subscript)
                                                and isinstance(parent.ctx, ast.Load)
                                                or isinstance(parent, ast.Attribute)
                                                and parent.attr in ("items", "keys")
                                            ):
                                                escaped = True
                                if (
                                    len(names) == len(entries)
                                    and not reassigned
                                    and not escaped
                                ):
                                    for name in names:
                                        module(name)
                                    continue
                    if (
                        target is None
                        and call.endswith(("run_path", "spec_from_file_location"))
                        and len(node.args) > index
                    ):
                        # A computed base with a literal local filename still identifies
                        # a dependency; follow only the target expression's assignments.
                        expression = node.args[index]
                        visited = set()
                        while (
                            isinstance(expression, ast.Name)
                            and bound_expression(expression) is not None
                            and expression.id not in visited
                        ):
                            visited.add(expression.id)
                            expression = bound_expression(expression)
                        literals = {
                            part.value
                            for part in ast.walk(expression)
                            if isinstance(part, ast.Constant)
                            and isinstance(part.value, str)
                            and Path(part.value).suffix == ".py"
                        }
                        candidates = {
                            path
                            for path in paths
                            for literal in literals
                            if path == literal
                            or ("/" not in literal and Path(path).name == literal)
                        }
                        if len(candidates) == 1:
                            target = next(iter(candidates))
                        elif not candidates and len(literals) == 1:
                            literal = next(iter(literals))
                            # An unknown prefix cannot exclude a known local
                            # basename just because the partial path differs.
                            if not any(
                                _reference_path(path).name
                                == _reference_path(literal).name
                                for path in paths
                            ):
                                target = literal
                    if call in ("exec", "eval") or not isinstance(target, str):
                        issue("opaque loader: " + call)
                    elif call.endswith(("run_path", "spec_from_file_location")):
                        if ".." in _reference_path(target).parts:
                            issue("loader path escape requires owner mapping: " + call)
                        else:
                            filename(target)
                    else:
                        if target.startswith("."):
                            package_arg = (
                                node.args[1]
                                if len(node.args) > 1
                                else next(
                                    (
                                        kw.value
                                        for kw in node.keywords
                                        if kw.arg == "package"
                                    ),
                                    None,
                                )
                            )
                            package = value(package_arg)
                            if (
                                call != "importlib.import_module"
                                or not isinstance(package, str)
                                or not package
                            ):
                                issue("opaque relative import package")
                                continue
                            try:
                                target = importlib.util.resolve_name(target, package)
                            except (ImportError, ValueError):
                                issue("invalid relative import package")
                                continue
                        module(target, execute=call == "runpy.run_module")
                        if call == "__import__":
                            fromlist_arg = (
                                node.args[3]
                                if len(node.args) > 3
                                else next(
                                    (
                                        kw.value
                                        for kw in node.keywords
                                        if kw.arg == "fromlist"
                                    ),
                                    ast.Constant(value=()),
                                )
                            )
                            level_arg = (
                                node.args[4]
                                if len(node.args) > 4
                                else next(
                                    (
                                        kw.value
                                        for kw in node.keywords
                                        if kw.arg == "level"
                                    ),
                                    ast.Constant(value=0),
                                )
                            )
                            imported = value(fromlist_arg)
                            if (
                                value(level_arg) != 0
                                or not isinstance(imported, (list, tuple))
                                or not all(
                                    isinstance(item, str) and item != "*"
                                    for item in imported
                                )
                            ):
                                issue("opaque __import__ fromlist or level")
                            else:
                                for item in imported:
                                    module(target + "." + item)
                elif call in SUBPROCESS_CALLS:
                    argument = subprocess_argument(node)
                    if argument is None:
                        continue
                    python = subprocess_python_call(node, argument)
                    if python is None:
                        issue("ambiguous Python CLI executable")
                        continue
                    command = value(argument)
                    if not isinstance(command, list) or not command:
                        if python:
                            issue("opaque Python CLI command construction")
                        continue
                    if python:
                        if any(kw.arg is None for kw in node.keywords):
                            issue("opaque Python CLI keyword arguments")
                            continue
                        if node in embedded_nodes and "-c" in command:
                            issue(
                                "nested embedded Python context requires owner mapping"
                            )
                        cwd_arg = next(
                            (kw.value for kw in node.keywords if kw.arg == "cwd"),
                            ast.Constant(value="."),
                        )
                        cwd = value(cwd_arg)
                        if (
                            not isinstance(cwd, str)
                            or (
                                _reference_path(cwd).is_absolute()
                                or bool(PureWindowsPath(cwd).drive)
                            )
                            or ".." in _reference_path(cwd).parts
                        ):
                            issue("opaque Python CLI cwd")
                            continue
                        position = 1
                        while position < len(command) and command[position] in (
                            "-I",
                            "-S",
                            "-B",
                            "-E",
                            "-s",
                            "-u",
                        ):
                            position += 1
                        command = command[:1] + command[position:]
                        if len(command) < 2 or command[1] is None:
                            issue("opaque Python CLI target")
                        elif command[1] == "-m":
                            if len(command) > 2 and isinstance(command[2], str):
                                if cwd not in ("", "."):
                                    issue(
                                        "Python CLI module cwd requires owner mapping"
                                    )
                                else:
                                    module(command[2], execute=True)
                            else:
                                issue("opaque Python CLI module")
                        elif command[1] != "-c":
                            if command[1] in ("-V", "--version", "-h", "--help"):
                                continue
                            if isinstance(command[1], str) and command[1].startswith(
                                "-"
                            ):
                                issue("unsupported Python CLI option")
                                continue
                            if (
                                isinstance(command[1], str)
                                and ".." not in Path(command[1]).parts
                            ):
                                filename(
                                    (
                                        _reference_path(cwd)
                                        / _reference_path(command[1])
                                    ).as_posix()
                                )
                            else:
                                issue("unresolved Python CLI file")
    return reverse, issues


def _dependents(reverse, seed):
    found = {seed}
    pending = [seed]
    while pending:
        for consumer in reverse.get(pending.pop(), set()) - found:
            found.add(consumer)
            pending.append(consumer)
    return found


def select_tests(repo: Path, files: list[str], config: dict) -> dict:
    """Return paths, per-file evidence and unresolved mappings; never execute policy text."""
    if "schema" in config and config["schema"] != "test-selection-policy/v1":
        raise ValueError("unknown selection policy schema")
    policy = {**DEFAULT_SELECTION, **config}
    for key in ("source_roots", "test_roots", "test_patterns", "code_suffixes"):
        if not isinstance(policy[key], list) or not all(
            isinstance(x, str) for x in policy[key]
        ):
            raise ValueError(f"{key} must be a list of strings")
    roots = policy["source_roots"] + policy["test_roots"]
    for root in roots:
        _safe_relative(repo, root)
    if not policy["test_patterns"] or not policy["test_roots"]:
        raise ValueError("test roots and patterns must be nonempty")
    all_tests = sorted(
        {
            _safe_relative(repo, p.relative_to(repo).as_posix())
            for root in policy["test_roots"]
            for p in _root_files(repo, root)
            if p.is_file()
            and any(fnmatch.fnmatchcase(p.name, pat) for pat in policy["test_patterns"])
        }
    )
    rules = policy["rules"]
    if not isinstance(rules, list):
        raise ValueError("rules must be a list")
    for rule in rules:
        if not isinstance(rule, dict) or not rule.get("changes"):
            raise ValueError("rules require changes")
        for key in ("changes", "tests", "checks"):
            values = rule.get(key, [])
            if not isinstance(values, list) or not all(
                isinstance(x, str) for x in values
            ):
                raise ValueError(f"rule {key} must be a list of strings")
        if rule.get("dependency_mode", "auto") not in ("auto", "mapped", "supplement"):
            raise ValueError("rule dependency_mode must be auto, mapped or supplement")
        if "no_tests" in rule and not isinstance(rule["no_tests"], bool):
            raise ValueError("rule no_tests must be a boolean")
        if any(
            not value.strip() or value != value.strip()
            for value in rule.get("checks", [])
        ):
            raise ValueError(
                "rule check IDs must be nonempty without boundary whitespace"
            )
        for pattern in rule["changes"] + rule.get("tests", []):
            _safe_relative(repo, pattern)
    reverse, dependency_issues = _dependency_graph(
        repo, roots, files, policy["code_suffixes"]
    )
    reasons: dict[str, list[dict]] = {}
    file_reasons: dict[str, list[str]] = {}
    unresolved: list[dict] = []
    checks: set[str] = set()
    affected: dict[str, set[str]] = {}

    def add(test: str, name: str, reason: str):
        reasons.setdefault(test, []).append({"file": name, "reason": reason})
        file_reasons[name].append(reason)

    for raw_name in sorted(set(files)):
        name = _safe_relative(repo, raw_name)
        file_reasons[name] = []
        if Path(name).name == "conftest.py":
            unresolved.append(
                {
                    "file": name,
                    "reason": "fixture-impact: implicit pytest consumers require owner scope review",
                }
            )
            continue
        matched = False
        dependency_modes: list[str] = []
        unresolved_before_rules = len(unresolved)
        for index, rule in enumerate(rules):
            if not any(
                fnmatch.fnmatchcase(name, pattern) for pattern in rule["changes"]
            ):
                continue
            if "toml_only_keys" in rule:
                unresolved.append(
                    {
                        "file": name,
                        "reason": "TOML condition requires base-bound plan evaluation",
                    }
                )
                continue
            matched = True
            mode = rule.get("dependency_mode", "auto")
            if mode != "supplement":
                dependency_modes.append(mode)
            checks.update(rule.get("checks", []))
            hits = [
                t
                for t in all_tests
                if any(
                    fnmatch.fnmatchcase(t, pattern) for pattern in rule.get("tests", [])
                )
            ]
            for test in hits:
                add(test, name, f"policy rule {index}")
            missing_patterns = [
                pat
                for pat in rule.get("tests", [])
                if not any(fnmatch.fnmatchcase(t, pat) for t in all_tests)
            ]
            if missing_patterns:
                unresolved.append(
                    {
                        "file": name,
                        "reason": f"policy rule {index} matched no tests for: "
                        + ", ".join(missing_patterns),
                    }
                )
            elif not hits and not rule.get("checks") and not rule.get("no_tests"):
                unresolved.append(
                    {"file": name, "reason": f"policy rule {index} has no disposition"}
                )
            else:
                file_reasons[name].append(f"policy rule {index}")
        if name in all_tests:
            add(name, name, "changed test")
        if (
            dependency_modes
            and all(mode == "mapped" for mode in dependency_modes)
            and len(unresolved) == unresolved_before_rules
        ):
            file_reasons[name].append(
                "owner mapped dependency scope; automatic closure omitted"
            )
            continue
        in_tests = any(_within_root(name, root) for root in policy["test_roots"])
        deleted_test = (
            in_tests
            and name not in all_tests
            and any(
                fnmatch.fnmatchcase(Path(name).name, p) for p in policy["test_patterns"]
            )
        )
        if deleted_test:
            file_reasons[name].append("deleted test; inspect remaining consumers")
        if (
            Path(name).suffix in policy["code_suffixes"]
            and not any(_within_root(name, root) for root in roots)
            and not matched
        ):
            unresolved.append(
                {
                    "file": name,
                    "reason": "code outside owner-declared roots; explicit mapping required",
                }
            )
            continue
        affected[name] = _dependents(reverse, name)
        hits = sorted(set(all_tests) & affected[name])
        for test in hits:
            add(test, name, "Python syntax file dependency")
        for issue in dependency_issues:
            if issue not in unresolved:
                unresolved.append(issue)
        if not hits and not matched and name not in all_tests and not deleted_test:
            unresolved.append(
                {
                    "file": name,
                    "reason": "no dependency or explicit mapping; owner review required",
                }
            )
    # An unresolved incoming edge cannot enlarge a consumer already reached by
    # this change. Other opaque consumers remain unknown, even with no tests.
    covered = set()
    for closure in affected.values():
        covered.update(closure)
    # Manual tests are covered only when their known downstream tail is covered
    # too (a test module can itself be imported as a helper by another test).
    available = covered | set(reasons)
    covered.update(test for test in reasons if _dependents(reverse, test) <= available)
    remaining = []
    for issue in unresolved:
        if (
            issue in dependency_issues
            and issue["file"] in covered
            and "path escape" not in issue["reason"]
        ):
            for name in file_reasons:
                if issue["file"] in affected.get(name, set()) or any(
                    item["file"] == name for item in reasons.get(issue["file"], [])
                ):
                    evidence = (
                        "dependency uncertainty already covered: " + issue["file"]
                    )
                    if evidence not in file_reasons[name]:
                        file_reasons[name].append(evidence)
        else:
            remaining.append(issue)
    unresolved = remaining
    return {
        "schema": "test-selection/v1",
        "status": (
            "unresolved" if unresolved else ("selected" if reasons else "no_tests")
        ),
        "changed_files": sorted(file_reasons),
        "selected_tests": sorted(reasons),
        "reasons": reasons,
        "file_reasons": file_reasons,
        "checks": sorted(checks),
        "unresolved": unresolved,
        **({"policy_tests": all_tests} if unresolved else {}),
    }


def _toml_scoped_config(repo: Path, files: list[str], config: dict, base_oid: str):
    """Prove allowed TOML fields against Git base, without trusting policy flags."""
    effective = {**config, "rules": []}
    evidence = []
    rules = config.get("rules", [])
    if not isinstance(rules, list):
        raise ValueError("rules must be a list")
    for rule in rules:
        if not isinstance(rule, dict):
            raise ValueError("rules require objects")
        if "toml_only_keys" not in rule:
            effective["rules"].append(rule)
            continue
        keys = rule["toml_only_keys"]
        if (
            not isinstance(keys, list)
            or not keys
            or not all(
                isinstance(key, str)
                and all(part.isidentifier() for part in key.split("."))
                for key in keys
            )
        ):
            raise ValueError("toml_only_keys must contain dotted TOML keys")
        patterns = rule.get("changes")
        if (
            not isinstance(patterns, list)
            or not patterns
            or not all(isinstance(pattern, str) for pattern in patterns)
        ):
            raise ValueError("conditional rule changes must be nonempty strings")
        for pattern in patterns:
            _safe_relative(repo, pattern)
        matches = [
            name
            for name in files
            if any(
                fnmatch.fnmatchcase(name, pattern)
                for pattern in rule.get("changes", [])
            )
        ]
        verified = []
        for name in matches:
            name = _safe_relative(repo, name)
            record = {
                "file": name,
                "keys": keys,
                "base_oid": base_oid,
                "base_sha256": None,
                "current_sha256": None,
                "matched": False,
            }
            try:
                try:
                    import tomllib
                except ImportError:
                    import tomli as tomllib
                current_path = repo / name
                if current_path.is_symlink() or not current_path.is_file():
                    raise ValueError("conditional TOML requires a regular file")
                tree_entry = subprocess.run(
                    ["git", "ls-tree", base_oid, "--", name],
                    cwd=repo,
                    capture_output=True,
                    check=True,
                ).stdout
                base_mode = tree_entry.split(b" ", 1)[0]
                current_mode = (
                    b"100755" if current_path.stat().st_mode & 0o111 else b"100644"
                )
                if base_mode not in (b"100644", b"100755") or base_mode != current_mode:
                    raise ValueError("conditional TOML mode changed")
                before_bytes = subprocess.run(
                    ["git", "show", base_oid + ":" + name],
                    cwd=repo,
                    capture_output=True,
                    check=True,
                ).stdout
                after_bytes = (repo / name).read_bytes()
                record.update(
                    base_sha256=hashlib.sha256(before_bytes).hexdigest(),
                    current_sha256=hashlib.sha256(after_bytes).hexdigest(),
                )
                before = tomllib.loads(before_bytes.decode("utf-8"))
                after = tomllib.loads(after_bytes.decode("utf-8"))
                for document in (before, after):
                    for key in keys:
                        parts = key.split(".")
                        parent = document
                        for part in parts[:-1]:
                            parent = (
                                parent.get(part) if isinstance(parent, dict) else None
                            )
                        if isinstance(parent, dict):
                            parent.pop(parts[-1], None)

                def same(left, right):
                    if type(left) is not type(right):
                        return False
                    if isinstance(left, dict):
                        return left.keys() == right.keys() and all(
                            same(left[key], right[key]) for key in left
                        )
                    if isinstance(left, list):
                        return len(left) == len(right) and all(
                            same(a, b) for a, b in zip(left, right)
                        )
                    return left == right

                if same(before, after):
                    verified.append(name)
                    record["matched"] = True
            except (ImportError, ValueError, OSError, subprocess.CalledProcessError):
                pass
            evidence.append(record)
        if verified:
            effective["rules"].append(
                {
                    key: value
                    for key, value in {**rule, "changes": verified}.items()
                    if key != "toml_only_keys"
                }
            )
    return effective, evidence


def plan_tests(repo: Path, base: str, config_path: Path | None = None) -> dict:
    config = _selection_config(repo, config_path)
    head_oid = git(repo, "rev-parse", "HEAD").strip()
    base_oid = git(repo, "merge-base", base, head_oid).strip()
    files = changed_files(repo, base_oid)
    effective_config, toml_evidence = _toml_scoped_config(repo, files, config, base_oid)
    plan = select_tests(repo, files, effective_config)
    if toml_evidence:
        for record in toml_evidence:
            if record["current_sha256"] is not None:
                current = repo / _safe_relative(repo, record["file"])
                if (
                    hashlib.sha256(current.read_bytes()).hexdigest()
                    != record["current_sha256"]
                ):
                    raise ValueError("TOML input changed during selection; retry")
        plan["toml_conditions"] = toml_evidence
    if git(repo, "rev-parse", "HEAD").strip() != head_oid:
        raise ValueError("HEAD changed during selection; retry")
    policy_bytes = json.dumps(
        {**DEFAULT_SELECTION, **config}, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    plan.update(
        head_oid=head_oid,
        base_oid=base_oid,
        base_kind="merge-base",
        policy_sha256=hashlib.sha256(policy_bytes).hexdigest(),
        selector_source_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
    )
    return plan


def related_tests(repo: Path, files: list[str]) -> list[str]:
    """Compatibility API: unknown mappings fail closed instead of returning full-suite sentinel."""
    plan = select_tests(repo, files, _selection_config(repo))
    if plan["unresolved"]:
        raise ValueError(
            "unresolved test selection: "
            + ", ".join(x["file"] for x in plan["unresolved"])
        )
    return plan["selected_tests"]


def redact(text: str, repo: Path) -> str:
    """ログに実ホームパス・アカウント名・作業場所の絶対パスを残さない。"""
    pairs = [
        (sys.executable, "<python>"),
        (sys.prefix, "<python-prefix>"),
        (sys.base_prefix, "<python-base>"),
        (str(repo), "<repo>"),
        (str(Path.home()), "~"),
    ]
    try:
        user = getpass.getuser()
    except Exception:  # noqa: BLE001 - 取れない環境では置き換えない
        user = ""
    for old, new in pairs:
        if old and old not in ("/", "."):
            text = text.replace(old, new)
    if len(user) >= 3:
        text = re.sub(
            rf"(?<![A-Za-z0-9_]){re.escape(user)}(?![A-Za-z0-9_])", "<user>", text
        )
    return text


def worktree_dirty(repo: Path) -> bool:
    """まだステージしていない変更か、未追跡のファイルがあるか。"""
    unstaged = git(repo, "diff", "--name-only", "-z")
    untracked = git(repo, "ls-files", "-z", "--others", "--exclude-standard")
    return bool(unstaged.strip("\0") or untracked.strip("\0"))


@contextmanager
def worktree_index_env(repo: Path):
    """作業ツリーの全部（未ステージ・未追跡を含む）を載せた一時の索引を作り、それを指す環境変数を返す。

    本物の索引（ステージの状態）は変えない。identity_gate の --staged は GIT_INDEX_FILE を
    そのまま使うので、この索引を渡すと「HEAD から作業ツリーまで」の足した行とファイル名を見る。
    """
    real = Path(git(repo, "rev-parse", "--git-path", "index").strip())
    if not real.is_absolute():
        real = repo / real
    with tempfile.TemporaryDirectory(prefix="local-check-") as tmp:
        index = Path(tmp) / "index"
        if real.exists():
            # copy2 で時刻も写す。copyfile だと索引の時刻が「今」になり、git の racy 判定
            # （索引と同じ時刻に書き換えたファイルは中身を読み直す）が効かず、同じ大きさで
            # 書き換えたファイルを「変更なし」と見落とす
            shutil.copy2(real, index)
        env = {**os.environ, "GIT_INDEX_FILE": str(index)}
        subprocess.run(
            ["git", "add", "-A"], cwd=repo, env=env, check=True, capture_output=True
        )
        yield env


def identity_worktree(repo: Path) -> int:
    """作業ツリーの全部を identity_gate --public --staged で見る（--identity-worktree の中身）。"""
    with worktree_index_env(repo) as env:
        return subprocess.run(
            [
                sys.executable,
                "scripts/identity_gate.py",
                "--public",
                "--repo",
                ".",
                "--staged",
            ],
            cwd=repo,
            env=env,
        ).returncode


class _Blocked(NamedTuple):
    reason: str
    selected_count: int
    unresolved_count: int
    check_count: int


class _Run(NamedTuple):
    tests: tuple[str, ...]
    workers: str | None
    fallback: bool


def _execution_decision(
    plan,
    *,
    ci=False,
    private=False,
    workers=None,
    allow_fallback=True,
    pytest_only=True,
) -> _Blocked | _Run:
    """Pure boundary: blocked selections never carry executable test paths."""
    counts = (
        len(set(plan["selected_tests"])),
        len(plan["unresolved"]),
        len(plan["checks"]),
    )
    if private:
        return _Blocked("private_selection", *counts)
    if pytest_only and plan["checks"]:
        return _Blocked("consumer_checks_required", *counts)
    unresolved = bool(plan["unresolved"]) or plan.get("status") == "unresolved"
    if unresolved and (ci or not allow_fallback):
        return _Blocked("unresolved_selection", *counts)
    tests = set(plan["selected_tests"])
    if unresolved:
        tests.update(plan.get("policy_tests", []))
        if not tests:
            return _Blocked("unresolved_selection", *counts)
    changed = set(plan.get("changed_files", []))
    return _Run(
        tuple(sorted(tests & changed) + sorted(tests - changed)),
        workers or ("auto" if ci or unresolved else None),
        unresolved,
    )


def _pytest_command(decision: _Run):
    if not isinstance(decision, _Run):
        raise TypeError("pytest requires a runnable selection")
    if not decision.tests:
        return []
    parallel = ["-n", decision.workers] if decision.workers else []
    return [
        sys.executable,
        "-m",
        "pytest",
        "-q",
        "-x",
        *parallel,
        "--",
        *decision.tests,
    ]


def _ci_report(plan, decision):
    """Only fixed vocabulary, counts and validated hashes may leave CI planning."""
    blocked = isinstance(decision, _Blocked)
    report = {
        "schema": "test-selection-ci/v1",
        "selection_status": (
            "unresolved" if blocked else ("selected" if decision.tests else "no_tests")
        ),
        "decision": "blocked" if blocked else ("run" if decision.tests else "skip"),
        "selected_count": len(set(plan["selected_tests"])),
        "unresolved_count": len(plan["unresolved"]),
        "check_count": len(plan["checks"]),
        "fallback": False,
    }
    if blocked:
        report["reason"] = decision.reason
    for key, width in (
        ("head_oid", 40),
        ("base_oid", 40),
        ("policy_sha256", 64),
        ("selector_source_sha256", 64),
    ):
        value = plan.get(key)
        if isinstance(value, str) and re.fullmatch(r"[0-9a-f]{%d}" % width, value):
            report[key] = value
    return report


def _write_ci_summary(report):
    target = os.environ.get("GITHUB_STEP_SUMMARY")
    if target:
        with Path(target).open("a", encoding="utf-8") as summary:
            summary.write("### Test selection\n\n| Field | Value |\n| --- | --- |\n")
            for key, value in report.items():
                rendered = str(value).lower() if isinstance(value, bool) else str(value)
                summary.write(f"| {key} | {rendered} |\n")
            summary.write("\n")


def _emit_ci_report(report, *, json_output):
    written = True
    try:
        _write_ci_summary(report)
    except OSError:
        report = {**report, "decision": "blocked", "reason": "summary_write_failed"}
        written = False
    print(json.dumps(report) if json_output else str(report), flush=True)
    return written


def _pytest_workers(value):
    if value == "auto" or (
        re.fullmatch(r"[1-9][0-9]{0,2}", value) and int(value) <= 256
    ):
        return value
    raise argparse.ArgumentTypeError("expected auto or a worker count from 1 to 256")


def build_steps(repo: Path, mode: str, base: str) -> list[tuple[str, list[str]]]:
    py = sys.executable
    merge_base = git(repo, "merge-base", base, "HEAD").strip()
    # 個人情報ゲート：どのモードでも全差分。絞らない
    identity = [
        py,
        "scripts/identity_gate.py",
        "--public",
        "--repo",
        ".",
        "--range",
        f"{merge_base}..HEAD",
    ]
    staged = git(repo, "diff", "--cached", "--name-only").strip()
    steps: list[tuple[str, list[str]]] = [("identity_gate（全差分）", identity)]
    if staged:
        steps.append(
            (
                "identity_gate（ステージ）",
                [py, "scripts/identity_gate.py", "--public", "--repo", ".", "--staged"],
            )
        )
    if worktree_dirty(repo):
        # 未ステージと未追跡も素通りさせない（一時の索引に載せて見る。本物の索引は変えない）
        steps.append(
            (
                "identity_gate（作業ツリー）",
                [
                    py,
                    "scripts/local_check.py",
                    "--repo",
                    ".",
                    "--identity-worktree",
                ],
            )
        )
    consistency = [
        py,
        "scripts/consistency_gate.py",
        "--repo",
        ".",
        "--base-ref",
        merge_base,
        "--require-config",
        "--require-mode",
        "enforce",
        "--json",
    ]
    readme = [py, "scripts/readme_release_gate.py", "--repo", ".", "--json"]
    if mode != "quick":
        steps += [
            ("ai_ratchet_gate", [py, "-m", "ai_ratchet_gate", "--repo", "."]),
            ("runtime_smoke", [py, "scripts/runtime_smoke.py", "--repo", "."]),
            ("readme_release_gate", readme),
        ]
    plan = plan_tests(repo, base)
    files = plan["changed_files"]
    steps.append(("consistency_gate", consistency))  # 0.1 秒程度で、差分だけを見る
    if mode == "quick" and any(
        f == "README.md" or f == "scripts/readme_release_gate.py" for f in files
    ):
        steps.append(("readme_release_gate", readme))
    py_files = [
        f
        for f in files
        if f.endswith(".py")
        and f.startswith(("scripts/", "tests/"))
        and (repo / f).exists()
    ]
    if py_files:
        steps.append(
            ("black（変えたファイル）", [py, "-m", "black", "--check", *py_files])
        )
    decision = _execution_decision(plan, allow_fallback=False, pytest_only=False)
    if isinstance(decision, _Blocked):
        raise ValueError("unresolved test selection")
    if decision.tests:
        steps.append(
            (
                f"pytest（関係 {len(decision.tests)} ファイル）",
                _pytest_command(decision),
            )
        )
    if mode == "quick" and any(
        f.startswith(("runtime/", "assets/"))
        or f
        in ("SKILL.md", "scripts/runtime_smoke.py", "scripts/install_runtime_skills.py")
        for f in files
    ):
        steps.append(("runtime_smoke", [py, "scripts/runtime_smoke.py", "--repo", "."]))
    return steps


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="backslashreplace")
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument(
        "--mode",
        choices=("selected", "full", "quick"),
        default="selected",
        help="selected（既定）。full / quick は互換名で、試験はすべて同じ差分選択を使う",
    )
    ap.add_argument(
        "--base-ref", default="origin/main", help="比較元（既定 origin/main）"
    )
    ap.add_argument("--repo", type=Path, default=ROOT)
    ap.add_argument(
        "--ci", action="store_true", help="CI: 未解決なら停止し、既定で自動並列実行"
    )
    ap.add_argument("--plan-tests", action="store_true", help="選択planだけを出す")
    ap.add_argument(
        "--run-selected-tests", action="store_true", help="選択したpytestだけを実行"
    )
    ap.add_argument(
        "--pytest-workers",
        type=_pytest_workers,
        help="pytest-xdist worker count (auto or 1-256)",
    )
    ap.add_argument("--selection-config", type=Path, help="対象repo内の選択policy")
    ap.add_argument("--json", action="store_true", help="planをJSONで出す")
    ap.add_argument("--dry-run", action="store_true", help="回す項目だけを出す")
    ap.add_argument(
        "--identity-worktree",
        action="store_true",
        help="内部用: 未ステージ・未追跡を含む作業ツリーの全部を identity_gate で見る",
    )
    ns = ap.parse_args(argv)
    if ns.ci:
        if ns.plan_tests or ns.dry_run or ns.identity_worktree:
            ap.error(
                "--ci conflicts with --plan-tests, --dry-run and --identity-worktree"
            )
        ns.run_selected_tests = True
    if ns.pytest_workers and not ns.run_selected_tests:
        ap.error("--pytest-workers requires --run-selected-tests")
    repo = ns.repo.resolve()
    identities = local_identity_guard.collect_identities(
        repo=repo, public_names={"nexus-ai-2045", "nexus_ai"}
    )
    if ns.identity_worktree:
        return identity_worktree(repo)
    try:
        if ns.plan_tests or ns.run_selected_tests:
            plan = plan_tests(repo, ns.base_ref, ns.selection_config)
            serialized = json.dumps(plan, ensure_ascii=False, indent=2)
            private_plan = bool(
                local_identity_guard.find_hits(serialized, identities)
                or redact(serialized, repo) != serialized
            )
            decision = _execution_decision(
                plan,
                ci=ns.ci,
                private=private_plan,
                workers=ns.pytest_workers,
                allow_fallback=ns.run_selected_tests,
            )
            if ns.ci:
                report = _ci_report(plan, decision)
                if not _emit_ci_report(report, json_output=ns.json):
                    return 2
            else:
                if private_plan:
                    # Redacted paths must never become executable selection inputs.
                    plan = {
                        "schema": "test-selection/v1",
                        "status": "unresolved",
                        "selected_tests": [],
                        "checks": [],
                        "unresolved": [
                            {
                                "reason": "private identity in plan; owner review required"
                            }
                        ],
                    }
                print(
                    (
                        json.dumps(plan, ensure_ascii=False, indent=2)
                        if ns.json
                        else str(plan)
                    ),
                    flush=True,
                )
            if isinstance(decision, _Blocked):
                if (
                    not ns.ci
                    and decision.reason == "consumer_checks_required"
                    and ns.run_selected_tests
                ):
                    print(
                        "停止: check IDs require the consumer runner", file=sys.stderr
                    )
                # A plan-only consumer owns check IDs; they do not block reading its plan.
                if ns.run_selected_tests or private_plan or plan["unresolved"]:
                    return 2
                return 0
            if ns.run_selected_tests:
                command = _pytest_command(decision)
                if decision.fallback:
                    print(
                        "対応が分からない変更があるので、全部の試験を回します",
                        file=sys.stderr,
                    )
                if command:
                    return subprocess.run(
                        command,
                        cwd=repo,
                        stdout=sys.stderr if ns.json or ns.ci else None,
                    ).returncode
            return 0
        if ns.selection_config:
            ap.error("--selection-config requires --plan-tests or --run-selected-tests")
        steps = build_steps(repo, ns.mode, ns.base_ref)
    except (ValueError, OSError) as exc:
        if ns.ci:
            _emit_ci_report(
                {
                    "schema": "test-selection-ci/v1",
                    "decision": "blocked",
                    "reason": "selection_failed",
                    "fallback": False,
                },
                json_output=ns.json,
            )
            return 2
        safe_error = local_identity_guard.redact(redact(str(exc), repo), identities)
        print(
            json.dumps({"status": "unresolved", "error": safe_error})
            if ns.json
            else f"停止: {safe_error}"
        )
        return 2
    except subprocess.CalledProcessError:
        if ns.ci:
            _emit_ci_report(
                {
                    "schema": "test-selection-ci/v1",
                    "decision": "blocked",
                    "reason": "comparison_failed",
                    "fallback": False,
                },
                json_output=ns.json,
            )
            return 2
        message = (
            "比較元が読めないので停止します（git fetch と比較元を確認してください）"
        )
        print(
            json.dumps({"status": "unresolved", "error": message})
            if ns.json
            else message
        )
        return 2
    if ns.dry_run:
        for name, _ in steps:
            print(name)
        return 0
    log_dir = Path(git(repo, "rev-parse", "--git-dir").strip())
    if not log_dir.is_absolute():
        log_dir = repo / log_dir
    log_dir = log_dir / "local-check"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"{time.strftime('%Y%m%d-%H%M%S')}-{ns.mode}.log"
    failed: list[str] = []
    started = time.monotonic()
    with log_path.open("w", encoding="utf-8") as log:
        for name, cmd in steps:
            t0 = time.monotonic()
            # 実行したコマンドの絶対パス（Python の場所など）は書かず、出力も伏せてから残す
            done = subprocess.run(
                cmd,
                cwd=repo,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="backslashreplace",
            )
            rc = done.returncode
            log.write(f"===== {name}\n")
            log.write(
                local_identity_guard.redact(redact(done.stdout, repo), identities)
            )
            mark = "緑" if rc == 0 else "赤"
            print(f"{mark} {name} {time.monotonic() - t0:.1f}秒")
            if rc != 0:
                failed.append(name)
    total = time.monotonic() - started
    # linked worktree では git-dir が repo の外にあるので相対パスで示す（実ホームパスを出さない）
    rel = Path(os.path.relpath(log_path, repo)).as_posix()
    status = "合格" if not failed else "不合格"
    print(
        f"{status}（{ns.mode}）: {len(steps) - len(failed)}/{len(steps)} 項目 {total:.1f}秒"
    )
    if failed:
        print("失敗した項目: " + "、".join(failed))
    print("CI の結果は別に確認してください（PR 本文・全 ref の走査を含む）")
    print(f"全文のログ: {rel}")
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
