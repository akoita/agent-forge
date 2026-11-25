"""AST and multi-language symbol extraction with dependency-aware PageRank mapping."""

from __future__ import annotations

import ast
import fnmatch
import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from agent_forge.agent.instructions import _estimate_tokens

if TYPE_CHECKING:
    from collections.abc import Sequence

_IGNORED_DIRS: frozenset[str] = frozenset(
    {
        ".git",
        "node_modules",
        ".venv",
        "venv",
        "__pycache__",
        "dist",
        "build",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        ".tox",
        ".eggs",
        ".agent-forge",
        ".eval-artifacts",
    }
)

_MAX_FILE_BYTES = 256 * 1024
_SUPPORTED_EXTENSIONS: frozenset[str] = frozenset(
    {
        ".py",
        ".ts",
        ".tsx",
        ".js",
        ".jsx",
        ".mjs",
        ".cjs",
        ".go",
        ".rs",
        ".java",
        ".c",
        ".cc",
        ".cpp",
        ".h",
        ".hpp",
        ".rb",
        ".cs",
        ".php",
        ".kt",
        ".kts",
        ".swift",
        ".sh",
        ".bash",
        ".sol",
    }
)

_IDENT_RE = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]{2,})\b")
_UPPER_CONST_RE = re.compile(r"^[A-Z][A-Z0-9_]{1,}$")
_WORKSPACE_PREFIX = "/workspace"

_LANG_DEF_PATTERNS: dict[str, tuple[tuple[str, re.Pattern[str]], ...]] = {
    "js": (
        ("class", re.compile(r"^\s*(?:export\s+)?(?:abstract\s+)?class\s+(\w+)", re.M)),
        ("type", re.compile(r"^\s*(?:export\s+)?(?:interface|type|enum)\s+(\w+)", re.M)),
        (
            "function",
            re.compile(r"^\s*(?:export\s+)?(?:async\s+)?function\s+(\w+)\s*\(([^)]*)\)", re.M),
        ),
        (
            "function",
            re.compile(
                r"^\s*(?:export\s+)?(?:const|let)\s+(\w+)\s*=\s*(?:async\s*)?\(([^)]*)\)\s*=>",
                re.M,
            ),
        ),
    ),
    "go": (
        ("type", re.compile(r"^\s*type\s+(\w+)\s+(?:struct|interface)\b", re.M)),
        ("function", re.compile(r"^\s*func\s+(?:\([^)]+\)\s*)?(\w+)\s*\(([^)]*)\)", re.M)),
    ),
    "rs": (
        (
            "type",
            re.compile(r"^\s*(?:pub(?:\([^)]*\))?\s+)?(?:struct|enum|trait|mod)\s+(\w+)", re.M),
        ),
        ("type", re.compile(r"^\s*impl(?:<[^>]+>)?\s+(\w+)", re.M)),
        (
            "function",
            re.compile(r"^\s*(?:pub(?:\([^)]*\))?\s+)?(?:async\s+)?fn\s+(\w+)\s*\(([^)]*)\)", re.M),
        ),
    ),
    "java": (
        (
            "class",
            re.compile(
                r"^\s*(?:public|private|protected|\s)*(?:class|interface|enum|record)\s+(\w+)",
                re.M,
            ),
        ),
        (
            "function",
            re.compile(
                r"^\s*(?:public|protected|private|static|final|\s)+[\w<>\[\]]+\s+(\w+)\s*\(([^)]*)\)\s*\{",
                re.M,
            ),
        ),
    ),
    "rb": (
        ("class", re.compile(r"^\s*(?:class|module)\s+([\w:]+)", re.M)),
        ("function", re.compile(r"^\s*def\s+([\w.!?=]+)(?:\(([^)]*)\))?", re.M)),
    ),
    "c": (
        ("type", re.compile(r"^\s*(?:typedef\s+)?(?:class|struct|enum)\s+(\w+)\s*\{", re.M)),
        ("function", re.compile(r"^[A-Za-z_][\w\s\*&:<>]*\b(\w+)\s*\(([^;{)]*)\)\s*\{", re.M)),
    ),
    "cs": (
        (
            "class",
            re.compile(
                r"^\s*(?:public|private|protected|internal|sealed|abstract|static|\s)*"
                r"(?:class|interface|struct|record|enum)\s+(\w+)",
                re.M,
            ),
        ),
        (
            "function",
            re.compile(
                r"^\s*(?:public|private|protected|internal|static|async|virtual|override|\s)+"
                r"[\w<>\[\]?]+\s+(\w+)\s*\(([^)]*)\)",
                re.M,
            ),
        ),
    ),
    "php": (
        (
            "class",
            re.compile(r"^\s*(?:abstract\s+|final\s+)?(?:class|interface|trait)\s+(\w+)", re.M),
        ),
        (
            "function",
            re.compile(
                r"^\s*(?:public|protected|private|static|\s)*function\s+(\w+)\s*\(([^)]*)\)", re.M
            ),
        ),
    ),
    "kotlin": (
        (
            "class",
            re.compile(
                r"^\s*(?:data\s+|sealed\s+|open\s+|abstract\s+)?(?:class|interface|object|enum\s+class)\s+(\w+)",
                re.M,
            ),
        ),
        (
            "function",
            re.compile(
                r"^\s*(?:suspend\s+|override\s+|private\s+|internal\s+)?fun\s+(?:<[^>]+>\s*)?(\w+)\s*\(([^)]*)\)",
                re.M,
            ),
        ),
    ),
    "swift": (
        (
            "class",
            re.compile(
                r"^\s*(?:public|internal|private|final|\s)*(?:class|struct|protocol|enum|actor)\s+(\w+)",
                re.M,
            ),
        ),
        (
            "function",
            re.compile(
                r"^\s*(?:public|internal|private|static|\s)*func\s+(\w+)\s*\(([^)]*)\)", re.M
            ),
        ),
    ),
    "sh": (
        ("function", re.compile(r"^\s*(?:function\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*\(\s*\)", re.M)),
    ),
    "sol": (
        ("class", re.compile(r"^\s*(?:abstract\s+)?(?:contract|interface|library)\s+(\w+)", re.M)),
        ("type", re.compile(r"^\s*(?:struct|enum)\s+(\w+)", re.M)),
        ("function", re.compile(r"^\s*function\s+(\w+)\s*\(([^)]*)\)", re.M)),
    ),
}

