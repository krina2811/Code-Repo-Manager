"""
Action Coordinator

Responsibilities
────────────────
1. Receive raw Findings from all agents.
2. Group them by (file, entity_type, entity_name) — the "entity key".
3. Merge duplicate/overlapping actions per entity (e.g. ADD_DOCSTRING +
   DELETE_FUNCTION on the same function → only DELETE_FUNCTION survives).
4. Produce symbol-based Action objects whose targets never contain raw
   line numbers; the executor resolves current lines from FileRegistry
   just before touching disk.
5. Order final actions so that within each file, deletes execute bottom-
   to-top (so an upper deletion cannot shift the start of a lower one),
   and non-destructive actions follow.

Entity type vocabulary (matches SymbolTable kinds)
──────────────────────────────────────────────────
  function   — def / async def
  class      — class Foo:
  import     — import os  /  from x import y
  assignment — module-level variable  BASE_URL = "…"
  statement  — module-level block     if __name__ == "__main__":
  line       — security issues (never merged; identified by line number)
  file       — whole-file actions
  dependency — package dependency
  structure  — structural split suggestion
"""

import re
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

from core.models import Finding, Action, ActionType, RiskLevel
from core.file_registry import FileRegistry
from core.logger import get_logger

logger = get_logger(__name__)

registry = FileRegistry.get_instance()

# ── Risk levels ───────────────────────────────────────────────────────────────

ACTION_RISK_LEVELS: Dict[ActionType, RiskLevel] = {
    ActionType.DELETE_FILE:       RiskLevel.CRITICAL,
    ActionType.DELETE_FUNCTION:   RiskLevel.HIGH,
    ActionType.DELETE_IMPORT:     RiskLevel.LOW,
    ActionType.MOVE_FILE:         RiskLevel.HIGH,
    ActionType.RESTRUCTURE:       RiskLevel.CRITICAL,
    ActionType.ADD_DOCSTRING:     RiskLevel.LOW,
    ActionType.FIX_SECURITY:      RiskLevel.HIGH,
    ActionType.REFACTOR_CODE:     RiskLevel.MEDIUM,
}

# Lower index = executed first within a file.
# FIX_SECURITY first: credentials are always safe to fix before structural changes.
# DELETE_IMPORT next: auto-approved, low-risk cleanup.
# DELETE_FUNCTION before REFACTOR/DOCSTRING: dead code gone first.
# REFACTOR_CODE before ADD_DOCSTRING: refactor the complex function first,
# then generate the docstring on the already-simplified version.
ACTION_PRIORITY: List[ActionType] = [
    ActionType.FIX_SECURITY,
    ActionType.DELETE_FILE,
    ActionType.DELETE_IMPORT,     # auto-approved, low-risk
    ActionType.DELETE_FUNCTION,
    ActionType.REFACTOR_CODE,     # ← refactor first, docstring describes clean code
    ActionType.ADD_DOCSTRING,
    ActionType.RESTRUCTURE,
    ActionType.MOVE_FILE,
]

# Action pairs that are COMPATIBLE — both execute sequentially on one approve.
# Key = primary action (executes first). Value = secondary actions after.
# REFACTOR_CODE is primary so it runs before ADD_DOCSTRING — the docstring
# is then written onto the already-refactored function body.
COMPATIBLE_PAIRS: Dict[ActionType, List[ActionType]] = {
    ActionType.REFACTOR_CODE: [ActionType.ADD_DOCSTRING],
    ActionType.ADD_DOCSTRING: [ActionType.REFACTOR_CODE],
}

# If a DELETE exists for an entity, these lower-priority actions are dropped
_DELETE_DOMINATES = frozenset([ActionType.ADD_DOCSTRING, ActionType.REFACTOR_CODE])


# ─────────────────────────────────────────────────────────────────────────────
# Entity extraction
# ─────────────────────────────────────────────────────────────────────────────

