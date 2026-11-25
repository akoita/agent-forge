"""Optional tree-sitter polyglot AST symbol & reference extraction."""

from __future__ import annotations

import importlib
import importlib.util
import re
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable

    from agent_forge.agent.repo_map import FileSymbols, SymbolDef

_LANGUAGE_MAP: dict[str, str] = {
    "ts": "typescript",
    "tsx": "tsx",
    "js": "javascript",
    "go": "go",
    "rs": "rust",
    "java": "java",
    "c": "c",
    "cpp": "cpp",
    "rb": "ruby",
    "cs": "c_sharp",
    "php": "php",
    "kotlin": "kotlin",
    "swift": "swift",
    "sh": "bash",
    "sol": "sol",
}

_CONTAINER_KINDS: dict[str, str] = {
    "class_declaration": "class",
    "class_definition": "class",
    "class_specifier": "class",
    "struct_declaration": "type",
    "struct_item": "type",
    "struct_specifier": "type",
    "type_declaration": "type",
    "interface_declaration": "type",
    "trait_item": "type",
    "enum_declaration": "type",
    "enum_item": "type",
    "record_declaration": "type",
    "contract_declaration": "type",
    "library_declaration": "type",
    "impl_item": "type",
    "module": "class",
    "object_declaration": "class",
    "protocol_declaration": "type",
}

_FUNCTION_KINDS: frozenset[str] = frozenset(
    {
        "function_declaration",
        "function_definition",
        "function_item",
        "method_declaration",
        "method_definition",
        "constructor_declaration",
        "method",
        "singleton_method",
    }
)

_IMPORT_KINDS: frozenset[str] = frozenset(
    {
        "import_statement",
        "import_declaration",
        "import_spec",
        "use_declaration",
        "preproc_include",
        "namespace_use_declaration",
        "using_directive",
    }
)

_SKIP_TEXT_KINDS: frozenset[str] = frozenset(
    {
        "comment",
        "line_comment",
        "block_comment",
        "string",
        "string_literal",
        "interpreted_string_literal",
        "raw_string_literal",
        "template_string",
        "char_literal",
    }
)

_IDENT_KINDS: frozenset[str] = frozenset(
    {
        "identifier",
        "type_identifier",
        "property_identifier",
        "field_identifier",
        "constant",
    }
)

_IMPORT_STRING_RE = re.compile(r"""['"<]([^'">\s]+)['">]""")
_IMPORT_PATH_RE = re.compile(r"(?:import|use|using|require(?:_relative)?)\s+([A-Za-z0-9_.:/]+)")


def is_treesitter_available() -> bool:
    """Return ``True`` when optional tree-sitter packages are importable."""
    return (
        importlib.util.find_spec("tree_sitter") is not None
        and importlib.util.find_spec("tree_sitter_language_pack") is not None
    )


def supported_treesitter_languages() -> frozenset[str]:
    """Return canonical language keys handled by tree-sitter symbol extraction."""
    return frozenset(_LANGUAGE_MAP)


def _node_text(node: Any, source_bytes: bytes) -> str:
    start = int(getattr(node, "start_byte", 0))
    end = int(getattr(node, "end_byte", 0))
    if end <= start:
        return ""
    return source_bytes[start:end].decode("utf-8", errors="ignore")


def _extract_name(node: Any, source_bytes: bytes) -> str:
    name_fn = getattr(node, "child_by_field_name", None)
    if callable(name_fn):
        for field_name in ("name", "type", "declarator"):
            child = name_fn(field_name)
            if child is not None:
                text = _node_text(child, source_bytes).strip()
                ident = re.match(r"([A-Za-z_][A-Za-z0-9_]*)", text)
                if ident:
                    return ident.group(1)
    for child in getattr(node, "children", ()) or ():
        if getattr(child, "type", "") in _IDENT_KINDS:
            text = _node_text(child, source_bytes).strip()
            if text:
                return text
    return ""


