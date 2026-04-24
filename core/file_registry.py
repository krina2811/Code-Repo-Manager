"""
FileRegistry — Live In-Memory AST Index

The single source of truth for all file state during an analysis session.
Every action reads from and writes back through this registry — never
touches disk directly.

Architecture:
  FileRegistry (singleton)
    └── FileSnapshot  per file
          ├── source: str           raw text, always current
          ├── lines: List[str]      split lines (1-indexed via helpers)
          ├── tree: ast.AST         re-parsed after every edit
          ├── symbols: SymbolTable  rebuilt after every edit
          ├── dirty: bool           written to disk on flush()
          └── checksum: str         sha256, used to detect external changes

SymbolTable tracks FIVE kinds of symbols:
  • function    — def / async def
  • class       — class Foo:
  • import      — import os, import sys
  • import_from — from pathlib import Path
  • assignment   — MODULE-SCOPE variable assignments
                   e.g.  BASE_URL = "…"   logger = …   app = Flask(…)
  • statement    — MODULE-SCOPE structural blocks that don't define a symbol
                   e.g.  if __name__=="__main__":  try/except  with  for

  FileEditor      — all mutations go here, triggers re-index after every op
  SymbolInfo      — start_line / end_line always reflect CURRENT in-memory state
"""

import ast
import hashlib
import threading
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from core.logger import get_logger

logger = get_logger(__name__)

try:
    import radon.complexity as radon_cc
    _RADON_AVAILABLE = True
except ImportError:
    _RADON_AVAILABLE = False


# ─────────────────────────────────────────────────────────────────────────────
# SymbolInfo
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class SymbolInfo:
    """
    Metadata for one code entity.  Lines are always 1-indexed.

    kind values
    ───────────
    function    — def / async def
    class       — class Foo:
    import      — import os
    import_from — from pathlib import Path
    assignment  — module-scope variable assignment (BASE_URL = "…")
    statement   — module-scope structural block (if / try / with / for / expr)
    """

    name: str
    kind: str
    start_line: int
    end_line: int
    col_offset: int = 0

    # function / class extras
    has_docstring: bool = False
    complexity: int = 0

    # import extras
    module: Optional[str] = None
    alias: Optional[str] = None

    # assignment extras
    annotation: Optional[str] = None
    value_repr: Optional[str] = None

    # statement extras
    stmt_kind: Optional[str] = None    # 'if' | 'try' | 'with' | 'for' | 'expr'

    scope: str = "module"

    def line_range(self) -> Tuple[int, int]:
        return (self.start_line, self.end_line)

    def line_count(self) -> int:
        return self.end_line - self.start_line + 1


# ─────────────────────────────────────────────────────────────────────────────
# SymbolTable
# ─────────────────────────────────────────────────────────────────────────────