def _extract_entity(finding: Finding) -> Tuple[str, str]:
    """
    Determine (entity_type, entity_name) from a Finding.

    entity_type vocabulary
    ──────────────────────
    function   → symbol lookup in SymbolTable.functions
    class      → SymbolTable.classes
    import     → SymbolTable.imports
    assignment → SymbolTable.assignments  (NEW)
    statement  → SymbolTable.statements   (NEW — named "if_block_L42" etc.)
    line       → security issues, resolved via line number (never merged)
    file       → whole-file actions
    dependency → package / requirements actions
    structure  → restructure suggestions
    """

    def search(text: str, patterns: List[str]) -> Optional[str]:
        for pat in patterns:
            m = re.search(pat, text)
            if m:
                return m.group(1)
        return None

    # ── FUNCTION ──────────────────────────────────────────────────────
    if finding.action_type in (
        ActionType.DELETE_FUNCTION,
        ActionType.ADD_DOCSTRING,
        ActionType.REFACTOR_CODE,
    ):
        hint_type   = finding.impact_analysis.get("entity_type", "")
        parent_class = finding.impact_analysis.get("parent_class")
        desc_start  = finding.description[:40].lower()

        # ── REFACTOR_CODE: method inside a class ─────────────────────
        # DeadCodeAgent stores entity_type="method" and parent_class="ClassName"
        # in impact_analysis when radon identifies a method as complex.
        # We look up the function by line number (most reliable for methods)
        # and return ("function", method_name) — the executor looks in
        # snap.symbols.functions which covers methods too.
        if hint_type == "method" and finding.action_type == ActionType.REFACTOR_CODE:
            if finding.line_number:
                snap = registry.get(finding.file_path)
                if snap:
                    for fn_name, sym in snap.symbols.functions.items():
                        if sym.start_line == finding.line_number:
                            return ("function", fn_name)
            # Fallback: extract method name from description
            method_patterns = [
                r"[Mm]ethod '(\w+)'",
                r"[Mm]ethod '(\w+)'",
                r"[Mm]ethod:?\s+(\w+)",
            ]
            for text in (finding.description, finding.title):
                name = search(text, method_patterns)
                if name:
                    return ("function", name)

        # ── CLASS: DocumentationAgent sets entity_type="class" ───────
        if hint_type == "class" or desc_start.startswith("class "):
            cls_patterns = [
                r"[Cc]lass '(\w+)'",
                r"[Cc]lass '(\w+)'",
                r"[Cc]lass (\w+) is missing",
            ]
            for text in (finding.description, finding.title):
                name = search(text, cls_patterns)
                if name:
                    return ("class", name)
            if finding.line_number:
                snap = registry.get(finding.file_path)
                if snap:
                    for cls_name, sym in snap.symbols.classes.items():
                        if sym.start_line == finding.line_number:
                            return ("class", cls_name)

        # ── FUNCTION: default path ────────────────────────────────────
        fn_patterns = [
            r"[Ff]unction '(\w+)'",
            r"[Ff]unction '(\w+)'",
            r"[Ff]unction '(\w+)' is missing",
            r"[Ff]unction:?\s+(\w+)",
            r"(?:Potentially unused function|Missing docstring for"
            r"|High complexity|High complexity method):\s*(\w+)",
            r"[Uu]nused:?\s+(\w+)",
        ]
        for text in (finding.description, finding.title):
            name = search(text, fn_patterns)
            if name:
                return ("function", name)

        # ── Line-number fallback ──────────────────────────────────────
        if finding.line_number:
            snap = registry.get(finding.file_path)
            if snap:
                for fn_name, sym in snap.symbols.functions.items():
                    if sym.start_line == finding.line_number:
                        return ("function", fn_name)
                for cls_name, sym in snap.symbols.classes.items():
                    if sym.start_line == finding.line_number:
                        return ("class", cls_name)

        return ("function", f"line_{finding.line_number}")

    # ── IMPORT ────────────────────────────────────────────────────────
    if finding.action_type == ActionType.DELETE_IMPORT:
        imp_patterns = [
            r"[Ii]mport ['\"]?(\w+)['\"]?",
            r"[Uu]nused import:?\s+['\"]?(\w+)['\"]?",
            r"'(\w+)' from '",
        ]
        for text in (finding.description, finding.title):
            name = search(text, imp_patterns)
            if name:
                return ("import", name)

        # Fallback: look up by line in registry
        if finding.line_number:
            snap = registry.get(finding.file_path)
            if snap:
                for imp_name, sym in snap.symbols.imports.items():
                    if sym.start_line == finding.line_number:
                        return ("import", imp_name)

        return ("import", f"line_{finding.line_number}")

    # ── SECURITY — resolve variable name from AST assignments ────────
    # Module-level secrets are tracked in snap.symbols.assignments.
    # Using @variable:VAR_NAME lets the executor resolve the CURRENT
    # line from the live SymbolTable (rebuilt after every edit), so the
    # target stays valid even after delete_function / delete_import
    # shifts lines above the credential.
    if finding.action_type == ActionType.FIX_SECURITY:
        if finding.line_number:
            snap = registry.get(finding.file_path)
            if snap:
                for asgn_name, sym in snap.symbols.assignments.items():
                    if sym.start_line == finding.line_number:
                        return ("variable", asgn_name)
        # Fallback: credential is inside a function or line lookup failed
        return ("line", str(finding.line_number))

    # ── ASSIGNMENT (module-level variable) ────────────────────────────
    # StructureAgent or future agents may flag module-level assignments
    if finding.action_type in (ActionType.REFACTOR_CODE, ActionType.DELETE_FUNCTION):
        if "variable" in finding.title.lower() or "constant" in finding.title.lower():
            var_patterns = [
                r"[Vv]ariable ['\"]?(\w+)['\"]?",
                r"[Cc]onstant ['\"]?(\w+)['\"]?",
                r"[Aa]ssignment ['\"]?(\w+)['\"]?",
            ]
            for text in (finding.description, finding.title):
                name = search(text, var_patterns)
                if name:
                    return ("assignment", name)

            # Fallback: look up by line in registry
            if finding.line_number:
                snap = registry.get(finding.file_path)
                if snap:
                    for asgn_name, sym in snap.symbols.assignments.items():
                        if sym.start_line == finding.line_number:
                            return ("assignment", asgn_name)

    # ── STATEMENT (module-level block) ────────────────────────────────
    # If a finding targets a module-level block (if __name__==__main__, etc.)
    if finding.action_type == ActionType.RESTRUCTURE:
        if finding.line_number:
            snap = registry.get(finding.file_path)
            if snap:
                stmt = snap.symbols.find_statement_at(finding.line_number)
                if stmt:
                    return ("statement", stmt.name)   # e.g. "if_block_L1"


    # ── FILE ──────────────────────────────────────────────────────────
    if finding.action_type in (ActionType.DELETE_FILE, ActionType.MOVE_FILE):
        return ("file", Path(finding.file_path).name)

    # ── RESTRUCTURE ───────────────────────────────────────────────────
    if finding.action_type == ActionType.RESTRUCTURE:
        struct_patterns = [
            r"[Cc]lass ['\"]?(\w+)['\"]?",
            r"[Mm]odule ['\"]?(\w+)['\"]?",
        ]
        for text in (finding.description, finding.title):
            name = search(text, struct_patterns)
            if name:
                return ("structure", name)
        return ("structure", Path(finding.file_path).stem)

    # ── CLASS ─────────────────────────────────────────────────────────
    if "class" in finding.title.lower() or "class" in finding.description.lower():
        cls_patterns = [r"[Cc]lass ['\"]?(\w+)['\"]?"]
        for text in (finding.description, finding.title):
            name = search(text, cls_patterns)
            if name:
                return ("class", name)

    # Unique fallback — prevents unwanted merging
    return ("line", str(finding.line_number))