def _extract_signature_line(node: Any, source_bytes: bytes, fallback: str) -> str:
    raw = _node_text(node, source_bytes)
    first_line = raw.splitlines()[0].strip() if raw else ""
    cleaned = first_line.split("{", 1)[0].strip()
    if not cleaned:
        return fallback
    return cleaned[:96] + "..." if len(cleaned) > 96 else cleaned


def _extract_import_targets(text: str) -> list[str]:
    targets = _IMPORT_STRING_RE.findall(text)
    if targets:
        return targets
    return _IMPORT_PATH_RE.findall(text)


def _walk_treesitter(
    node: Any,
    source_bytes: bytes,
    *,
    parent_type: str | None,
    defs: list[SymbolDef],
    refs: set[str],
    imports: set[str],
) -> None:
    from agent_forge.agent.repo_map import SymbolDef

    node_type = str(getattr(node, "type", ""))
    if node_type in _SKIP_TEXT_KINDS:
        return

    line = int(getattr(node, "start_point", (0, 0))[0]) + 1
    next_parent = parent_type

    if node_type in _IMPORT_KINDS:
        imports.update(_extract_import_targets(_node_text(node, source_bytes)))
        return

    if node_type in _CONTAINER_KINDS:
        sym_name = _extract_name(node, source_bytes)
        if sym_name:
            kind = _CONTAINER_KINDS[node_type]
            defs.append(
                SymbolDef(
                    name=sym_name,
                    kind=kind,
                    signature=f"{kind} {sym_name}",
                    line=line,
                    parent=parent_type,
                )
            )
            next_parent = sym_name
    elif node_type in _FUNCTION_KINDS:
        sym_name = _extract_name(node, source_bytes)
        if sym_name:
            kind = "method" if parent_type is not None else "function"
            sig = _extract_signature_line(node, source_bytes, f"{kind} {sym_name}()")
            defs.append(
                SymbolDef(
                    name=sym_name,
                    kind=kind,
                    signature=sig,
                    line=line,
                    parent=parent_type,
                )
            )
    elif node_type in _IDENT_KINDS:
        ident = _node_text(node, source_bytes).strip()
        if len(ident) > 2:
            refs.add(ident)

    for child in getattr(node, "children", ()) or ():
        _walk_treesitter(
            child,
            source_bytes,
            parent_type=next_parent,
            defs=defs,
            refs=refs,
            imports=imports,
        )


def _load_parser(
    grammar_name: str,
    parser_factory: Callable[[str], Any] | None,
) -> Any | None:
    if parser_factory is not None:
        return parser_factory(grammar_name)
    if not is_treesitter_available():
        return None
    try:
        pack = importlib.import_module("tree_sitter_language_pack")
        return pack.get_parser(grammar_name)
    except (ImportError, LookupError, ValueError, RuntimeError, OSError):
        return None


def extract_treesitter_file_symbols(
    rel_path: str,
    lang: str,
    source: str,
    *,
    parser_factory: Callable[[str], Any] | None = None,
) -> FileSymbols | None:
    """Parse *source* via tree-sitter when available; return ``None`` on fallback."""
    from agent_forge.agent.repo_map import FileSymbols

    grammar_name = _LANGUAGE_MAP.get(lang)
    if grammar_name is None:
        return None

    parser = _load_parser(grammar_name, parser_factory)
    if parser is None:
        return None

    source_bytes = source.encode("utf-8", errors="ignore")
    try:
        tree = parser.parse(source_bytes)
    except (RuntimeError, ValueError, TypeError, OSError):
        return None

    root = getattr(tree, "root_node", None)
    if root is None:
        return None

    defs: list[SymbolDef] = []
    refs: set[str] = set()
    imports: set[str] = set()
    _walk_treesitter(
        root,
        source_bytes,
        parent_type=None,
        defs=defs,
        refs=refs,
        imports=imports,
    )
    defs.sort(key=lambda sym: (sym.line, sym.name))
    defined = {sym.name for sym in defs}
    return FileSymbols(
        rel_path=rel_path,
        definitions=tuple(defs),
        references=frozenset(refs - defined),
        imports=frozenset(imports),
        line_count=source.count("\n") + 1,
    )