_LANG_IMPORT_PATTERNS: dict[str, tuple[re.Pattern[str], ...]] = {
    "js": (
        re.compile(r"""import\s+[^;]*?from\s+['"]([^'"]+)['"]"""),
        re.compile(r"""require\(\s*['"]([^'"]+)['"]\s*\)"""),
    ),
    "go": (re.compile(r"""^\s*(?:[A-Za-z_]\w*\s+)?["]([^"]+)["]""", re.M),),
    "rs": (re.compile(r"^\s*(?:pub\s+)?use\s+([\w:]+)", re.M),),
    "java": (re.compile(r"^\s*import\s+(?:static\s+)?([\w.]+);", re.M),),
    "rb": (re.compile(r"""^\s*require(?:_relative)?\s+['"]([^'"]+)['"]""", re.M),),
    "c": (re.compile(r"""^\s*#include\s*[<"]([^>"]+)[>"]""", re.M),),
    "cs": (re.compile(r"^\s*using\s+(?:static\s+)?([\w.]+);", re.M),),
    "php": (re.compile(r"^\s*use\s+([\w\\]+);", re.M),),
    "kotlin": (re.compile(r"^\s*import\s+([\w.]+)", re.M),),
    "swift": (re.compile(r"^\s*import\s+(\w+)", re.M),),
    "sh": (re.compile(r"""^\s*(?:source|\.)\s+['"]?([^\s'"]+)['"]?""", re.M),),
    "sol": (re.compile(r"""^\s*import\s+(?:[^'"]*from\s+)?['"]([^'"]+)['"]""", re.M),),
}

