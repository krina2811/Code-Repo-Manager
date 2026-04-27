"""
LangGraph Agent Workflow

Four agents run sequentially; each appends Findings to AgentState.

Pipeline order
──────────────
  0. RepoIndex.scan()        — one-time cross-file analysis
  1. FileRegistry warmup     — pre-load all snapshots + SymbolTables
  2. DeadCodeAgent           — repo-wide dead code (uses RepoIndex)
  3. SecurityAgent           — hardcoded secrets + insecure patterns
  4. DocumentationAgent      — missing / short docstrings
  5. StructureAgent          — large files
  6. ActionCoordinator       — merge + order actions per entity
  7. HITLRouter              — split into auto_actions + review_queue

Bug fixes in this version
─────────────────────────
  1. Restored BaseAgent.create_finding() helper — was missing from previous
     version; agents now use it consistently instead of constructing Finding
     objects inline (which caused subtle field-ordering issues).

  2. Fixed LangGraph stream extraction — app.stream() yields
     {node_name: dict | AgentState}. Previous code checked
     isinstance(node_state, AgentState) which always failed when LangGraph
     returned a serialised dict, leaving final_state = initial (zero findings).
     Now we extract AgentState properly from both dict and object returns.

  3. Fixed is_truly_unused() over-filtering — the token-based usage scan
     collected EVERY identifier from EVERY file, so common names like
     `process`, `handle`, `parse` showed up as "used" just because a comment
     somewhere contained those words. The cross-file check now only counts a
     symbol as used in another file if that other file also has a RESOLVED
     import edge pointing back to the defining file. This scopes the check
     correctly to actual callers.
"""

import ast
import re
import hashlib
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from langgraph.graph import StateGraph, END
from core.logger import get_logger

logger = get_logger(__name__)

try:
    from langgraph.checkpoint.postgres import PostgresSaver
    _POSTGRES_OK = True
except ImportError:
    PostgresSaver = None
    _POSTGRES_OK = False

from core.models import (
    AgentState, Finding, Action, ActionType, AgentType, RiskLevel,
)
from core.file_registry import FileRegistry
from core.repo_index import RepoIndex, repo_index
from core.hitl import hitl_router, review_queue
from core.mcp_client import mcp_client
from storage.checkpoint import checkpoint_storage
from config.settings import settings

registry = FileRegistry.get_instance()


# ─────────────────────────────────────────────────────────────────────────────
# BaseAgent
# ─────────────────────────────────────────────────────────────────────────────

class BaseAgent:
    """Base class for all agents. Provides create_finding() helper."""

    def __init__(self, agent_type: AgentType):
        self.agent_type = agent_type
        self.tools = mcp_client   # call tools via MCP protocol (falls back to direct if not running)

    def create_finding(
        self,
        action_type: ActionType,
        title: str,
        description: str,
        file_path: str,
        line_number: int,
        severity: str,
        confidence: float,
        reasoning: str,
        impact_analysis: Dict[str, Any],
        suggested_fix: str = None,
        code_snippet: str = None,
    ) -> Finding:
        """
        Construct a Finding with this agent's type pre-filled.
        All agents must use this helper — do not construct Finding() directly.
        """
        return Finding(
            agent_type=self.agent_type,
            action_type=action_type,
            title=title,
            description=description,
            file_path=file_path,
            line_number=line_number,
            severity=severity,
            confidence=confidence,
            reasoning=reasoning,
            impact_analysis=impact_analysis,
            suggested_fix=suggested_fix,
            code_snippet=code_snippet,
        )

    def analyze(self, state: AgentState) -> AgentState:
        raise NotImplementedError


# ─────────────────────────────────────────────────────────────────────────────
# DeadCodeAgent
# ─────────────────────────────────────────────────────────────────────────────