# ─────────────────────────────────────────────────────────────────────────────
# EntityGroup
# ─────────────────────────────────────────────────────────────────────────────

class EntityGroup:
    """
    All findings targeting the same code entity
    (file_path + entity_type + entity_name).
    """

    def __init__(self, entity_type: str, entity_name: str, file_path: str):
        self.entity_type = entity_type
        self.entity_name = entity_name
        self.file_path   = file_path
        self.findings:     List[Finding]    = []
        self.action_types: List[ActionType] = []

    def add(self, finding: Finding):
        self.findings.append(finding)
        if finding.action_type not in self.action_types:
            self.action_types.append(finding.action_type)

    def primary_action(self) -> ActionType:
        for a in ACTION_PRIORITY:
            if a in self.action_types:
                return a
        return self.action_types[0]

    def primary_finding(self) -> Finding:
        pa = self.primary_action()
        for f in self.findings:
            if f.action_type == pa:
                return f
        return self.findings[0]

    def _current_start_line(self) -> int:
        """
        Resolve current start line via FileRegistry.
        Works for function, class, import, assignment, and statement kinds.
        Returns 0 if unresolvable.
        """
        if self.entity_type == "line":
            try:
                return int(self.entity_name)
            except ValueError:
                return 0

        snap = registry.get(self.file_path)
        if not snap:
            return 0

        # assignment and statement have their own lookup paths
        if self.entity_type == "assignment":
            sym = snap.symbols.assignments.get(self.entity_name)
            return sym.start_line if sym else 0

        if self.entity_type == "statement":
            sym = snap.symbols.find(self.entity_name, "statement")
            return sym.start_line if sym else 0

        # function / class / import — standard find()
        sym = snap.symbols.find(self.entity_name, self.entity_type)
        return sym.start_line if sym else 0

    def to_action(self) -> Optional[Action]:
        """
        Merge all findings into one symbol-based Action.

        For COMPATIBLE action pairs (e.g. ADD_DOCSTRING + REFACTOR_CODE),
        both actions are stored as an ordered sub_actions list in impact_analysis.
        The executor loops through them sequentially on a single approve click.

        For DESTRUCTIVE merges (DELETE dominates), lower-priority actions
        are dropped as before.
        """
        pa = self.primary_action()
        pf = self.primary_finding()

        has_delete = any(
            a in (ActionType.DELETE_FUNCTION, ActionType.DELETE_IMPORT, ActionType.DELETE_FILE)
            for a in self.action_types
        )
        if has_delete and pa in _DELETE_DOMINATES:
            return None

        risk   = ACTION_RISK_LEVELS.get(pa, RiskLevel.MEDIUM)
        target = f"{self.file_path}@{self.entity_type}:{self.entity_name}"

        desc = pf.description
        if len(self.findings) > 1:
            desc += f" [merged {len(self.findings)} findings]"

        impact = dict(pf.impact_analysis) if pf.impact_analysis else {}
        impact["merged_actions"] = [a.value for a in self.action_types]
        impact["entity"]         = f"{self.entity_type}:{self.entity_name}"

        if pa == ActionType.FIX_SECURITY and pf.code_snippet:
            impact["credential_line"] = pf.code_snippet

        # ── Build ordered sub_actions for compatible pairs ────────────
        # These are action_type strings ordered by ACTION_PRIORITY so the
        # executor runs them in the right sequence (e.g. docstring first,
        # then refactor on the now-documented function).
        compatible_secondary = COMPATIBLE_PAIRS.get(pa, [])
        sub_actions = [pa.value]   # primary first
        for secondary in compatible_secondary:
            if secondary in self.action_types:
                sub_actions.append(secondary.value)

        if len(sub_actions) > 1:
            impact["sub_actions"] = sub_actions   # ordered execution list
            desc = (
                f"Execute {len(sub_actions)} actions on "
                f"{self.entity_type} '{self.entity_name}': "
                + " → ".join(sub_actions)
            )

        return Action(
            finding_id=f"merged_{pf.id}",
            action_type=pa,
            description=desc,
            target=target,
            confidence=max(f.confidence for f in self.findings),
            risk_level=risk,
            reasoning=pf.reasoning + (
                f" (merged {len(self.findings)} findings)" if len(self.findings) > 1 else ""
            ),
            impact_analysis=impact,
            suggested_changes=pf.suggested_fix,
        )