_EXT_TO_LANG: dict[str, str] = {
    ".ts": "ts",
    ".tsx": "tsx",
    ".js": "js",
    ".jsx": "js",
    ".mjs": "js",
    ".cjs": "js",
    ".go": "go",
    ".rs": "rs",
    ".java": "java",
    ".rb": "rb",
    ".c": "c",
    ".cc": "cpp",
    ".cpp": "cpp",
    ".h": "c",
    ".hpp": "cpp",
    ".cs": "cs",
    ".php": "php",
    ".kt": "kotlin",
    ".kts": "kotlin",
    ".swift": "swift",
    ".sh": "sh",
    ".bash": "sh",
    ".sol": "sol",
}

_PATTERN_ALIAS: dict[str, str] = {
    "ts": "js",
    "tsx": "js",
    "cpp": "c",
}


@dataclass(frozen=True)
class SymbolDef:
    """Extracted source-code definition."""

    name: str
    kind: str
    signature: str
    line: int
    parent: str | None = None


@dataclass(frozen=True)
class FileSymbols:
    """Definitions, outgoing symbol references, and imports extracted from one file."""

    rel_path: str
    definitions: tuple[SymbolDef, ...]
    references: frozenset[str]
    imports: frozenset[str]
    line_count: int


@dataclass(frozen=True)
class RepoMap:
    """Dependency-ranked repository map rendered for system prompt injection."""

    rendered: str
    ranked_files: tuple[str, ...]
    total_files_scanned: int
    total_symbols: int
    estimated_tokens: int
    truncated: bool


def _format_py_arg(arg: ast.arg) -> str:
    if arg.annotation is None:
        return arg.arg
    return f"{arg.arg}: {ast.unparse(arg.annotation)}"


def _format_py_function_signature(
    node: ast.FunctionDef | ast.AsyncFunctionDef,
    *,
    is_method: bool,
) -> str:
    params = [_format_py_arg(a) for a in node.args.posonlyargs + node.args.args]
    if is_method and params and params[0].split(":", 1)[0] in ("self", "cls"):
        params = params[1:]
    if len(params) > 4:
        params = [*params[:4], "..."]
    if node.args.kwonlyargs:
        kw_params = [_format_py_arg(a) for a in node.args.kwonlyargs[:2]]
        params.append("*")
        params.extend(kw_params)
    prefix = "async def " if isinstance(node, ast.AsyncFunctionDef) else "def "
    ret = f" -> {ast.unparse(node.returns)}" if node.returns is not None else ""
    return f"{prefix}{node.name}({', '.join(params)}){ret}"


def _extract_python_class(node: ast.ClassDef) -> list[SymbolDef]:
    bases = ", ".join(ast.unparse(b) for b in node.bases[:3])
    sig = f"class {node.name}({bases})" if bases else f"class {node.name}"
    symbols = [SymbolDef(name=node.name, kind="class", signature=sig, line=node.lineno)]
    for item in node.body:
        if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
            symbols.append(
                SymbolDef(
                    name=item.name,
                    kind="method",
                    signature=_format_py_function_signature(item, is_method=True),
                    line=item.lineno,
                    parent=node.name,
                )
            )
    return symbols


def _extract_python_const_names(node: ast.AST) -> list[str]:
    names: list[str] = []
    if isinstance(node, ast.Assign):
        targets = node.targets
    elif isinstance(node, ast.AnnAssign):
        targets = [node.target]
    else:
        return names
    for target in targets:
        if isinstance(target, ast.Name) and _UPPER_CONST_RE.match(target.id):
            names.append(target.id)
    return names


def _record_import_from(
    node: ast.ImportFrom,
    refs: set[str],
    imports: set[str],
) -> None:
    mod = node.module or ""
    if mod:
        imports.add(mod)
    for alias in node.names:
        if alias.name == "*":
            continue
        refs.add(alias.name)
        if mod:
            imports.add(f"{mod}.{alias.name}")


def _collect_python_refs_and_imports(tree: ast.AST) -> tuple[frozenset[str], frozenset[str]]:
    refs: set[str] = set()
    imports: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            _record_import_from(node, refs, imports)
        elif isinstance(node, ast.Name) and len(node.id) > 1:
            refs.add(node.id)
        elif isinstance(node, ast.Attribute) and len(node.attr) > 1:
            refs.add(node.attr)
    return frozenset(refs), frozenset(imports)