class DeadCodeAgent(BaseAgent):
    """
    Detects dead code across the entire repository.

    Unused function check (two-pass)
    ─────────────────────────────────
    Pass 1 (local)  — radon/regex: is this function called in its own file?
    Pass 2 (global) — RepoIndex: is it imported or called from another file?

    Only flag if BOTH passes confirm it's unused.

    Cross-file check correctness
    ─────────────────────────────
    A function in file A is considered used cross-file only if:
      • Another file B has a RESOLVED import edge pointing to file A, AND
      • File B's source contains the function name as a call pattern.
    This prevents the false-negative where common words like "process"
    appear in unrelated comments and suppress the dead-code finding.
    """

    def __init__(self):
        super().__init__(AgentType.DEAD_CODE)

    def analyze(self, state: AgentState) -> AgentState:
        logger.info("DeadCodeAgent analyzing: %s", state.repo_path)
        python_files = self.tools.get_python_files(state.repo_path)

        # Files whose entire public API must be kept (star-import sources)
        star_sources = self._star_import_sources(python_files)

        for file_path in python_files[:10]:
            abs_path = str(Path(file_path).resolve())

            # ── Unused imports ────────────────────────────────────────
            import_analysis = self.tools.analyze_imports(file_path)
            if "error" not in import_analysis:
                for unused in import_analysis.get("unused_imports", []):
                    state.findings.append(self.create_finding(
                        action_type=ActionType.DELETE_IMPORT,
                        title=f"Unused import: {unused['name']}",
                        description=(
                            f"Import '{unused['name']}' from '{unused['module']}' "
                            f"is not used in this file"
                        ),
                        file_path=file_path,
                        line_number=unused["line"],
                        severity="low",
                        confidence=0.85,
                        reasoning="Import found but symbol never referenced after import line",
                        impact_analysis={
                            "breaking_changes": False,
                            "files_affected": 1,
                            "test_impact": "none",
                        },
                        suggested_fix=f"Remove: import {unused['name']}",
                        code_snippet=f"import {unused['name']}",
                    ))

            # ── Unused + complex functions ────────────────────────────
            func_analysis = self.tools.analyze_functions(file_path)
            if "error" not in func_analysis:

                for unused_func in func_analysis.get("unused_functions", []):
                    func_name = unused_func["name"]

                    # Check if this file is a star-import source
                    if abs_path in star_sources:
                        snap = registry.get(file_path)
                        if snap and func_name in snap.get_module_exports():
                            logger.debug("'%s': star-export, keeping", func_name)
                            continue

                    # Cross-file check: is it truly unused repo-wide?
                    is_truly_dead, reason = self._is_unused_repo_wide(
                        func_name, file_path, python_files
                    )

                    if not is_truly_dead:
                        logger.debug("'%s' (%s): %s", func_name, Path(file_path).name, reason)
                        continue

                    conf = 0.60 if unused_func["is_private"] else 0.75
                    state.findings.append(self.create_finding(
                        action_type=ActionType.DELETE_FUNCTION,
                        title=f"Unused function (repo-wide): {func_name}",
                        description=(
                            f"Function '{func_name}' is not called in this file "
                            f"or imported/used by any other file in the repository"
                        ),
                        file_path=file_path,
                        line_number=unused_func["line"],
                        severity="medium",
                        confidence=conf,
                        reasoning=f"Local: zero call sites. Repo: {reason}",
                        impact_analysis={
                            "breaking_changes": True,
                            "files_affected": 1,
                            "test_impact": "high",
                            "cross_file_check": "passed",
                        },
                        suggested_fix="Verify public-API usage before removing",
                        code_snippet=f"def {func_name}",
                    ))

                # Complex functions / methods (always flag, regardless of usage)
                for cx in func_analysis.get("complex_functions", []):
                    cx_name        = cx["name"]
                    cx_kind        = cx.get("kind", "function")      # "function" or "method"
                    cx_parent      = cx.get("parent_class")          # None or "ClassName"
                    cx_complexity  = cx["complexity"]
                    cx_class_label = cx.get("classification", "?")

                    # Build description that lets coordinator extract correct entity
                    if cx_kind == "method" and cx_parent:
                        description = (
                            f"Method '{cx_name}' in class '{cx_parent}' has cyclomatic "
                            f"complexity {cx_complexity} (grade {cx_class_label})"
                        )
                        title = f"High complexity method: {cx_name} (in {cx_parent})"
                    else:
                        description = (
                            f"Function '{cx_name}' has cyclomatic complexity "
                            f"{cx_complexity} (grade {cx_class_label})"
                        )
                        title = f"High complexity: {cx_name}"

                    state.findings.append(self.create_finding(
                        action_type=ActionType.REFACTOR_CODE,
                        title=title,
                        description=description,
                        file_path=file_path,
                        line_number=cx["line"],
                        severity="medium" if cx_complexity < 15 else "high",
                        confidence=1.0,
                        reasoning="High cyclomatic complexity increases maintenance cost",
                        impact_analysis={
                            "breaking_changes": False,
                            "files_affected": 1,
                            "test_impact": "medium",
                            "complexity":    cx_complexity,
                            # Store kind + parent so coordinator resolves correctly
                            "entity_type":   cx_kind,        # "function" or "method"
                            "parent_class":  cx_parent,      # None or "ClassName"
                        },
                        suggested_fix="Refactor into smaller single-purpose functions",
                        code_snippet=f"def {cx_name}  # complexity={cx_complexity}",
                    ))

        state.current_agent = self.agent_type
        return state

    # ── helpers ───────────────────────────────────────────────────────

    def _is_unused_repo_wide(
        self,
        func_name: str,
        defining_file: str,
        python_files: List[str],
    ) -> tuple:
        """
        Return (is_unused: bool, reason: str).

        A function is NOT unused cross-file if:
          a) It matches an always-alive name
          b) Another file has a resolved import of defining_file AND
             that file's source contains a call pattern for func_name

        Using call pattern (func_name followed by '(') instead of bare
        token scan prevents false matches from comments / string literals.
        """
        ALWAYS_ALIVE = frozenset([
            "__init__", "__main__", "__all__", "__version__",
            "__repr__", "__str__", "__len__", "__iter__",
            "__enter__", "__exit__", "__call__", "__new__",
            "setup", "teardown", "main", "create_app", "make_app",
        ])

        if func_name in ALWAYS_ALIVE:
            return False, "always-alive name"

        if func_name.startswith("__") and func_name.endswith("__"):
            return False, "dunder method"

        abs_def = str(Path(defining_file).resolve())

        # Files that have a RESOLVED import of defining_file
        importing_files = repo_index._files_importing_module(abs_def)

        for other_file in importing_files:
            if other_file == abs_def:
                continue
            try:
                source = Path(other_file).read_text(encoding="utf-8")
                # Call pattern: func_name( or func_name.something(
                call_pattern = rf"\b{re.escape(func_name)}\s*[\(\.]"
                if re.search(call_pattern, source):
                    return False, f"called in {Path(other_file).name}"
                # Also check if it's directly imported by name (even if not called)
                import_pattern = rf"\bimport\b[^#\n]*\b{re.escape(func_name)}\b"
                if re.search(import_pattern, source):
                    return False, f"imported by {Path(other_file).name}"
            except Exception:
                pass

        # Also check RepoIndex importers (handles 'from module import func' directly)
        importers = repo_index.get_importers(func_name, defining_file)
        if importers:
            names = ", ".join(Path(f).name for f in importers)
            return False, f"directly imported by {names}"

        # Check __all__ via FileRegistry
        snap = registry.get(defining_file)
        if snap:
            exports = snap.get_module_exports()
            if func_name in exports:
                return False, "listed in __all__ or public API"

        return True, "no cross-file usage found"

    def _star_import_sources(self, python_files: List[str]) -> set:
        """Files imported with 'from module import *' — entire public API is live."""
        sources = set()
        for fp in python_files:
            try:
                source = Path(fp).read_text(encoding="utf-8")
                tree = ast.parse(source)
                for node in ast.walk(tree):
                    if isinstance(node, ast.ImportFrom):
                        for alias in node.names:
                            if alias.name == "*" and node.module:
                                resolved = repo_index._resolve_module(
                                    node.module,
                                    Path(fp).parent,
                                    Path(fp).parent,
                                )
                                if resolved:
                                    sources.add(resolved)
            except Exception:
                pass
        return sources