# ─────────────────────────────────────────────────────────────────────────────
# ActionCoordinator
# ─────────────────────────────────────────────────────────────────────────────

class ActionCoordinator:

    def __init__(self):
        self._groups: Dict[str, EntityGroup] = {}

    def clear(self):
        self._groups.clear()

    def _group_key(self, entity_type: str, entity_name: str, file_path: str) -> str:
        return f"{Path(file_path).resolve()}::{entity_type}::{entity_name}"

    def add_finding(self, finding: Finding):
        entity_type, entity_name = _extract_entity(finding)
        key = self._group_key(entity_type, entity_name, finding.file_path)
        if key not in self._groups:
            self._groups[key] = EntityGroup(entity_type, entity_name, finding.file_path)
        self._groups[key].add(finding)

    def _ordered_actions(self) -> List[Action]:
        """
        Produce actions ordered for safe serial execution within each file.
        Deletes are sorted bottom-to-top by CURRENT line (from registry),
        so deleting line 80 never invalidates the address of line 30.
        """
        # Separate by file
        by_file: Dict[str, List[EntityGroup]] = defaultdict(list)
        for group in self._groups.values():
            by_file[group.file_path].append(group)

        DELETE_TYPES = frozenset([
            ActionType.DELETE_FUNCTION,
            ActionType.DELETE_IMPORT,
            ActionType.DELETE_FILE,
        ])

        actions: List[Action] = []
        for file_path, groups in by_file.items():
            delete_groups = [g for g in groups if g.primary_action() in DELETE_TYPES]
            other_groups  = [g for g in groups if g not in delete_groups]

            # Sort deletes bottom-to-top using live registry line numbers
            delete_groups.sort(key=lambda g: g._current_start_line(), reverse=True)

            other_groups.sort(
                key=lambda g: (
                    ACTION_PRIORITY.index(g.primary_action())
                    if g.primary_action() in ACTION_PRIORITY else 99
                )
            )

            for group in delete_groups + other_groups:
                action = group.to_action()
                if action:
                    actions.append(action)

        return actions

    def process_findings(self, findings: List[Finding]) -> List[Action]:
        """Public entry point. Returns a list of coordinated, ordered Actions."""
        self.clear()
        logger.info("Coordinating %d findings", len(findings))

        for finding in findings:
            self.add_finding(finding)

        actions = self._ordered_actions()
        merged_count = len(findings) - len(actions)
        logger.info(
            "%d coordinated actions (%d merged/skipped duplicates)",
            len(actions), merged_count,
        )
        return actions


# ── Singleton ─────────────────────────────────────────────────────────────────

action_coordinator = ActionCoordinator()