def _extract_python_file_symbols(rel_path: str, source: str) -> FileSymbols | None:
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return None

    defs: list[SymbolDef] = []
    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            defs.extend(_extract_python_class(node))
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            defs.append(
                SymbolDef(
                    name=node.name,
                    kind="function",
                    signature=_format_py_function_signature(node, is_method=False),
                    line=node.lineno,
                )
            )
        else:
            for const_name in _extract_python_const_names(node):
                defs.append(
                    SymbolDef(
                        name=const_name,
                        kind="const",
                        signature=const_name,
                        line=getattr(node, "lineno", 1),
                    )
                )

    refs, imports = _collect_python_refs_and_imports(tree)
    defined_names = {sym.name for sym in defs}
    return FileSymbols(
        rel_path=rel_path,
        definitions=tuple(defs),
        references=refs - defined_names,
        imports=imports,
        line_count=source.count("\n") + 1,
    )


def _preserve_newlines(match: re.Match[str]) -> str:
    return "\n" * match.group(0).count("\n")


def _strip_comments_and_strings(source: str, lang: str) -> str:
    """Replace comments and string literals with whitespace while preserving line numbers."""
    if lang in {"sh", "rb"}:
        comment_pat = re.compile(r"#[^\n]*")
    elif lang == "php":
        comment_pat = re.compile(r"//[^\n]*|#[^\n]*|/\*[\s\S]*?\*/")
    else:
        comment_pat = re.compile(r"//[^\n]*|/\*[\s\S]*?\*/")
    without_comments = comment_pat.sub(_preserve_newlines, source)
    string_pat = re.compile(r'`[^`]*`|"(?:\\.|[^"\\\n])*"|\'(?:\\.|[^\'\\\n])*\'')
    return string_pat.sub(_preserve_newlines, without_comments)


def _advance_brace_line(
    line: str,
    depth: int,
    pending: str | None,
    stack: list[tuple[int, str]],
) -> tuple[int, str | None]:
    for ch in line:
        if ch == "{":
            depth += 1
            if pending is not None:
                stack.append((depth, pending))
                pending = None
        elif ch == "}":
            while stack and depth <= stack[-1][0]:
                stack.pop()
            depth = max(0, depth - 1)
    return depth, pending


def _enclosing_parents_by_line(
    source: str,
    containers: list[SymbolDef],
) -> dict[int, str]:
    """Compute enclosing class/struct/contract names per line via brace depth."""
    opener_by_line = {sym.line: sym.name for sym in containers if sym.kind in {"class", "type"}}
    if not opener_by_line:
        return {}

    parents: dict[int, str] = {}
    stack: list[tuple[int, str]] = []
    depth = 0
    pending: str | None = None
    for lineno, line in enumerate(source.splitlines(), start=1):
        if lineno in opener_by_line:
            pending = opener_by_line[lineno]
        parents[lineno] = stack[-1][1] if stack else ""
        depth, pending = _advance_brace_line(line, depth, pending, stack)
    return parents