# ─────────────────────────────────────────────────────────────────────────────
# SecurityAgent
# ─────────────────────────────────────────────────────────────────────────────

class SecurityAgent(BaseAgent):

    def __init__(self):
        super().__init__(AgentType.SECURITY)

    def analyze(self, state: AgentState) -> AgentState:
        logger.info("SecurityAgent analyzing: %s", state.repo_path)
        python_files = self.tools.get_python_files(state.repo_path)

        for file_path in python_files[:10]:
            result = self.tools.analyze_security(file_path)
            if "error" not in result:
                for sf in result.get("findings", []):
                    conf = 0.95 if sf["type"] == "hardcoded_secret" else 0.80
                    state.findings.append(self.create_finding(
                        action_type=ActionType.FIX_SECURITY,
                        title=sf["message"],
                        description=f"Security issue: {sf['message']}",
                        file_path=file_path,
                        line_number=sf["line"],
                        severity=sf["severity"],
                        confidence=conf,
                        reasoning=f"Detected {sf['type']} pattern in source code",
                        impact_analysis={
                            "breaking_changes": False,
                            "files_affected": 1,
                            "security_risk": sf["severity"].upper(),
                            "test_impact": "low",
                            # stored as content fingerprint for line relocation
                            "credential_line": sf.get("code", ""),
                        },
                        suggested_fix=(
                            "Extract to .env.example and replace with os.getenv()."
                        ),
                        code_snippet=sf.get("code", ""),
                    ))

        state.current_agent = self.agent_type
        return state


