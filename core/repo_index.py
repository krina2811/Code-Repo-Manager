"""
RepoIndex — Cross-File Symbol Analysis

Builds three maps over the entire repository in a single scan pass:

  definitions   Dict[symbol_name → List[DefinitionSite]]
                  Where every function/class/assignment is defined

  usages        Dict[symbol_name → Set[file_path]]
                  Which files reference each symbol by name

  import_graph  Dict[file_path → List[ImportEdge]]
                  Resolved import chains — what each file imports and from where

These maps let DeadCodeAgent answer the question:
  "Is this function TRULY unused across the whole repo,
   or does another file import and call it?"

A symbol is considered TRULY DEAD only when:
  1. Not called anywhere in its own file
  2. Not imported by any other file
  3. Not referenced by name in any file that has imported it
  4. Not in the file's public API (__all__ or __init__.py exports)

Import resolution
─────────────────
  "from utils import parse_config"
    → look for utils.py relative to importing file
    → look for utils/__init__.py
    → walk up to repo root and try again
    → if still unresolved → mark as EXTERNAL (stdlib / third-party → never dead)

Usage detection
───────────────
  We use a fast token scan (re.findall) rather than full AST walking so
  we catch dynamic usages like  getattr(obj, "func_name")  and string refs.
  This produces false-negatives (symbols kept alive by string references)
  which is the SAFE direction — we'd rather not delete something than delete
  something used dynamically.
"""

import ast
import re
import sys
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

from core.file_registry import FileRegistry
from core.logger import get_logger

logger = get_logger(__name__)

registry = FileRegistry.get_instance()

# Names that are always considered "alive" even with zero explicit usages
ALWAYS_ALIVE_PATTERNS = frozenset([
    "__init__", "__main__", "__all__", "__version__", "__author__",
    "__repr__", "__str__", "__len__", "__iter__", "__next__",
    "__enter__", "__exit__", "__get__", "__set__", "__delete__",
    "__call__", "__class__", "__new__", "__del__",
    "setup", "teardown", "main",          # common entry points
    "create_app", "make_app",             # Flask / Django factory patterns
    "celery", "app",                      # common module-level singletons
])

STDLIB_MODULES = frozenset(sys.stdlib_module_names) if hasattr(sys, "stdlib_module_names") else frozenset()


# ─────────────────────────────────────────────────────────────────────────────
# Data classes
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class DefinitionSite:
    """Where a symbol is defined."""
    file_path: str
    start_line: int
    end_line: int
    kind: str           # 'function' | 'class' | 'assignment'
    is_public: bool     # not starting with underscore
    in_all: bool        # listed in __all__


@dataclass
class ImportEdge:
    """One import statement in a file, fully resolved."""
    importing_file: str         # the file that contains the import statement
    source_module: str          # raw module string, e.g. "utils.helpers"
    symbol_name: str            # imported name, e.g. "parse_config"  ("*" for star)
    resolved_file: Optional[str]  # absolute path of the source file, or None if external
    is_external: bool           # True = stdlib or third-party, skip for dead-code


@dataclass
class UsageContext:
    """
    Where a symbol is referenced (not defined).
    Includes both call sites and plain name usages.
    """
    file_path: str
    line_numbers: List[int] = field(default_factory=list)


# ─────────────────────────────────────────────────────────────────────────────
# RepoIndex  (singleton)
# ─────────────────────────────────────────────────────────────────────────────