class SymbolTable:
    """
    Keyed lookup for every symbol in a file.
    Completely rebuilt after every FileEditor mutation.

    Containers
    ──────────
    functions   — Dict[name, SymbolInfo]
    classes     — Dict[name, SymbolInfo]
    imports     — Dict[name_or_alias, SymbolInfo]
    assignments — Dict[name, SymbolInfo]   module-level variables / constants
    statements  — List[SymbolInfo]         unnamed module-level blocks
    """

    def __init__(self):
        self.functions:   Dict[str, SymbolInfo] = {}
        self.classes:     Dict[str, SymbolInfo] = {}
        self.imports:     Dict[str, SymbolInfo] = {}
        self.assignments: Dict[str, SymbolInfo] = {}
        self.statements:  List[SymbolInfo] = []

    # ── rebuild ───────────────────────────────────────────────────────

    def rebuild(self, tree: ast.AST, source: str):
        """Re-populate every container from a freshly-parsed AST."""
        self.functions.clear()
        self.classes.clear()
        self.imports.clear()
        self.assignments.clear()
        self.statements.clear()

        # Complexity map (radon, optional)
        complexity_map: Dict[str, int] = {}
        if _RADON_AVAILABLE:
            try:
                for item in radon_cc.cc_visit(source):
                    complexity_map[item.name] = item.complexity
            except Exception:
                pass

        # Walk full AST for functions / classes / imports
        for node in ast.walk(tree):

            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                name = node.name
                has_doc = (
                    node.body
                    and isinstance(node.body[0], ast.Expr)
                    and isinstance(node.body[0].value, ast.Constant)
                    and isinstance(node.body[0].value.value, str)
                )
                self.functions[name] = SymbolInfo(
                    name=name,
                    kind="function",
                    start_line=node.lineno,
                    end_line=node.end_lineno,
                    col_offset=node.col_offset,
                    has_docstring=has_doc,
                    complexity=complexity_map.get(name, 0),
                )

            elif isinstance(node, ast.ClassDef):
                name = node.name
                has_doc = (
                    node.body
                    and isinstance(node.body[0], ast.Expr)
                    and isinstance(node.body[0].value, ast.Constant)
                    and isinstance(node.body[0].value.value, str)
                )
                self.classes[name] = SymbolInfo(
                    name=name,
                    kind="class",
                    start_line=node.lineno,
                    end_line=node.end_lineno,
                    col_offset=node.col_offset,
                    has_docstring=has_doc,
                )

            elif isinstance(node, ast.Import):
                for alias in node.names:
                    key = alias.asname or alias.name
                    self.imports[key] = SymbolInfo(
                        name=key,
                        kind="import",
                        start_line=node.lineno,
                        end_line=node.lineno,
                        module=alias.name,
                        alias=alias.asname,
                    )

            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                for alias in node.names:
                    key = alias.asname or alias.name
                    self.imports[key] = SymbolInfo(
                        name=key,
                        kind="import_from",
                        start_line=node.lineno,
                        end_line=node.lineno,
                        module=module,
                        alias=alias.asname,
                    )

        # Walk only MODULE-SCOPE statements for assignments + blocks
        for node in tree.body:
            self._index_module_statement(node)

    def _index_module_statement(self, node: ast.stmt):
        """Index a single module-scope statement into assignments or statements."""

        # x = expr
        if isinstance(node, ast.Assign):
            for target in node.targets:
                for name in self._extract_target_names(target):
                    self.assignments[name] = SymbolInfo(
                        name=name,
                        kind="assignment",
                        start_line=node.lineno,
                        end_line=node.end_lineno,
                        value_repr=self._repr_value(node.value),
                    )

        # x: int = expr
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            name = node.target.id
            ann = ast.unparse(node.annotation) if hasattr(ast, "unparse") else ""
            self.assignments[name] = SymbolInfo(
                name=name,
                kind="assignment",
                start_line=node.lineno,
                end_line=node.end_lineno,
                annotation=ann,
                value_repr=self._repr_value(node.value) if node.value else None,
            )

        # x += 1
        elif isinstance(node, ast.AugAssign) and isinstance(node.target, ast.Name):
            name = node.target.id
            self.assignments[name] = SymbolInfo(
                name=name,
                kind="assignment",
                start_line=node.lineno,
                end_line=node.end_lineno,
                value_repr="augmented",
            )

        # Already indexed above — skip
        elif isinstance(node, (
            ast.FunctionDef, ast.AsyncFunctionDef,
            ast.ClassDef, ast.Import, ast.ImportFrom,
        )):
            return

        # Structural blocks — if / try / with / for / while / expr
        else:
            stmt_map = {
                ast.If:       "if",
                ast.Try:      "try",
                ast.With:     "with",
                ast.AsyncWith:"with",
                ast.For:      "for",
                ast.AsyncFor: "for",
                ast.While:    "while",
                ast.Expr:     "expr",
            }
            # Python 3.11+ TryStar
            if hasattr(ast, "TryStar") and isinstance(node, ast.TryStar):
                stmt_map[type(node)] = "try"

            stmt_kind = stmt_map.get(type(node))
            if stmt_kind:
                auto_name = f"{stmt_kind}_block_L{node.lineno}"
                self.statements.append(SymbolInfo(
                    name=auto_name,
                    kind="statement",
                    start_line=node.lineno,
                    end_line=node.end_lineno,
                    stmt_kind=stmt_kind,
                ))

    # ── helpers ───────────────────────────────────────────────────────

    @staticmethod
    def _extract_target_names(target: ast.expr) -> List[str]:
        if isinstance(target, ast.Name):
            return [target.id]
        if isinstance(target, (ast.Tuple, ast.List)):
            names = []
            for elt in target.elts:
                names.extend(SymbolTable._extract_target_names(elt))
            return names
        if isinstance(target, ast.Starred):
            return SymbolTable._extract_target_names(target.value)
        return []

    @staticmethod
    def _repr_value(node) -> str:
        if node is None:
            return ""
        try:
            if hasattr(ast, "unparse"):
                raw = ast.unparse(node)
                return raw[:60] + ("…" if len(raw) > 60 else "")
        except Exception:
            pass
        return type(node).__name__

    # ── lookup ────────────────────────────────────────────────────────

    def find(self, name: str, kind: Optional[str] = None) -> Optional[SymbolInfo]:
        buckets = {
            "function":    self.functions,
            "class":       self.classes,
            "import":      self.imports,
            "import_from": self.imports,
            "assignment":  self.assignments,
        }
        if kind and kind in buckets:
            return buckets[kind].get(name)

        for bucket in (self.functions, self.classes, self.imports, self.assignments):
            if name in bucket:
                return bucket[name]

        for stmt in self.statements:
            if stmt.name == name:
                return stmt

        return None

    def find_statement_at(self, line: int) -> Optional[SymbolInfo]:
        """Return the module-scope statement block containing line."""
        for stmt in self.statements:
            if stmt.start_line <= line <= stmt.end_line:
                return stmt
        return None

    def all_symbols(self) -> List[SymbolInfo]:
        return (
            list(self.functions.values())
            + list(self.classes.values())
            + list(self.imports.values())
            + list(self.assignments.values())
            + self.statements
        )

    def summary(self) -> dict:
        return {
            "functions":   len(self.functions),
            "classes":     len(self.classes),
            "imports":     len(self.imports),
            "assignments": len(self.assignments),
            "statements":  len(self.statements),
        }


