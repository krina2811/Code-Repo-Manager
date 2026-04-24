"""
Human-in-the-Loop (HITL) Routing System

Decision flow for every Action
────────────────────────────────
  Finding list
      ↓ ActionCoordinator.process_findings()
  Symbol-based Action list
      ↓ HITLRouter.route()
      ├─ AUTO  → execute immediately, record result
      └─ HUMAN → ReviewQueue.add()
                     ↓ (user approves / rejects via API / UI)
                 execute on approval, skip on rejection
                 record decision for future learning

All routing decisions are logged to checkpoint_storage so the system
learns from historical human feedback (higher rejection rate → force review).
"""

import hashlib
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from core.models import (
    Action, ActionType, Finding, ReviewRequest,
    ReviewStatus, RiskLevel,
)
from core.action_coordinator import ACTION_RISK_LEVELS
from core.file_registry import FileRegistry

_registry = FileRegistry.get_instance()
from core.action_coordinator import ActionCoordinator, ACTION_RISK_LEVELS
from core.file_registry import FileRegistry
from storage.checkpoint import checkpoint_storage
from core.logger import get_logger

registry = FileRegistry.get_instance()
logger = get_logger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# HITLRouter
# ─────────────────────────────────────────────────────────────────────────────

class HITLRouter:
    """
    Decides whether each action should be auto-executed or queued for
    human review, based on:
      • risk level   (CRITICAL → always review)
      • confidence   (below threshold → review)
      • action type  (security fixes → higher bar)
      • history      (past rejections → escalate)
    """

    def __init__(self, confidence_threshold: float = 0.7):
        self.confidence_threshold = confidence_threshold

    # Merged execution order — defines priority within compatible pairs.
    # Lower index = executed first. This mirrors COMPATIBLE_PAIRS in
    # action_coordinator but lives here so routing decisions can use it.
    MERGED_EXECUTION_ORDER: Dict[str, int] = {
        ActionType.FIX_SECURITY.value:      0,   # credentials first
        ActionType.DELETE_IMPORT.value:     1,   # auto-approved cleanup
        ActionType.DELETE_FUNCTION.value:   2,
        ActionType.REFACTOR_CODE.value:     3,   # refactor before docstring
        ActionType.ADD_DOCSTRING.value:     4,
        ActionType.RESTRUCTURE.value:       5,
    }

    # ------------------------------------------------------------------
    def should_require_review(self, action: Action) -> Tuple[bool, str]:
        """
        Returns (needs_review: bool, reason: str).
        Called once per action after coordination.

        For MERGED actions (sub_actions list in impact_analysis), routing
        is based on the WORST risk level across all sub-actions, not just
        the primary. This prevents a LOW-risk primary from masking a
        MEDIUM-risk secondary that will also execute on approve.
        """
        # ── Resolve effective risk for merged actions ─────────────────
        # A merged ADD_DOCSTRING (LOW) + REFACTOR_CODE (MEDIUM) should
        # route as MEDIUM, not LOW.
        effective_risk = action.risk_level
        sub_action_types = action.impact_analysis.get("sub_actions", [])

        if sub_action_types and len(sub_action_types) > 1:
            risk_order = {
                RiskLevel.LOW:      0,
                RiskLevel.MEDIUM:   1,
                RiskLevel.HIGH:     2,
                RiskLevel.CRITICAL: 3,
            }
            for sat in sub_action_types:
                try:
                    sub_at = ActionType(sat)
                    sub_risk = ACTION_RISK_LEVELS.get(sub_at, RiskLevel.MEDIUM)
                    if risk_order.get(sub_risk, 0) > risk_order.get(effective_risk, 0):
                        effective_risk = sub_risk
                except ValueError:
                    pass

            if effective_risk != action.risk_level:
                logger.debug(
                    "Merged action risk escalated: %s → %s (sub-actions: %s)",
                    action.risk_level.value, effective_risk.value, sub_action_types,
                )

        # ── Hard gates ────────────────────────────────────────────────
        if effective_risk == RiskLevel.CRITICAL:
            return True, f"Critical risk: {action.action_type.value}"

        if action.confidence < self.confidence_threshold:
            return True, f"Low confidence: {action.confidence:.2f} < {self.confidence_threshold}"

        # ── High-risk needs higher confidence bar ─────────────────────
        if effective_risk == RiskLevel.HIGH and action.confidence < 0.85:
            return True, f"HIGH risk requires ≥0.85 confidence — got {action.confidence:.2f}"

        # ── Security fixes always require human review ────────────────
        if action.action_type == ActionType.FIX_SECURITY or \
                ActionType.FIX_SECURITY.value in sub_action_types:
            return True, "Security fixes always require manager approval"

        # ── Code refactoring always requires human review ─────────────
        if action.action_type == ActionType.REFACTOR_CODE or \
                ActionType.REFACTOR_CODE.value in sub_action_types:
            return True, "Code refactoring always requires manager approval"

        # ── Restructure always requires human review ───────────────────
        if action.action_type == ActionType.RESTRUCTURE or \
                ActionType.RESTRUCTURE.value in sub_action_types:
            return True, "File restructuring requires human planning"

        # ── Historical rejection rate ─────────────────────────────────
        past = self._past_decisions(action)
        if past:
            rejection_rate = sum(1 for d in past if not d.get("was_approved", True)) / len(past)
            if rejection_rate > 0.50:
                return True, f"Historical rejection rate {rejection_rate:.0%} > 50% — escalating"

        reason = f"Auto-approved: {effective_risk.value} risk, confidence={action.confidence:.2f}"
        if sub_action_types and len(sub_action_types) > 1:
            reason += f" | {len(sub_action_types)} sub-actions: {' → '.join(sub_action_types)}"
        return (False, reason)

    # ------------------------------------------------------------------
    def _context_hash(self, action: Action) -> str:
        ctx = f"{action.action_type.value}_{action.risk_level.value}"
        return hashlib.md5(ctx.encode()).hexdigest()

    def _past_decisions(self, action: Action) -> List[Dict]:
        try:
            return checkpoint_storage.get_similar_past_decisions(
                action_type=action.action_type.value,
                context_hash=self._context_hash(action),
                limit=10,
            )
        except Exception:
            logger.warning("Failed to fetch past decisions for action %s", action.action_type.value, exc_info=True)
            return []

    def record_decision(
        self,
        action: Action,
        was_approved: bool,
        reviewer: str,
        notes: Optional[str] = None,
    ):
        """Persist a human decision so future routing can learn from it."""
        try:
            checkpoint_storage.save_learning_data(
                action_type=action.action_type.value,
                context_hash=self._context_hash(action),
                was_approved=was_approved,
                confidence=action.confidence,
                review_notes=notes,
            )
        except Exception:
            logger.warning("Failed to record decision for action %s", action.action_type.value, exc_info=True)

    # ------------------------------------------------------------------
    def process_findings(self, findings: List[Finding]) -> List[Action]:
        """
        Convenience wrapper: coordinate raw findings → symbol-based actions.
        Delegates to ActionCoordinator.
        """
        coordinator = ActionCoordinator()
        return coordinator.process_findings(findings)

    # ------------------------------------------------------------------
    def route(
        self,
        actions: List[Action],
        review_queue: "ReviewQueue",
        auto_approve_override: bool = False,
    ) -> Tuple[List[Action], List[str]]:
        """
        Route a list of coordinated actions.

        Returns:
            auto_actions  — list of Actions to execute immediately
            review_ids    — list of ReviewRequest IDs queued for human review
        """
        auto_actions: List[Action] = []
        review_ids: List[str] = []

        for action in actions:
            if auto_approve_override:
                reason = "auto_approve override enabled"
                needs_review = False
            else:
                needs_review, reason = self.should_require_review(action)

            if needs_review:
                rid = review_queue.add(action, reason)
                review_ids.append(rid)
                logger.debug("REVIEW [%s] %s", action.action_type.value, reason)
            else:
                auto_actions.append(action)
                logger.debug("AUTO   [%s] %s", action.action_type.value, reason)

        return auto_actions, review_ids