def _extract_polyglot_file_symbols(rel_path: str, lang: str, source: str) -> FileSymbols:
    pat_lang = _PATTERN_ALIAS.get(lang, lang)
    imports: set[str] = set()
    for imp_pat in _LANG_IMPORT_PATTERNS.get(pat_lang, ()):
        for match in imp_pat.finditer(source):
            imports.add(match.group(1).replace("\\", "."))

    cleaned = _strip_comments_and_strings(source, pat_lang)
    containers: list[SymbolDef] = []
    raw_defs: list[tuple[str, str, str, int]] = []
    for kind, pattern in _LANG_DEF_PATTERNS.get(pat_lang, ()):
        for match in pattern.finditer(cleaned):
            name = match.group(1)
            params = match.group(2).strip() if match.lastindex and match.lastindex >= 2 else ""
            line = cleaned.count("\n", 0, match.start()) + 1
            sig = f"{kind} {name}({params})" if kind == "function" else f"{kind} {name}"
            if kind in {"class", "type"}:
                containers.append(SymbolDef(name=name, kind=kind, signature=sig, line=line))
            raw_defs.append((name, kind, sig, line))

    parent_map = _enclosing_parents_by_line(cleaned, containers)
    defs: list[SymbolDef] = []
    for name, kind, sig, line in raw_defs:
        enclosing = parent_map.get(line, "")
        parent = enclosing if (enclosing and enclosing != name and kind == "function") else None
        effective_kind = "method" if parent is not None else kind
        defs.append(
            SymbolDef(
                name=name,
                kind=effective_kind,
                signature=sig,
                line=line,
                parent=parent,
            )
        )

    defs.sort(key=lambda item: (item.line, item.name))
    defined_names = {d.name for d in defs}
    refs = {m.group(1) for m in _IDENT_RE.finditer(cleaned)} - defined_names
    return FileSymbols(
        rel_path=rel_path,
        definitions=tuple(defs),
        references=frozenset(refs),
        imports=frozenset(imports),
        line_count=source.count("\n") + 1,
    )


def _extract_file_symbols(rel_path: str, suffix: str, source: str) -> FileSymbols:
    if suffix == ".py":
        parsed = _extract_python_file_symbols(rel_path, source)
        if parsed is not None:
            return parsed
        return _extract_polyglot_file_symbols(rel_path, "py", source)
    lang = _EXT_TO_LANG.get(suffix, "")
    if lang:
        from agent_forge.agent.treesitter import extract_treesitter_file_symbols

        ts_parsed = extract_treesitter_file_symbols(rel_path, lang, source)
        if ts_parsed is not None:
            return ts_parsed
    return _extract_polyglot_file_symbols(rel_path, lang, source)


def _load_gitignore_patterns(repo_root: Path) -> tuple[str, ...]:
    gitignore = repo_root / ".gitignore"
    if not gitignore.is_file():
        return ()
    patterns: list[str] = []
    try:
        lines = gitignore.read_text(encoding="utf-8", errors="ignore").splitlines()
    except OSError:
        return ()
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith(("#", "!")):
            continue
        patterns.append(line.rstrip("/"))
    return tuple(patterns)


def _is_gitignored(rel_path: str, patterns: tuple[str, ...]) -> bool:
    if not patterns:
        return False
    parts = Path(rel_path).parts
    name = parts[-1] if parts else rel_path
    for pat in patterns:
        cleaned = pat.lstrip("/")
        if "/" in cleaned:
            if fnmatch.fnmatch(rel_path, cleaned) or rel_path.startswith(cleaned + "/"):
                return True
        elif fnmatch.fnmatch(name, cleaned) or any(fnmatch.fnmatch(p, cleaned) for p in parts):
            return True
    return False


def _collect_source_files(repo_root: Path, *, max_files: int) -> tuple[list[Path], bool]:
    patterns = _load_gitignore_patterns(repo_root)
    candidates: list[Path] = []
    for path in repo_root.rglob("*"):
        if not path.is_file() or path.suffix.lower() not in _SUPPORTED_EXTENSIONS:
            continue
        try:
            rel = path.relative_to(repo_root)
        except ValueError:
            continue
        if any(part in _IGNORED_DIRS for part in rel.parts):
            continue
        if _is_gitignored(rel.as_posix(), patterns):
            continue
        try:
            if path.stat().st_size > _MAX_FILE_BYTES:
                continue
        except OSError:
            continue
        candidates.append(path)

    candidates.sort(key=lambda p: p.relative_to(repo_root).as_posix())
    if len(candidates) > max_files:
        return candidates[:max_files], True
    return candidates, False


def _build_module_lookup(files: Sequence[FileSymbols]) -> dict[str, list[str]]:
    lookup: dict[str, list[str]] = {}
    for entry in files:
        path = Path(entry.rel_path)
        no_ext = path.with_suffix("")
        keys = {
            path.stem,
            no_ext.as_posix(),
            ".".join(no_ext.parts),
            "::".join(no_ext.parts),
        }
        if no_ext.name == "__init__" and len(no_ext.parts) > 1:
            pkg = no_ext.parent
            keys.add(pkg.as_posix())
            keys.add(".".join(pkg.parts))
        for key in keys:
            if key:
                lookup.setdefault(key, []).append(entry.rel_path)
    return lookup