# ─────────────────────────────────────────────────────────────────────────────
# FileSnapshot
# ─────────────────────────────────────────────────────────────────────────────

class FileSnapshot:
    """
    Complete in-memory representation of one Python file.
    After every edit the AST and SymbolTable are immediately rebuilt.
    """

    def __init__(self, path: str):
        self.path: str = path
        self.source: str = ""
        self.lines: List[str] = []
        self.tree: Optional[ast.AST] = None
        self.symbols: SymbolTable = SymbolTable()
        self.dirty: bool = False
        self.checksum: str = ""
        self.last_synced: Optional[datetime] = None
        self.parse_error: Optional[str] = None
        self._lock = threading.Lock()

    def load_from_disk(self) -> bool:
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                self.source = f.read()
            self._reindex()
            self.dirty = False
            self.last_synced = datetime.now()
            return True
        except Exception as e:
            self.parse_error = str(e)
            return False

    def _reindex(self):
        self.lines = self.source.splitlines(keepends=True)
        self.checksum = hashlib.sha256(self.source.encode()).hexdigest()
        try:
            self.tree = ast.parse(self.source)
            self.symbols.rebuild(self.tree, self.source)
            self.parse_error = None
        except SyntaxError as e:
            self.parse_error = str(e)
            self.tree = None

    def flush_to_disk(self) -> bool:
        try:
            with open(self.path, "w", encoding="utf-8") as f:
                f.write(self.source)
            self.dirty = False
            self.last_synced = datetime.now()
            return True
        except Exception:
            return False

    def get_line(self, line_number: int) -> Optional[str]:
        idx = line_number - 1
        return self.lines[idx] if 0 <= idx < len(self.lines) else None

    def find_line_by_content(self, hint: str, search_from: int = 1) -> Optional[int]:
        hint = hint.strip()
        if not hint:
            return None
        for i, line in enumerate(self.lines[search_from - 1:], start=search_from):
            if hint in line.strip() or line.strip() in hint:
                return i
        return None

    def get_lines_range(self, start: int, end: int) -> List[str]:
        return self.lines[start - 1 : end]

    def total_lines(self) -> int:
        return len(self.lines)

    def has_import(self, name: str) -> bool:
        return name in self.symbols.imports

    def get_module_exports(self) -> List[str]:
        """
        Return names in __all__ if defined, else all public-facing names.
        Used by DeadCodeAgent to protect public API symbols.
        """
        if "__all__" in self.symbols.assignments:
            sym = self.symbols.assignments["__all__"]
            try:
                import re
                all_line = self.get_line(sym.start_line) or ""
                m = re.search(r"\[([^\]]+)\]", all_line)
                if m:
                    return [
                        s.strip().strip("'\"")
                        for s in m.group(1).split(",")
                        if s.strip().strip("'\"")
                    ]
            except Exception:
                pass
        return (
            [n for n in self.symbols.functions  if not n.startswith("_")]
            + [n for n in self.symbols.classes  if not n.startswith("_")]
            + [n for n in self.symbols.assignments if not n.startswith("_")]
        )

    def state_dict(self) -> dict:
        return {
            "path":        self.path,
            "lines":       self.total_lines(),
            "dirty":       self.dirty,
            "checksum":    self.checksum[:10],
            "parse_error": self.parse_error,
            "symbols":     self.symbols.summary(),
        }


