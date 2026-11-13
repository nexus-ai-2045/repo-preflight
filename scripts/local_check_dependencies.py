"""Repository file inventory and conservative static Python dependency analysis."""

from __future__ import annotations

import ast
import importlib.util
import os
from pathlib import Path, PurePosixPath, PureWindowsPath

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


def safe_relative(repo: Path, name: str) -> str:
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


def root_files(repo: Path, root: str):
    path = repo / safe_relative(repo, root)
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


def within_root(name: str, root: str) -> bool:
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


def dependency_graph(repo: Path, roots: list[str], changed: list[str], suffixes=None):
    """Resolve Python syntax to file identities; opaque loaders remain explicit issues."""
    paths = {
        p.relative_to(repo).as_posix()
        for root in roots
        for p in root_files(repo, root)
        if p.is_file() and safe_relative(repo, p.relative_to(repo).as_posix())
    }
    paths.update(safe_relative(repo, p) for p in changed)
    modules: dict[str, set[str]] = {}
    for path in paths:
        if not path.endswith(".py"):
            continue
        for root in ["."] + roots:
            if root != "." and not within_root(path, root):
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
