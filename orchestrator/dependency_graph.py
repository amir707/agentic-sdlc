"""Import-graph extraction and blast radius (deterministic tool, NOT an
agent).

Static analysis of the governed repo: parse imports, build the module
graph, compute the transitive closure of any set of changed files — the
honest version of "what does this change touch". Consumers: the risk
assessor (risk input), verify+label (actual impact), the release
manager (PR conflict = overlapping closures, replacing a crude
same-area check). Trivial at toy scale; the seam is the point.
"""

import ast
from pathlib import Path


class UnparseableSource(Exception):
    """A repo file does not parse, so the graph — and any risk math on
    top of it — cannot be trusted. Agent-written code makes this a
    NORMAL input, not an engine fault: callers turn it into a rejection
    (reason code `code_unparseable`); this module only refuses to
    guess."""

    def __init__(self, path: str, detail: str):
        super().__init__(f"{path}: {detail}")
        self.path = path
        self.detail = detail


def _module_name(py_file: Path, repo_dir: Path) -> str:
    rel = py_file.relative_to(repo_dir).with_suffix("")
    return ".".join(rel.parts)


def build_import_graph(repo_dir: str | Path) -> dict[str, set[str]]:
    """module -> set of project-internal modules it imports."""
    repo_dir = Path(repo_dir)
    modules: dict[str, Path] = {
        _module_name(f, repo_dir): f
        for f in repo_dir.rglob("*.py")
        if ".venv" not in f.parts and not f.name.startswith(".")
    }
    graph: dict[str, set[str]] = {name: set() for name in modules}

    for name, py_file in modules.items():
        try:
            tree = ast.parse(py_file.read_text(), filename=str(py_file))
        except SyntaxError as exc:
            raise UnparseableSource(
                str(py_file.relative_to(repo_dir)),
                f"{exc.msg} (line {exc.lineno})") from exc
        for node in ast.walk(tree):
            imported: list[str] = []
            if isinstance(node, ast.Import):
                imported = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                # `from app import payments` names modules in aliases too.
                imported = [node.module] + [
                    f"{node.module}.{alias.name}" for alias in node.names]
            for target in imported:
                # Keep only project-internal edges.
                if target in graph:
                    graph[name].add(target)
    return graph


def _reverse(graph: dict[str, set[str]]) -> dict[str, set[str]]:
    reverse: dict[str, set[str]] = {name: set() for name in graph}
    for src, targets in graph.items():
        for target in targets:
            reverse[target].add(src)
    return reverse


def dependents_closure(graph: dict[str, set[str]],
                       changed: set[str]) -> set[str]:
    """Changed modules plus everything that (transitively) imports them."""
    reverse = _reverse(graph)
    closure = set(changed) & set(graph)
    frontier = list(closure)
    while frontier:
        module = frontier.pop()
        for dependent in reverse.get(module, ()):
            if dependent not in closure:
                closure.add(dependent)
                frontier.append(dependent)
    return closure


def blast_radius(repo_dir: str | Path, changed_files: list[str]) -> set[str]:
    """Transitive impact of a change, as module names.

    Non-Python files (flags.json, configs) map to no module and carry no
    graph edges; they contribute themselves as-is so callers still see
    them in the touched set.
    """
    repo_dir = Path(repo_dir)
    graph = build_import_graph(repo_dir)
    changed_modules = {
        _module_name(repo_dir / f, repo_dir)
        for f in changed_files if f.endswith(".py")
    }
    radius = dependents_closure(graph, changed_modules)
    radius |= {f for f in changed_files if not f.endswith(".py")}
    return radius