def _resolve_import_targets(imp: str, lookup: dict[str, list[str]]) -> list[str]:
    normalized = imp.strip().lstrip("./")
    for ext in _SUPPORTED_EXTENSIONS:
        if normalized.endswith(ext):
            normalized = normalized[: -len(ext)]
            break
    targets: list[str] = []
    for key in (normalized, normalized.replace(".", "/"), normalized.rsplit(".", 1)[-1]):
        for candidate in lookup.get(key, ()):
            if candidate not in targets:
                targets.append(candidate)
    return targets


def _add_file_edges(
    entry: FileSymbols,
    definers: dict[str, set[str]],
    lookup: dict[str, list[str]],
) -> dict[str, float]:
    outgoing: dict[str, float] = {}
    for imp in entry.imports:
        targets = [t for t in _resolve_import_targets(imp, lookup) if t != entry.rel_path]
        if targets:
            weight = 3.0 / len(targets)
            for tgt in targets:
                outgoing[tgt] = outgoing.get(tgt, 0.0) + weight

    for ref in entry.references:
        providers = definers.get(ref)
        if not providers:
            continue
        weight = 1.0 / len(providers)
        for tgt in providers:
            if tgt != entry.rel_path:
                outgoing[tgt] = outgoing.get(tgt, 0.0) + weight

    total = sum(outgoing.values())
    if total <= 0.0:
        return {}
    return {dst: w / total for dst, w in outgoing.items()}


def _build_dependency_graph(
    files: Sequence[FileSymbols],
) -> dict[str, dict[str, float]]:
    definers: dict[str, set[str]] = {}
    for entry in files:
        for sym in entry.definitions:
            if len(sym.name) > 1:
                definers.setdefault(sym.name, set()).add(entry.rel_path)

    lookup = _build_module_lookup(files)
    return {entry.rel_path: _add_file_edges(entry, definers, lookup) for entry in files}


def _normalize_focus_path(raw: str) -> str:
    text = raw.strip().replace("\\", "/")
    if text.startswith(_WORKSPACE_PREFIX + "/"):
        return text[len(_WORKSPACE_PREFIX) + 1 :].lstrip("/")
    return text.lstrip("./")


def _is_test_path(rel_path: str) -> bool:
    parts = Path(rel_path).parts
    name = Path(rel_path).name
    return (
        "tests" in parts
        or name.startswith("test_")
        or name.endswith("_test.py")
        or ".test." in name
    )


def _compute_personalization(
    files: Sequence[FileSymbols],
    *,
    task: str,
    focus_files: Sequence[str],
) -> dict[str, float]:
    task_lower = task.lower()
    task_tokens = set(_IDENT_RE.findall(task))
    focus_set = {_normalize_focus_path(p) for p in focus_files if p.strip()}

    weights: dict[str, float] = {}
    for entry in files:
        rel = entry.rel_path
        path_obj = Path(rel)
        explicitly_referenced = rel in focus_set or rel.lower() in task_lower
        base = 0.5 if (_is_test_path(rel) and not explicitly_referenced) else 1.0

        if rel in focus_set:
            base += 8.0
        if rel.lower() in task_lower:
            base += 10.0
        elif path_obj.name.lower() in task_lower or (
            len(path_obj.stem) > 2 and re.search(rf"\b{re.escape(path_obj.stem)}\b", task)
        ):
            base += 5.0

        matched_defs = sum(1 for sym in entry.definitions if sym.name in task_tokens)
        base += min(matched_defs, 4) * 3.0
        weights[rel] = base

    total = sum(weights.values()) or 1.0
    return {k: v / total for k, v in weights.items()}