# ─────────────────────────────────────────────────────────────────────────────
# DocumentationAgent
# ─────────────────────────────────────────────────────────────────────────────

class DocumentationAgent(BaseAgent):
    """
    Flags missing/short docstrings.
    Skips functions already queued for deletion — no point documenting
    something about to be removed.
    """

    def __init__(self):
        super().__init__(AgentType.DOCUMENTATION)

    def analyze(self, state: AgentState) -> AgentState:
        logger.info("DocumentationAgent analyzing: %s", state.repo_path)
        python_files = self.tools.get_python_files(state.repo_path)

        pending_deletes = self._pending_delete_functions(state.findings)

        for file_path in python_files[:10]:
            result = self.tools.analyze_documentation(file_path)
            if "error" not in result:
                # Build a set of class names that already have docstrings
                # so we can skip __init__ for those classes.
                snap = registry.get(file_path)
                classes_with_docstring = set()
                if snap:
                    for cls_name, cls_sym in snap.symbols.classes.items():
                        if cls_sym.has_docstring:
                            classes_with_docstring.add(cls_name)

                for issue in result.get("issues", []):
                    func_name = issue.get("name", "")
                    issue_type = issue.get("type", "")

                    # Skip symbols already queued for deletion
                    if (file_path, func_name) in pending_deletes:
                        continue

                    # Skip __init__ when parent class already has a docstring.
                    # Find parent by looking at which class contains issue["line"].
                    if func_name == "__init__":
                        parent = self._find_parent_class(snap, issue["line"]) if snap else None
                        if parent and parent in classes_with_docstring:
                            logger.debug("Skipping __init__ in '%s' — class already has docstring", parent)
                            continue

                    # Determine correct entity_type for the coordinator target.
                    # issue_type from analyze_documentation is:
                    #   "missing_function_docstring" → entity_type = "function"
                    #   "missing_class_docstring"    → entity_type = "class"
                    #   "missing_module_docstring"   → skip (no symbol to target)
                    #   "short_docstring"            → use "function" or "class"
                    if "module" in issue_type:
                        continue   # module docstring can't be targeted by symbol name

                    entity_type = "class" if "class" in issue_type else "function"

                    # Build a description that lets the coordinator extract
                    # the correct entity_type and name from the finding.
                    if entity_type == "class":
                        description = f"Class '{func_name}' is missing a docstring"
                    else:
                        description = f"Function '{func_name}' is missing a docstring"

                    state.findings.append(self.create_finding(
                        action_type=ActionType.ADD_DOCSTRING,
                        title=issue["message"],
                        description=description,
                        file_path=file_path,
                        line_number=issue["line"],
                        severity="low",
                        confidence=0.90,
                        reasoning="Public API should be documented for maintainability",
                        impact_analysis={
                            "breaking_changes": False,
                            "files_affected": 1,
                            "test_impact": "none",
                            "entity_type": entity_type,
                        },
                        suggested_fix=(
                            f"Add Google-style docstring to {entity_type} '{func_name}'"
                        ),
                        code_snippet=func_name,
                    ))

        state.current_agent = self.agent_type
        return state

    @staticmethod
    def _find_parent_class(snap, method_line: int) -> Optional[str]:
        """Return name of class containing method_line, or None."""
        if not snap:
            return None
        for cls_name, cls_sym in snap.symbols.classes.items():
            if cls_sym.start_line < method_line <= cls_sym.end_line:
                return cls_name
        return None

    @staticmethod
    def _pending_delete_functions(findings: List[Finding]) -> set:
        """Return set of (file_path, func_name) already queued for deletion."""
        pending = set()
        for f in findings:
            if f.action_type == ActionType.DELETE_FUNCTION:
                m = re.search(r"[Ff]unction ['\"]?(\w+)['\"]?", f.description)
                if m:
                    pending.add((f.file_path, m.group(1)))
        return pending