# ─────────────────────────────────────────────────────────────────────────────
# FileEditor
# ─────────────────────────────────────────────────────────────────────────────

class FileEditor:
    """
    All mutations go through here.
    Every method rebuilds the AST + full SymbolTable (incl. assignments
    and statements) after the edit so the registry stays consistent.
    """

    @staticmethod
    def _normalize(lines: List[str]) -> List[str]:
        return [ln if ln.endswith("\n") else ln + "\n" for ln in lines]

    @staticmethod
    def _commit(snapshot: FileSnapshot, new_lines: List[str]):
        snapshot.lines = new_lines
        snapshot.source = "".join(new_lines)
        snapshot.dirty = True
        snapshot._reindex()

    @staticmethod
    def delete_lines(snapshot: FileSnapshot, start_line: int, end_line: int) -> bool:
        with snapshot._lock:
            s = start_line - 1
            e = end_line
            if s < 0 or e > len(snapshot.lines) or s >= e:
                return False
            new_lines = snapshot.lines[:s] + snapshot.lines[e:]
            clean: List[str] = []
            prev_blank = False
            for ln in new_lines:
                is_blank = ln.strip() == ""
                if is_blank and prev_blank:
                    continue
                clean.append(ln)
                prev_blank = is_blank
            FileEditor._commit(snapshot, clean)
            return True

    @staticmethod
    def insert_lines(
        snapshot: FileSnapshot,
        after_line: int,
        new_lines: List[str],
    ) -> bool:
        with snapshot._lock:
            idx = after_line
            if idx < 0 or idx > len(snapshot.lines):
                return False
            normalized = FileEditor._normalize(new_lines)
            result = snapshot.lines[:idx] + normalized + snapshot.lines[idx:]
            FileEditor._commit(snapshot, result)
            return True

    @staticmethod
    def replace_lines(
        snapshot: FileSnapshot,
        start_line: int,
        end_line: int,
        new_lines: List[str],
    ) -> bool:
        with snapshot._lock:
            s = start_line - 1
            e = end_line
            if s < 0 or e > len(snapshot.lines) or s > e:
                return False
            normalized = FileEditor._normalize(new_lines)
            result = snapshot.lines[:s] + normalized + snapshot.lines[e:]
            FileEditor._commit(snapshot, result)
            return True

    @staticmethod
    def replace_source(snapshot: FileSnapshot, new_source: str) -> bool:
        with snapshot._lock:
            snapshot.source = new_source
            snapshot.dirty = True
            snapshot._reindex()
            return True