# ─────────────────────────────────────────────────────────────────────────────
# ReviewQueue
# ─────────────────────────────────────────────────────────────────────────────

_RISK_ORDER = {
    RiskLevel.CRITICAL: 0,
    RiskLevel.HIGH:     1,
    RiskLevel.MEDIUM:   2,
    RiskLevel.LOW:      3,
}

# Safe execution order per file — lower index MUST execute before higher index.
# RESTRUCTURE is last because it only makes sense after dead code is removed;
# the file may no longer need restructuring once unused functions are deleted.
FILE_ACTION_ORDER: Dict[str, int] = {
    ActionType.FIX_SECURITY.value:      0,   # credentials first — always safe to fix before any structural change
    ActionType.DELETE_IMPORT.value:     1,   # auto-approved, low-risk cleanup
    ActionType.DELETE_FUNCTION.value:   2,
    ActionType.REFACTOR_CODE.value:     3,
    ActionType.ADD_DOCSTRING.value:     4,
    ActionType.RESTRUCTURE.value:       5,   # must come after all deletes
    ActionType.MOVE_FILE.value:         6,
}

# Actions that are "destructive" — their completion may invalidate others
DESTRUCTIVE_ACTIONS = frozenset([
    ActionType.DELETE_FUNCTION.value,
    ActionType.DELETE_IMPORT.value,
    ActionType.DELETE_FILE.value,
    ActionType.REFACTOR_CODE.value,
])