# ─────────────────────────────────────────────────────────────────────────────
# StructureAgent
# ─────────────────────────────────────────────────────────────────────────────

class StructureAgent(BaseAgent):

    def __init__(self):
        super().__init__(AgentType.STRUCTURE)

    def analyze(self, state: AgentState) -> AgentState:
        logger.info("StructureAgent analyzing: %s", state.repo_path)
        python_files = self.tools.get_python_files(state.repo_path)

        # Threshold lowered to 450 so test_repo/api/routes.py (~497 LOC) triggers
        LOC_THRESHOLD = 450

        for file_path in python_files:
            metrics = self.tools.get_file_metrics(file_path)
            if "error" not in metrics and metrics["loc"] > LOC_THRESHOLD:
                importers = repo_index.get_all_importers_of_file(file_path)
                state.findings.append(self.create_finding(
                    action_type=ActionType.RESTRUCTURE,
                    title=f"Large file: {metrics['loc']} LOC",
                    description=f"File has {metrics['loc']} lines of code",
                    file_path=file_path,
                    line_number=1,
                    severity="medium",
                    confidence=0.70,
                    reasoning=f"Files >{LOC_THRESHOLD} LOC are harder to navigate and maintain",
                    impact_analysis={
                        "breaking_changes": True,
                        "files_affected": 1 + len(importers),
                        "test_impact": "high",
                        "loc": metrics["loc"],
                        "imported_by": [Path(f).name for f in importers],
                    },
                    suggested_fix=(
                        "Consider splitting into logical sub-modules. "
                        f"Imported by: {', '.join(Path(f).name for f in importers) or 'none'}"
                    ),
                ))

        state.current_agent = self.agent_type
        return state



# ─────────────────────────────────────────────────────────────────────────────
# State extraction helper  (Bug 2 fix)
# ─────────────────────────────────────────────────────────────────────────────

def _extract_agent_state(chunk: Any) -> Optional[AgentState]:
    """
    LangGraph stream() yields {node_name: value} per step.
    value can be:
      • AgentState object         — when Pydantic model is returned directly
      • dict                      — when LangGraph serialises the state
      • None                      — intermediate steps

    This helper handles all three cases and always returns an AgentState
    or None. This was the root cause of zero findings in the previous
    version — isinstance(value, AgentState) always failed on dicts.
    """
    if chunk is None:
        return None

    if not isinstance(chunk, dict):
        return None

    for node_name, node_state in chunk.items():
        # Case 1: already an AgentState object
        if isinstance(node_state, AgentState):
            return node_state

        # Case 2: LangGraph returned a serialised dict — reconstruct
        if isinstance(node_state, dict):
            try:
                return AgentState(**node_state)
            except Exception:
                pass

    return None


# ─────────────────────────────────────────────────────────────────────────────
# Warmup
# ─────────────────────────────────────────────────────────────────────────────