def _rank_files(
    files: Sequence[FileSymbols],
    *,
    task: str,
    focus_files: Sequence[str],
) -> tuple[str, ...]:
    if not files:
        return ()
    edges = _build_dependency_graph(files)
    personal = _compute_personalization(files, task=task, focus_files=focus_files)
    scores = dict(personal)
    damping = 0.85

    for _ in range(20):
        dangling_mass = sum(scores[node] for node, out in edges.items() if not out)
        next_scores = {
            node: (1.0 - damping) * personal[node] + damping * dangling_mass * personal[node]
            for node in scores
        }
        for src, out in edges.items():
            if not out:
                continue
            src_score = scores[src]
            for dst, weight in out.items():
                next_scores[dst] += damping * src_score * weight
        scores = next_scores

    ordered = sorted(scores, key=lambda path: (-scores[path], path))
    return tuple(ordered)


def _format_file_outline(entry: FileSymbols, *, max_symbols: int = 24) -> list[str]:
    lines = [f"{entry.rel_path}:"]
    if not entry.definitions:
        lines.append(f"  ({entry.line_count} lines)")
        return lines

    shown = entry.definitions[:max_symbols]
    for sym in shown:
        indent = "    " if sym.parent is not None else "  "
        lines.append(f"{indent}{sym.signature}")
    if len(entry.definitions) > max_symbols:
        lines.append(f"  ... (+{len(entry.definitions) - max_symbols} more symbols)")
    return lines


def _render_repo_map(
    by_path: dict[str, FileSymbols],
    ranked_files: tuple[str, ...],
    *,
    max_tokens: int,
    files_capped: bool,
) -> tuple[str, tuple[str, ...], bool]:
    if not ranked_files or max_tokens <= 0:
        return "", (), bool(ranked_files)

    header = "## Repository Map\n"
    used_tokens = _estimate_tokens(header)
    blocks: list[str] = []
    emitted: list[str] = []
    truncated = files_capped

    for idx, rel_path in enumerate(ranked_files):
        entry = by_path[rel_path]
        block = "\n".join(_format_file_outline(entry))
        block_tokens = _estimate_tokens(block + "\n")
        if used_tokens + block_tokens > max_tokens:
            if idx == 0:
                short = "\n".join(_format_file_outline(entry, max_symbols=8))
                if used_tokens + _estimate_tokens(short) <= max_tokens:
                    blocks.append(short)
                    emitted.append(rel_path)
            truncated = True
            break
        blocks.append(block)
        emitted.append(rel_path)
        used_tokens += block_tokens

    if not blocks:
        return "", (), True
    rendered = header + "\n".join(blocks)
    return rendered, tuple(emitted), truncated


def build_repo_map(
    repo_path: Path | str,
    *,
    task: str = "",
    focus_files: Sequence[str] = (),
    max_tokens: int = 1_500,
    max_files: int = 500,
) -> RepoMap:
    """Build a dependency-ranked symbol outline for source files inside *repo_path*."""
    base = Path(repo_path)
    if not base.is_dir() or max_files <= 0:
        return RepoMap(
            rendered="",
            ranked_files=(),
            total_files_scanned=0,
            total_symbols=0,
            estimated_tokens=0,
            truncated=False,
        )

    repo_root = base.resolve()
    paths, files_capped = _collect_source_files(repo_root, max_files=max_files)
    extracted: list[FileSymbols] = []
    for path in paths:
        try:
            source = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        rel = path.relative_to(repo_root).as_posix()
        extracted.append(_extract_file_symbols(rel, path.suffix.lower(), source))

    if not extracted:
        return RepoMap(
            rendered="",
            ranked_files=(),
            total_files_scanned=0,
            total_symbols=0,
            estimated_tokens=0,
            truncated=False,
        )

    by_path = {item.rel_path: item for item in extracted}
    ranked = _rank_files(extracted, task=task, focus_files=focus_files)
    rendered, emitted, truncated = _render_repo_map(
        by_path,
        ranked,
        max_tokens=max_tokens,
        files_capped=files_capped,
    )
    total_symbols = sum(len(item.definitions) for item in extracted)
    return RepoMap(
        rendered=rendered,
        ranked_files=emitted,
        total_files_scanned=len(extracted),
        total_symbols=total_symbols,
        estimated_tokens=_estimate_tokens(rendered),
        truncated=truncated,
    )