# ─────────────────────────────────────────────────────────────────────────────
# FileRegistry  (Singleton)
# ─────────────────────────────────────────────────────────────────────────────

class FileRegistry:
    """
    Singleton holding all FileSnapshots for the current session.
    Now surfaces assignments and statements alongside functions/classes/imports.
    """

    _instance: Optional["FileRegistry"] = None
    _creation_lock = threading.Lock()

    def __new__(cls):
        with cls._creation_lock:
            if cls._instance is None:
                inst = super().__new__(cls)
                inst._snapshots: Dict[str, FileSnapshot] = {}
                inst._rlock = threading.RLock()
                cls._instance = inst
        return cls._instance

    @classmethod
    def get_instance(cls) -> "FileRegistry":
        return cls()

    def _resolve(self, path: str) -> str:
        return str(Path(path).resolve())

    def load(self, path: str) -> Optional[FileSnapshot]:
        abs_path = self._resolve(path)
        with self._rlock:
            if abs_path in self._snapshots:
                snap = self._snapshots[abs_path]
                try:
                    disk_hash = hashlib.sha256(Path(abs_path).read_bytes()).hexdigest()
                    if disk_hash != snap.checksum and not snap.dirty:
                        snap.load_from_disk()
                except Exception:
                    pass
                return snap
            snap = FileSnapshot(abs_path)
            if snap.load_from_disk():
                self._snapshots[abs_path] = snap
                return snap
            return None

    def get(self, path: str) -> Optional[FileSnapshot]:
        return self.load(path)

    def get_symbol(
        self,
        path: str,
        name: str,
        kind: Optional[str] = None,
    ) -> Optional[SymbolInfo]:
        snap = self.get(path)
        return snap.symbols.find(name, kind) if snap else None

    def get_assignment(self, path: str, name: str) -> Optional[SymbolInfo]:
        snap = self.get(path)
        return snap.symbols.assignments.get(name) if snap else None

    def get_statement_at_line(self, path: str, line: int) -> Optional[SymbolInfo]:
        snap = self.get(path)
        return snap.symbols.find_statement_at(line) if snap else None

    def flush(self, path: str) -> bool:
        abs_path = self._resolve(path)
        with self._rlock:
            snap = self._snapshots.get(abs_path)
            if snap and snap.dirty:
                ok = snap.flush_to_disk()
                logger.debug("Flushed: %s (%d lines)", Path(abs_path).name, snap.total_lines())
                return ok
        return True

    def flush_all(self):
        with self._rlock:
            for abs_path, snap in self._snapshots.items():
                if snap.dirty:
                    snap.flush_to_disk()
                    logger.debug("Flushed: %s", Path(abs_path).name)

    def invalidate(self, path: str):
        abs_path = self._resolve(path)
        with self._rlock:
            self._snapshots.pop(abs_path, None)

    def clear(self):
        with self._rlock:
            self._snapshots.clear()

    def summary(self) -> dict:
        with self._rlock:
            return {p: s.state_dict() for p, s in self._snapshots.items()}

    def dirty_files(self) -> List[str]:
        with self._rlock:
            return [p for p, s in self._snapshots.items() if s.dirty]

    def loaded_files(self) -> List[str]:
        with self._rlock:
            return list(self._snapshots.keys())


# ── Module-level singleton ────────────────────────────────────────────────────

registry = FileRegistry.get_instance()