class RepoIndex:
    """
    Singleton.  Call RepoIndex.get_instance().scan(repo_path) once per session.
    Afterwards the three maps are populated and query methods are available.
    """

    _instance: Optional["RepoIndex"] = None
    _lock = threading.Lock()

    def __new__(cls):
        with cls._lock:
            if cls._instance is None:
                inst = super().__new__(cls)
                inst._repo_path: Optional[str] = None
                inst.definitions:  Dict[str, List[DefinitionSite]] = {}
                inst.usages:       Dict[str, Set[str]] = {}
                inst.import_graph: Dict[str, List[ImportEdge]] = {}
                inst._init_files_exports: Dict[str, Set[str]] = {}
                inst._scanned = False
                inst._rlock = threading.RLock()
                cls._instance = inst
        return cls._instance

    @classmethod
    def get_instance(cls) -> "RepoIndex":
        return cls()

    # ── public entry point ────────────────────────────────────────────

    def scan(self, repo_path: str, python_files: Optional[List[str]] = None):
        """
        Scan the entire repository and populate all three maps.

        Args:
            repo_path     — root directory of the repo
            python_files  — optional pre-computed file list (avoids re-glob)
        """
        with self._rlock:
            self._repo_path = str(Path(repo_path).resolve())
            self.definitions.clear()
            self.usages.clear()
            self.import_graph.clear()
            self._init_files_exports.clear()
            self._scanned = False

            if python_files is None:
                python_files = self._discover_files(repo_path)

            logger.info("RepoIndex scanning %d files in %s", len(python_files), repo_path)

            # ── Pass 1: collect definitions + raw source for usage scan ──
            file_sources: Dict[str, str] = {}
            for fp in python_files:
                try:
                    source = Path(fp).read_text(encoding="utf-8")
                    file_sources[fp] = source
                    self._collect_definitions(fp, source)
                    self._collect_imports(fp, source, repo_path)
                except Exception as e:
                    logger.warning("Skipping %s during index scan: %s", Path(fp).name, e)

            # ── Pass 2: collect cross-file usages ─────────────────────
            for fp, source in file_sources.items():
                self._collect_usages(fp, source)

            # ── Pass 3: resolve __init__.py exports ───────────────────
            self._collect_init_exports(repo_path, file_sources)

            # Resolve import edges (map module string → actual file)
            self._resolve_import_edges(repo_path)

            self._scanned = True

            logger.info(
                "RepoIndex complete: %d symbols, %d usages, %d import edges",
                len(self.definitions),
                len(self.usages),
                sum(len(v) for v in self.import_graph.values()),
            )

    # ── is_truly_unused ───────────────────────────────────────────────

    def is_truly_unused(self, symbol_name: str, defining_file: str) -> Tuple[bool, str]:
        """
        Return (is_unused: bool, reason: str).

        A symbol is NOT unused if ANY of the following:
          a) Its name matches an always-alive pattern
          b) Another file imports it from defining_file
          c) Another file references it by name (and has imported defining_file)
          d) It's exported in defining_file's __all__
          e) It's re-exported via an __init__.py
        """
        with self._rlock:
            # ── a) Always-alive names ─────────────────────────────────
            if symbol_name in ALWAYS_ALIVE_PATTERNS:
                return False, "always-alive pattern"

            if symbol_name.startswith("__") and symbol_name.endswith("__"):
                return False, "dunder method/attribute"

            # ── b) Imported by another file directly ──────────────────
            importers = self.get_importers(symbol_name, defining_file)
            if importers:
                return False, f"imported by: {', '.join(Path(f).name for f in importers)}"

            # ── c) Referenced by name in files that import defining_file ─
            abs_defining = str(Path(defining_file).resolve())
            files_that_import_module = self._files_importing_module(abs_defining)
            if symbol_name in self.usages:
                cross_users = self.usages[symbol_name] - {abs_defining}
                relevant = cross_users & files_that_import_module
                if relevant:
                    return (
                        False,
                        f"used in: {', '.join(Path(f).name for f in relevant)}",
                    )

            # ── d) In __all__ ─────────────────────────────────────────
            abs_def = str(Path(defining_file).resolve())
            sites = self.definitions.get(symbol_name, [])
            for site in sites:
                if str(Path(site.file_path).resolve()) == abs_def and site.in_all:
                    return False, "exported in __all__"

            # ── e) Re-exported via __init__.py ────────────────────────
            for init_file, exports in self._init_files_exports.items():
                if symbol_name in exports:
                    # Check if init_file belongs to same package as defining_file
                    init_dir = str(Path(init_file).parent.resolve())
                    if abs_def.startswith(init_dir):
                        return False, f"re-exported via {Path(init_file).name}"

            return True, "no cross-file usage found"

    # ── query helpers ─────────────────────────────────────────────────

    def get_importers(self, symbol_name: str, defining_file: str) -> Set[str]:
        """
        Return set of files that import symbol_name from defining_file.
        Handles 'from module import name' and 'from module import *'.
        """
        abs_def = str(Path(defining_file).resolve())
        importers: Set[str] = set()

        for importing_file, edges in self.import_graph.items():
            for edge in edges:
                if edge.resolved_file == abs_def:
                    if edge.symbol_name in (symbol_name, "*"):
                        importers.add(importing_file)

        return importers

    def get_all_importers_of_file(self, file_path: str) -> Set[str]:
        """Return all files that import ANYTHING from file_path."""
        abs_path = str(Path(file_path).resolve())
        return {
            fp for fp, edges in self.import_graph.items()
            if any(e.resolved_file == abs_path for e in edges)
        }

    def get_definitions(self, symbol_name: str) -> List[DefinitionSite]:
        return self.definitions.get(symbol_name, [])

    def get_usages_in(self, symbol_name: str, file_path: str) -> bool:
        """True if symbol_name appears as a usage in file_path."""
        abs_path = str(Path(file_path).resolve())
        return abs_path in self.usages.get(symbol_name, set())

    def public_api_of(self, file_path: str) -> Set[str]:
        """
        Return the effective public API of a file:
        __all__ contents if defined, otherwise all public names.
        """
        snap = registry.get(file_path)
        if snap:
            return set(snap.get_module_exports())
        return set()

    def summary(self) -> dict:
        return {
            "repo_path":          self._repo_path,
            "scanned":            self._scanned,
            "defined_symbols":    len(self.definitions),
            "symbols_with_usage": len(self.usages),
            "import_edges":       sum(len(v) for v in self.import_graph.values()),
            "init_exports":       sum(len(v) for v in self._init_files_exports.values()),
        }

    # ── internal: scan passes ─────────────────────────────────────────

    def _collect_definitions(self, file_path: str, source: str):
        """Pass 1a: find all function/class/assignment definitions."""
        abs_path = str(Path(file_path).resolve())

        try:
            tree = ast.parse(source)
        except SyntaxError:
            return

        # Determine __all__
        all_names: Set[str] = set()
        for node in tree.body:
            if isinstance(node, ast.Assign):
                for t in node.targets:
                    if isinstance(t, ast.Name) and t.id == "__all__":
                        if isinstance(node.value, (ast.List, ast.Tuple)):
                            for elt in node.value.elts:
                                if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
                                    all_names.add(elt.value)

        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                name = node.name
                site = DefinitionSite(
                    file_path=abs_path,
                    start_line=node.lineno,
                    end_line=node.end_lineno,
                    kind="function",
                    is_public=not name.startswith("_"),
                    in_all=name in all_names,
                )
                self.definitions.setdefault(name, []).append(site)

            elif isinstance(node, ast.ClassDef):
                name = node.name
                site = DefinitionSite(
                    file_path=abs_path,
                    start_line=node.lineno,
                    end_line=node.end_lineno,
                    kind="class",
                    is_public=not name.startswith("_"),
                    in_all=name in all_names,
                )
                self.definitions.setdefault(name, []).append(site)

        # Module-level assignments (constants, singletons)
        for node in tree.body:
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    for name in self._target_names(target):
                        site = DefinitionSite(
                            file_path=abs_path,
                            start_line=node.lineno,
                            end_line=node.end_lineno,
                            kind="assignment",
                            is_public=not name.startswith("_"),
                            in_all=name in all_names,
                        )
                        self.definitions.setdefault(name, []).append(site)
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                name = node.target.id
                site = DefinitionSite(
                    file_path=abs_path,
                    start_line=node.lineno,
                    end_line=node.end_lineno,
                    kind="assignment",
                    is_public=not name.startswith("_"),
                    in_all=name in all_names,
                )
                self.definitions.setdefault(name, []).append(site)

    def _collect_imports(self, file_path: str, source: str, repo_path: str):
        """Pass 1b: collect all import edges (unresolved at this stage)."""
        abs_path = str(Path(file_path).resolve())

        try:
            tree = ast.parse(source)
        except SyntaxError:
            return

        edges: List[ImportEdge] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    mod = alias.name
                    edges.append(ImportEdge(
                        importing_file=abs_path,
                        source_module=mod,
                        symbol_name=alias.asname or alias.name,
                        resolved_file=None,     # resolved in pass 3
                        is_external=self._is_external_module(mod),
                    ))

            elif isinstance(node, ast.ImportFrom) and node.module:
                mod = node.module
                is_ext = self._is_external_module(mod)
                for alias in node.names:
                    sym = alias.name
                    edges.append(ImportEdge(
                        importing_file=abs_path,
                        source_module=mod,
                        symbol_name=sym,
                        resolved_file=None,
                        is_external=is_ext,
                    ))

        if edges:
            self.import_graph[abs_path] = edges

    def _collect_usages(self, file_path: str, source: str):
        """
        Pass 2: find all symbol name references in file_path.
        Uses token-based scan (re.findall) to catch dynamic usages too.
        """
        abs_path = str(Path(file_path).resolve())

        # Find all identifiers in source
        for match in re.finditer(r"\b([A-Za-z_]\w*)\b", source):
            name = match.group(1)
            if len(name) < 2:      # skip single-letter names
                continue
            self.usages.setdefault(name, set()).add(abs_path)

    def _collect_init_exports(self, repo_path: str, file_sources: Dict[str, str]):
        """Pass 3: collect names re-exported via __init__.py files."""
        for fp, source in file_sources.items():
            if Path(fp).name != "__init__.py":
                continue
            abs_path = str(Path(fp).resolve())
            exports: Set[str] = set()
            try:
                tree = ast.parse(source)
                for node in ast.walk(tree):
                    if isinstance(node, ast.ImportFrom):
                        for alias in node.names:
                            exports.add(alias.asname or alias.name)
                    elif isinstance(node, ast.Import):
                        for alias in node.names:
                            exports.add(alias.asname or alias.name)
                if exports:
                    self._init_files_exports[abs_path] = exports
            except SyntaxError:
                pass

    def _resolve_import_edges(self, repo_path: str):
        """
        Resolve each ImportEdge.source_module to an absolute file path.
        External (stdlib/third-party) edges are left with resolved_file=None.
        """
        repo_root = Path(repo_path).resolve()

        for importing_file, edges in self.import_graph.items():
            context_dir = Path(importing_file).parent

            for edge in edges:
                if edge.is_external:
                    continue

                resolved = self._resolve_module(
                    edge.source_module, context_dir, repo_root
                )
                edge.resolved_file = resolved

    def _resolve_module(
        self,
        module: str,
        context_dir: Path,
        repo_root: Path,
    ) -> Optional[str]:
        """
        Attempt to resolve a module string to an absolute .py file path.

        Resolution order:
          1. context_dir/module.py
          2. context_dir/module/__init__.py
          3. repo_root/module.py  (for relative imports without explicit relative dot)
          4. repo_root / module_parts[0] / … /module_parts[-1].py
          5. None (external or unresolvable)
        """
        parts = module.split(".")
        candidates = [
            context_dir / (parts[-1] + ".py"),
            context_dir / "/".join(parts) / "__init__.py",
            context_dir / "/".join(parts[:-1]) / (parts[-1] + ".py") if len(parts) > 1 else None,
            repo_root / (parts[-1] + ".py"),
            repo_root / "/".join(parts) / "__init__.py",
            repo_root / "/".join(parts[:-1]) / (parts[-1] + ".py") if len(parts) > 1 else None,
        ]

        for c in candidates:
            if c and c.exists():
                return str(c.resolve())

        return None

    def _files_importing_module(self, abs_file: str) -> Set[str]:
        """Return all files that have at least one import edge resolving to abs_file."""
        result: Set[str] = set()
        for fp, edges in self.import_graph.items():
            if any(e.resolved_file == abs_file for e in edges):
                result.add(fp)
        return result

    # ── utilities ─────────────────────────────────────────────────────

    @staticmethod
    def _is_external_module(module_name: str) -> bool:
        """True for stdlib and known third-party packages."""
        top_level = module_name.split(".")[0]
        if top_level in STDLIB_MODULES:
            return True
        # Heuristic: well-known third-party roots
        KNOWN_THIRD_PARTY = {
            "django", "flask", "fastapi", "sqlalchemy", "pydantic",
            "requests", "aiohttp", "celery", "redis", "boto3",
            "numpy", "pandas", "scipy", "sklearn", "torch", "tensorflow",
            "langchain", "langgraph", "anthropic", "openai",
            "pytest", "click", "typer", "rich", "radon", "bandit",
        }
        return top_level in KNOWN_THIRD_PARTY

    @staticmethod
    def _target_names(target: ast.expr) -> List[str]:
        if isinstance(target, ast.Name):
            return [target.id]
        if isinstance(target, (ast.Tuple, ast.List)):
            names = []
            for elt in target.elts:
                names.extend(RepoIndex._target_names(elt))
            return names
        return []

    @staticmethod
    def _discover_files(
        repo_path: str,
        exclude: Optional[List[str]] = None,
    ) -> List[str]:
        if exclude is None:
            exclude = [
                "venv", ".venv", "__pycache__", ".git",
                ".tox", "node_modules", "dist", "build", ".eggs",
            ]
        result = []
        for fp in Path(repo_path).rglob("*.py"):
            if not any(pat in str(fp) for pat in exclude):
                result.append(str(fp))
        return result


# ── Module-level singleton ────────────────────────────────────────────────────

repo_index = RepoIndex.get_instance()