# LOC threshold for RESTRUCTURE — if file drops below this after deletions,
# the restructure action is invalidated automatically
RESTRUCTURE_LOC_THRESHOLD = 450


class ReviewQueue:
    """
    Priority review queue with per-file ordering and stale invalidation.

    Key behaviours:
    ─────────────────
    1. ORDERING — within the same file, actions must be reviewed in
       FILE_ACTION_ORDER sequence. RESTRUCTURE is always last because
       deleting unused functions may make the file small enough that
       restructuring is no longer needed.

    2. BLOCKING — a RESTRUCTURE or REFACTOR action for file X is shown
       as BLOCKED if any DELETE action for file X is still pending.
       The user must process deletes first.

    3. INVALIDATION — after any destructive action executes on file X,
       invalidate_stale(file_path) is called. It checks:
         • RESTRUCTURE: is file still > RESTRUCTURE_LOC_THRESHOLD?
           If not → mark INVALIDATED
         • ADD_DOCSTRING / REFACTOR_CODE: does target function still exist?
           If not → mark INVALIDATED
         • DELETE_FUNCTION: was function already deleted?
           If not in SymbolTable → mark INVALIDATED

    4. DISPLAY — get_pending_grouped() returns actions grouped by file
       with ordering metadata so the UI can show them in correct sequence.
    """

    def __init__(self):
        self.pending:   Dict[str, ReviewRequest] = {}
        self.completed: List[ReviewRequest] = []

    # ------------------------------------------------------------------
    def add(self, action: Action, reason: str = "", owner: Optional[str] = None) -> str:
        # Deduplicate: if a pending review for the same action_type + target
        # already exists, return its ID instead of creating a duplicate.
        # This prevents re-analysis from flooding the queue with copies of
        # reviews that a human has not yet acted on.
        dedup_key = (action.action_type.value, action.target)
        for req in self.pending.values():
            if (req.action.action_type.value, req.action.target) == dedup_key:
                logger.debug(
                    "Duplicate review skipped: %s %s (existing id=%s)",
                    action.action_type.value, action.target, req.id,
                )
                return req.id

        req = ReviewRequest(action=action, status=ReviewStatus.PENDING, owner=owner)
        req.review_notes = reason
        self.pending[req.id] = req
        return req.id

    def get(self, request_id: str) -> Optional[ReviewRequest]:
        return self.pending.get(request_id)

    # ── Ordering and blocking ─────────────────────────────────────────

    def _file_path_of(self, req: ReviewRequest) -> str:
        """Extract file path from action target."""
        target = req.action.target
        if "@" in target:
            return target.split("@")[0]
        if ":" in target:
            return target.split(":")[0]
        return target

    def _action_order(self, req: ReviewRequest) -> int:
        return FILE_ACTION_ORDER.get(req.action.action_type.value, 99)

    def is_blocked(self, request_id: str) -> bool:
        """
        Return True if this action is blocked by a higher-priority pending
        action on the same file.

        Example: RESTRUCTURE is blocked if any DELETE_FUNCTION is still
        pending for the same file. The user must process deletes first.
        """
        req = self.pending.get(request_id)
        if not req:
            return False

        my_order    = self._action_order(req)
        my_file     = self._file_path_of(req)

        for other_id, other_req in self.pending.items():
            if other_id == request_id:
                continue
            if self._file_path_of(other_req) != my_file:
                continue
            if self._action_order(other_req) < my_order:
                return True   # a lower-order action exists → I am blocked

        return False

    def get_pending(self, username: Optional[str] = None) -> List[ReviewRequest]:
        """
        Return pending reviews sorted by file path → action order → risk.

        When username is given, only reviews owned by that user are returned.
        Blocking logic (is_blocked) still considers the global queue so that
        physical file-ordering constraints are respected across all users.
        """
        reqs = (
            [r for r in self.pending.values() if r.owner == username]
            if username
            else list(self.pending.values())
        )
        return sorted(
            reqs,
            key=lambda r: (
                self._file_path_of(r),
                self._action_order(r),
                _RISK_ORDER.get(r.action.risk_level, 2),
            ),
        )

    def get_pending_grouped(self, username: Optional[str] = None) -> List[Dict]:
        """
        Return actions grouped by file with blocking metadata.

        When username is given, only reviews owned by that user are grouped.
        Blocking checks still use the full pending queue so that physical
        file-ordering constraints are respected (a lower-priority action for
        the same file owned by the same user is still shown as blocked even
        if the blocking action happens to share the same owner).

        Returns list of:
          {
            file_path: str,
            file_name: str,
            actions:   List[{req, order, is_blocked, block_reason}]
          }
        """
        from pathlib import Path as _Path
        from collections import defaultdict

        by_file = defaultdict(list)
        visible = (
            {rid: req for rid, req in self.pending.items() if req.owner == username}
            if username
            else self.pending
        )
        for req in visible.values():
            fp = self._file_path_of(req)
            by_file[fp].append(req)

        groups = []
        for fp, reqs in by_file.items():
            # Sort within file by FILE_ACTION_ORDER
            reqs.sort(key=lambda r: self._action_order(r))

            actions = []
            for req in reqs:
                blocked   = self.is_blocked(req.id)
                block_req = None
                if blocked:
                    # Find which action is blocking this one
                    my_order = self._action_order(req)
                    for other in reqs:
                        if self._action_order(other) < my_order:
                            block_req = other
                            break

                actions.append({
                    "req":          req,
                    "order":        self._action_order(req),
                    "is_blocked":   blocked,
                    "block_reason": (
                        f"Complete '{block_req.action.action_type.value}' first"
                        if block_req else ""
                    ),
                })

            groups.append({
                "file_path": fp,
                "file_name": _Path(fp).name,
                "actions":   actions,
                "has_blocked": any(a["is_blocked"] for a in actions),
            })

        # Sort groups: files with unblocked actions first
        groups.sort(key=lambda g: (g["has_blocked"], g["file_path"]))
        return groups

    # ── Stale invalidation ────────────────────────────────────────────

    def invalidate_stale(self, file_path: str) -> List[str]:
        """
        After a destructive action executes on file_path, re-evaluate
        all pending actions for that file and invalidate any that are
        no longer applicable.

        Returns list of invalidated request IDs.
        """
        from pathlib import Path as _Path

        abs_path   = str(_Path(file_path).resolve())
        invalidated = []

        snap = _registry.get(abs_path)
        if not snap:
            return invalidated

        current_loc = snap.total_lines()

        to_invalidate = []
        for req_id, req in self.pending.items():
            req_file = str(_Path(self._file_path_of(req)).resolve())
            if req_file != abs_path:
                continue

            action_type = req.action.action_type.value
            target      = req.action.target
            entity_name = target.split(":")[-1] if ":" in target else ""

            reason = None

            # RESTRUCTURE: invalidate if file is now below threshold
            if action_type == ActionType.RESTRUCTURE.value:
                if current_loc <= RESTRUCTURE_LOC_THRESHOLD:
                    reason = (
                        f"File now has {current_loc} LOC "
                        f"(≤ {RESTRUCTURE_LOC_THRESHOLD}) after deletions "
                        f"— restructuring no longer needed"
                    )

            # DELETE_FUNCTION / REFACTOR / DOCSTRING: invalidate if function gone
            elif action_type in (
                ActionType.DELETE_FUNCTION.value,
                ActionType.REFACTOR_CODE.value,
                ActionType.ADD_DOCSTRING.value,
            ):
                if entity_name and entity_name not in snap.symbols.functions:
                    reason = (
                        f"Function '{entity_name}' no longer exists in "
                        f"{_Path(abs_path).name} — already deleted or renamed"
                    )

            # DELETE_IMPORT: invalidate if import already gone
            elif action_type == ActionType.DELETE_IMPORT.value:
                if entity_name and entity_name not in snap.symbols.imports:
                    reason = (
                        f"Import '{entity_name}' no longer exists in "
                        f"{_Path(abs_path).name} — already removed"
                    )

            # FIX_SECURITY (@variable:VAR_NAME): invalidate if variable already
            # uses os.getenv / os.environ — the fix was already applied
            elif action_type == ActionType.FIX_SECURITY.value:
                # Only applies to symbol-based targets (@variable:NAME)
                if "@variable:" in target and entity_name:
                    asgn = snap.symbols.assignments.get(entity_name)
                    if asgn is not None:
                        asgn_line = snap.get_line(asgn.start_line) or ""
                        if "os.getenv" in asgn_line or "os.environ" in asgn_line:
                            reason = (
                                f"Variable '{entity_name}' already uses os.getenv "
                                f"in {_Path(abs_path).name} — security fix already applied"
                            )
                    else:
                        # Variable gone entirely — credential was removed or renamed
                        reason = (
                            f"Variable '{entity_name}' no longer exists in "
                            f"{_Path(abs_path).name} — credential removed or renamed"
                        )

            if reason:
                to_invalidate.append((req_id, reason))

        for req_id, reason in to_invalidate:
            req = self.pending.pop(req_id)
            req.status       = ReviewStatus.INVALIDATED
            req.review_notes = (req.review_notes or "") + f" | INVALIDATED: {reason}"
            self.completed.append(req)
            invalidated.append(req_id)
            logger.info("Invalidated [%s]: %s", req.action.action_type.value, reason)

        if invalidated:
            logger.info(
                "%d stale action(s) invalidated for %s",
                len(invalidated), _Path(file_path).name,
            )

        return invalidated

    # ------------------------------------------------------------------
    def approve(
        self,
        request_id: str,
        reviewer: str,
        notes: Optional[str] = None,
    ) -> Optional[ReviewRequest]:
        req = self.pending.pop(request_id, None)
        if not req:
            return None
        req.status = ReviewStatus.APPROVED
        req.reviewer = reviewer
        req.review_notes = notes
        req.review_timestamp = datetime.now()
        self.completed.append(req)
        return req

    def reject(
        self,
        request_id: str,
        reviewer: str,
        reason: str,
    ) -> Optional[ReviewRequest]:
        req = self.pending.pop(request_id, None)
        if not req:
            return None
        req.status = ReviewStatus.REJECTED
        req.reviewer = reviewer
        req.review_notes = reason
        req.review_timestamp = datetime.now()
        self.completed.append(req)
        return req

    # ------------------------------------------------------------------
    def attach_execution_result(
        self,
        request_id: str,
        execution_result: dict,
        job_id: Optional[str] = None,
    ) -> bool:
        """
        Attach a background job's execution result to a completed review.
        Called by the job polling endpoint once the async job finishes.

        For multi-step merged actions, checks per-step success to determine
        whether the review should be marked APPROVED or PARTIALLY_APPROVED.
        PARTIALLY_APPROVED means the human approved it but not all sub-actions
        succeeded — the learning system treats this differently from a full
        rejection (rejection rate is not incremented for partial failures).
        """
        for req in self.completed:
            if req.id == request_id:
                steps = execution_result.get("steps", [])
                overall_success = execution_result.get("success", False)

                if steps:
                    # Multi-step execution — check individual step results
                    n_ok   = sum(1 for s in steps if s.get("success"))
                    n_fail = len(steps) - n_ok

                    if n_ok == len(steps):
                        # All steps succeeded — stay APPROVED
                        pass
                    elif n_ok > 0:
                        # Some steps succeeded — mark PARTIALLY_APPROVED
                        req.status = ReviewStatus.PARTIALLY_APPROVED
                    # All failed — keep APPROVED (human approved it; execution
                    # failure is separate from the review decision)

                    suffix = (
                        f" | execution: {n_ok}/{len(steps)} steps succeeded"
                        f"{f' | job:{job_id}' if job_id else ''}"
                    )
                else:
                    # Single action
                    suffix = (
                        f" | execution: {'success' if overall_success else 'failed'}"
                        f"{f' | job:{job_id}' if job_id else ''}"
                    )

                req.review_notes = (req.review_notes or "") + suffix
                return True
        return False

    def stats(self) -> Dict[str, Any]:
        total = len(self.completed)
        approved = sum(1 for r in self.completed if r.status == ReviewStatus.APPROVED)
        return {
            "pending":       len(self.pending),
            "completed":     total,
            "approved":      approved,
            "rejected":      total - approved,
            "approval_rate": round(approved / total, 2) if total else 0,
        }


# ─────────────────────────────────────────────────────────────────────────────
# Singletons
# ─────────────────────────────────────────────────────────────────────────────

hitl_router  = HITLRouter()
review_queue = ReviewQueue()