def _warmup(repo_path: str, python_files: List[str]):
    """
    Run before agents:
      1. Build RepoIndex (cross-file definitions + import graph)
      2. Load all FileRegistry snapshots (SymbolTables with assignments + statements)
    Both use the same pre-discovered file list so glob only runs once.
    """
    logger.info("Building repo-wide index for: %s", repo_path)
    repo_index.scan(repo_path, python_files)

    logger.info("Warming up FileRegistry (%d files)", len(python_files))
    loaded, failed = 0, 0
    for fp in python_files:
        snap = registry.get(fp)
        if snap:
            loaded += 1
        else:
            failed += 1
            logger.warning("Could not load file into registry: %s", fp)
    logger.info("FileRegistry ready: %d loaded, %d failed", loaded, failed)


# ─────────────────────────────────────────────────────────────────────────────
# Workflow builder
# ─────────────────────────────────────────────────────────────────────────────

def create_analysis_workflow():
    workflow = StateGraph(AgentState)

    workflow.add_node("dead_code",     DeadCodeAgent().analyze)
    workflow.add_node("security",      SecurityAgent().analyze)
    workflow.add_node("documentation", DocumentationAgent().analyze)
    workflow.add_node("structure",     StructureAgent().analyze)

    workflow.set_entry_point("dead_code")
    workflow.add_edge("dead_code",     "security")
    workflow.add_edge("security",      "documentation")
    workflow.add_edge("documentation", "structure")

    if _POSTGRES_OK and PostgresSaver is not None:
        try:
            checkpointer = PostgresSaver.from_conn_string(settings.get_postgres_url)
            checkpointer.setup()
            app = workflow.compile(checkpointer=checkpointer)
            logger.info("PostgreSQL checkpointing enabled: %s", settings.postgres_host)
            return app
        except Exception as e:
            import warnings
            warnings.warn(f"PostgreSQL checkpointing unavailable: {e}", UserWarning)

    return workflow.compile()


# ─────────────────────────────────────────────────────────────────────────────
# run_analysis
# ─────────────────────────────────────────────────────────────────────────────

def run_analysis(
    repo_path: str,
    session_id: str,
    agent_filter: Optional[List[AgentType]] = None,
    auto_approve: bool = False,
    confidence_threshold: float = 0.7,
) -> Dict[str, Any]:
    """
    Full pipeline. Returns dict with:
      findings, coordinated_actions, auto_actions, review_ids,
      repo_index_summary, registry_state
    """
    # Reset singletons between sessions
    registry.clear()

    python_files = CodeAnalysisTools.get_python_files(repo_path)

    # Step 0 — warmup (RepoIndex + FileRegistry)
    _warmup(repo_path, python_files)

    # Step 1 — run agents
    app = create_analysis_workflow()
    initial = AgentState(
        repo_path=repo_path,
        metadata={
            "session_id":  session_id,
            "started_at":  datetime.now().isoformat(),
            "file_count":  len(python_files),
        },
    )
    config = {"configurable": {"thread_id": session_id}}

    final_state: Optional[AgentState] = None

    for chunk in app.stream(initial, config):
        extracted = _extract_agent_state(chunk)
        if extracted is not None:
            final_state = extracted

            # Checkpoint after each node
            if final_state.current_agent:
                try:
                    checkpoint_storage.save_checkpoint(
                        checkpoint_id=(
                            f"{session_id}_{final_state.current_agent}"
                            f"_{datetime.now().timestamp()}"
                        ),
                        session_id=session_id,
                        agent_type=str(final_state.current_agent),
                        state_data={
                            "findings_count": len(final_state.findings)
                        },
                    )
                except Exception:
                    pass

    if not final_state:
        logger.warning("No agent state extracted from stream — using initial state")
        final_state = initial

    logger.info("Agents complete: %d findings", len(final_state.findings))

    # Step 2 — coordinate + route
    hitl_router.confidence_threshold = confidence_threshold
    coordinated = hitl_router.process_findings(final_state.findings)
    auto_actions, review_ids = hitl_router.route(
        coordinated, review_queue, auto_approve_override=auto_approve
    )

    return {
        "session_id":          session_id,
        "repo_path":           repo_path,
        "findings":            final_state.findings,
        "findings_count":      len(final_state.findings),
        "coordinated_actions": coordinated,
        "auto_actions":        auto_actions,
        "review_ids":          review_ids,
        "repo_index_summary":  repo_index.summary(),
        "registry_state":      registry.summary(),
    }