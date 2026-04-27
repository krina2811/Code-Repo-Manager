"""
FastAPI Backend for Code Repository Manager

Provides REST API for:
- JWT authentication (register / login)
- Starting repository analysis
- Managing review queue
- Project watching and notifications
- Background job polling
"""

import sys
import traceback
import uuid
import concurrent.futures
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from fastapi import Depends, FastAPI, HTTPException, BackgroundTasks, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from core.models import (
    AnalysisRequest, AnalysisResult, Finding, Action,
    ReviewRequest, ReviewStatus, AgentType, ActionType, RegisterRequest, LoginRequest, 
    ProjectRequest, StartAnalysisRequest, AnalysisStatusResponse, ReviewDecisionRequest
)
from core.hitl import hitl_router, review_queue
from core.executor import execute_action
from core.watcher import project_watcher
from core.path_validator import validate_repo_path
from core.logger import get_logger, setup_logging
from core.auth import create_access_token, get_current_user, hash_password, verify_password
from agents.workflow import run_analysis
from storage.checkpoint import checkpoint_storage
from config.settings import settings

setup_logging(settings.log_level, settings.log_file)
logger = get_logger(__name__)

# ── Thread pool for long-running LLM actions ─────────────────────────────────
_thread_pool = concurrent.futures.ThreadPoolExecutor(max_workers=4)

LLM_ACTIONS = {ActionType.REFACTOR_CODE, ActionType.ADD_DOCSTRING, ActionType.RESTRUCTURE}
# ── FastAPI app ───────────────────────────────────────────────────────────────

app = FastAPI(
    title="Code Repository Manager API",
    description="AI-powered code analysis with JWT authentication and HITL review",
    version="2.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.allowed_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "DELETE", "PUT", "PATCH"],
    allow_headers=["Content-Type", "Authorization"],
)


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    logger.error("Unhandled exception on %s %s: %s", request.method, request.url.path, exc, exc_info=True)
    return JSONResponse(status_code=500, content={"detail": "Internal server error"})


# ── Lifecycle ─────────────────────────────────────────────────────────────────

@app.on_event("startup")
async def startup_event():
    logger.info("Code Repository Manager API v2 starting up")
    logger.info("Listening on http://%s:%s", settings.api_host, settings.api_port)
    logger.info("PostgreSQL: %s:%s/%s", settings.postgres_host, settings.postgres_port, settings.postgres_db)

    # Start MCP server subprocess — agents use it via stdio protocol
    from core.mcp_client import mcp_client
    try:
        mcp_client.start()
    except Exception as e:
        logger.warning("MCP server failed to start: %s — agents will use direct fallback", e)

    # Restore pending reviews from DB into the in-memory queue
    pending = checkpoint_storage.get_pending_reviews_db()
    for r in pending:
        try:
            action = Action(**r["action_data"])
            owner = r.get("username") or ""
            req = ReviewRequest(id=r["id"], action=action, status=ReviewStatus.PENDING, owner=owner)
            req.review_notes = r.get("review_notes", "")
            if r.get("created_at"):
                req.created_at = datetime.fromisoformat(r["created_at"])
            review_queue.pending[req.id] = req
        except Exception as e:
            logger.warning("Could not restore review %s: %s", r["id"], e)
    if pending:
        logger.info("Restored %d pending review(s) from PostgreSQL", len(pending))

    # Restart file watchers for all registered projects
    projects = checkpoint_storage.get_all_projects()
    for project in projects:
        name = project["name"]
        started = project_watcher.start_watching(
            project["project_id"],
            project["repo_path"],
            lambda pid, rp, n=name, mgr=project["manager"]: _on_project_change(pid, rp, n, mgr),
        )
        if started:
            logger.info("Resumed watcher for project '%s'", name)
        else:
            logger.warning("Could not resume watcher for '%s' (%s)", name, project["repo_path"])


@app.on_event("shutdown")
async def shutdown_event():
    project_watcher.stop_all()
    from core.mcp_client import mcp_client
    mcp_client.stop()
    logger.info("Code Repository Manager API shut down")


# ── Watcher callback ──────────────────────────────────────────────────────────

def _on_project_change(project_id: str, repo_path: str, project_name: str = "", manager: str = ""):
    """Fired by ProjectWatcher thread after debounce. Runs analysis and creates notification."""
    session_id = str(uuid.uuid4())
    checkpoint_storage.update_project_status(project_id, "analyzing", datetime.now().isoformat())
    checkpoint_storage.create_session(
        session_id=session_id,
        repo_path=repo_path,
        project_id=project_id,
        username=manager,
    )

    try:
        result     = run_analysis(repo_path=repo_path, session_id=session_id)
        findings   = result.get("findings", [])
        review_ids = result.get("review_ids", [])

        checkpoint_storage.update_session(
            session_id,
            status="completed",
            findings=findings,
            pending_reviews=review_ids,
            completed_at=datetime.now().isoformat(),
        )
        checkpoint_storage.update_project_status(project_id, "watching")

        # Persist each review to PostgreSQL so they survive server restarts
        for rid in review_ids:
            req = review_queue.get(rid)
            if req:
                req.owner = manager   # tag with owning manager for per-user filtering
                action_data = req.action.model_dump() if hasattr(req.action, "model_dump") else req.action.dict()
                checkpoint_storage.save_review(
                    review_id=rid,
                    action_data=action_data,
                    review_notes=req.review_notes or "",
                    created_at=req.created_at.isoformat() if hasattr(req.created_at, "isoformat") else str(req.created_at),
                    project_id=project_id,
                    session_id=session_id,
                    username=manager,
                )

        if review_ids:
            checkpoint_storage.create_notification(
                notif_id=str(uuid.uuid4()),
                project_id=project_id,
                project_name=project_name,
                session_id=session_id,
                message=f"{len(review_ids)} action(s) need your review in '{project_name}'",
                pending_count=len(review_ids),
                findings_count=len(findings),
                username=manager,
            )
            logger.info("Notification queued for manager '%s': %d reviews in '%s'",
                        manager, len(review_ids), project_name)

    except Exception as e:
        logger.exception("Analysis failed for project '%s': %s", project_id, e)
        checkpoint_storage.update_session(session_id, status="error", error=str(e))
        checkpoint_storage.update_project_status(project_id, "watching")


# ── Thread-pool worker ────────────────────────────────────────────────────────

def _run_execute_in_thread(action, dry_run: bool, job_id: str):
    """Runs in thread pool. Updates background_jobs table when done."""
    import traceback as _tb
    from copy import deepcopy
    from core.models import ActionType as AT

    checkpoint_storage.update_job(job_id, status="running")

    sub_action_types = action.impact_analysis.get("sub_actions", [])

    if not sub_action_types or len(sub_action_types) <= 1:
        try:
            result = execute_action(action, dry_run=dry_run)
            checkpoint_storage.update_job(
                job_id,
                status="done",
                execution_result=result,
                steps=[{"action": action.action_type.value, "result": result}],
                completed_at=datetime.now().isoformat(),
            )
            _log_job_checkpoint(job_id, action, result, dry_run)
        except Exception as e:
            checkpoint_storage.update_job(
                job_id,
                status="error",
                execution_result={"success": False, "error": str(e)},
                completed_at=datetime.now().isoformat(),
            )
            logger.error("Job %s failed: %s", job_id, e, exc_info=True)
        return

    steps = []
    all_success = True
    final_error = None
    logger.info("Job %s: executing %d sub-actions sequentially", job_id, len(sub_action_types))

    for step_idx, action_type_str in enumerate(sub_action_types):
        step_label = f"Step {step_idx + 1}/{len(sub_action_types)}: {action_type_str}"
        checkpoint_storage.update_job(
            job_id,
            current_step=step_idx + 1,
            current_step_label=step_label,
            total_steps=len(sub_action_types),
        )

        try:
            sub_action_type = AT(action_type_str)
        except ValueError:
            steps.append({"action": action_type_str,
                           "result": {"success": False, "error": f"Unknown action type: {action_type_str}"},
                           "step": step_idx + 1, "success": False})
            all_success = False
            continue

        sub_impact = dict(action.impact_analysis)
        sub_impact.pop("sub_actions", None)
        sub_action = action.model_copy(update={
            "action_type": sub_action_type,
            "finding_id": f"{action.finding_id}_step{step_idx}",
            "impact_analysis": sub_impact,
        })

        try:
            result = execute_action(sub_action, dry_run=dry_run)

            if action_type_str in ("refactor_code", "delete_function", "delete_import", "delete_file"):
                file_path_for_check = sub_action.target.split("@")[0]
                if result.get("success") and not dry_run:
                    invalidated = review_queue.invalidate_stale(file_path_for_check)
                    if invalidated:
                        result["invalidated_reviews"] = invalidated

                if action_type_str == "refactor_code" and result.get("success"):
                    from core.file_registry import registry as _reg
                    entity_for_check = sub_action.target.split(":")[-1]
                    snap_check = _reg.get(file_path_for_check)
                    if snap_check:
                        sym_check = snap_check.symbols.functions.get(entity_for_check)
                        has_doc = sym_check.has_docstring if sym_check else False
                        result["refactored_has_docstring"] = has_doc

            step_success = result.get("success", False) or result.get("skipped", False)
            steps.append({"action": action_type_str, "result": result,
                           "step": step_idx + 1, "success": step_success,
                           "skipped": result.get("skipped", False)})
            if not step_success:
                all_success = False
        except Exception as e:
            steps.append({"action": action_type_str,
                           "result": {"success": False, "error": str(e)},
                           "step": step_idx + 1, "success": False, "skipped": False})
            all_success = False
            final_error = str(e)
            logger.error("Job %s — %s raised exception: %s", job_id, step_label, e, exc_info=True)

    final_result = {"success": all_success, "steps": steps,
                    "total_steps": len(sub_action_types), "error": final_error}
    checkpoint_storage.update_job(
        job_id,
        status="done" if all_success else "partial",
        execution_result=final_result,
        steps=steps,
        completed_at=datetime.now().isoformat(),
    )
    _log_job_checkpoint(job_id, action, final_result, dry_run)
    logger.info("Job %s completed — %d/%d steps succeeded",
                job_id, sum(1 for s in steps if s.get("success")), len(steps))


def _log_job_checkpoint(job_id: str, action, result: dict, dry_run: bool):
    try:
        checkpoint_storage.log_action(
            checkpoint_id=f"async_job_{job_id}_{datetime.now().timestamp()}",
            action_type=action.action_type.value,
            action_data={"target": action.target, "description": action.description,
                         "execution_result": result, "job_id": job_id, "async": True},
            confidence=action.confidence,
            risk_level=action.risk_level.value,
            was_approved=True,
            reviewer="async_executor",
            review_notes=f"Executed via background job {job_id} (dry_run={dry_run})",
        )
    except Exception as log_err:
        logger.warning("Checkpoint log failed for job %s: %s", job_id, log_err)


# ─────────────────────────────────────────────────────────────────────────────
# PUBLIC ROUTES
# ─────────────────────────────────────────────────────────────────────────────

@app.get("/")
async def root():
    return {"message": "Code Repository Manager API v2", "version": "2.0.0",
            "auth": "POST /api/auth/register  |  POST /api/auth/login"}


@app.post("/api/auth/register", status_code=201)
async def register(req: RegisterRequest):
    """Register a new user. Email is used as the login identifier."""
    if checkpoint_storage.get_user_by_username(req.email):
        raise HTTPException(status_code=400, detail="Email already registered")

    user = checkpoint_storage.create_user(
        username=req.email,
        hashed_password=hash_password(req.password),
        email=req.email,
    )
    if not user:
        raise HTTPException(status_code=400, detail="Email already registered")

    token = create_access_token(req.email)
    logger.info("New user registered: %s", req.email)
    return {"access_token": token, "token_type": "bearer",
            "username": req.email, "role": "manager"}


@app.post("/api/auth/login")
async def login(req: LoginRequest):
    """Login with email + password. Returns JWT token."""
    user = checkpoint_storage.get_user_by_username(req.email)
    if not user or not verify_password(req.password, user["hashed_password"]):
        raise HTTPException(status_code=401, detail="Invalid email or password")
    if not user["is_active"]:
        raise HTTPException(status_code=403, detail="Account disabled")

    token = create_access_token(req.email, user["role"])
    logger.info("User logged in: %s", req.email)
    return {"access_token": token, "token_type": "bearer",
            "username": req.email, "role": user["role"]}


# ─────────────────────────────────────────────────────────────────────────────
# PROTECTED ROUTES  — all require valid JWT
# ─────────────────────────────────────────────────────────────────────────────

# ── Projects ──────────────────────────────────────────────────────────────────

@app.get("/api/projects")
async def list_projects(current_user: dict = Depends(get_current_user)):
    """Return projects belonging to the logged-in manager."""
    return checkpoint_storage.get_all_projects(manager=current_user["username"])


@app.post("/api/projects", status_code=201)
async def create_project(
    req: ProjectRequest,
    background_tasks: BackgroundTasks,
    current_user: dict = Depends(get_current_user),
):
    """Register a new project and immediately start analysis."""
    is_valid, result, message = validate_repo_path(req.repo_path)
    if not is_valid:
        raise HTTPException(status_code=400, detail={"error": result, "help": message})

    project_id = str(uuid.uuid4())
    saved = checkpoint_storage.save_project(
        project_id=project_id,
        name=req.name,
        repo_path=result,
        manager=current_user["username"],
    )
    if not saved:
        raise HTTPException(status_code=500, detail="Failed to save project")

    username = current_user["username"]
    project_watcher.start_watching(
        project_id, result,
        lambda pid, rp: _on_project_change(pid, rp, req.name, username),
    )

    # Trigger immediate first analysis — no need to wait for a file change
    background_tasks.add_task(_on_project_change, project_id, result, req.name, username)

    logger.info("Project '%s' registered by %s — initial analysis queued", req.name, username)
    return checkpoint_storage.get_project(project_id)


@app.delete("/api/projects/{project_id}")
async def delete_project(
    project_id: str,
    current_user: dict = Depends(get_current_user),
):
    """Unregister a project. Only the owning manager can delete it."""
    project = checkpoint_storage.get_project(project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    if project["manager"] != current_user["username"]:
        raise HTTPException(status_code=403, detail="Not your project")
    project_watcher.stop_watching(project_id)
    checkpoint_storage.delete_project(project_id)
    return {"message": f"Project '{project['name']}' removed"}


@app.post("/api/projects/{project_id}/analyze")
async def trigger_project_analysis(
    project_id: str,
    background_tasks: BackgroundTasks,
    current_user: dict = Depends(get_current_user),
):
    """Manually trigger analysis for a project."""
    project = checkpoint_storage.get_project(project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    if project["manager"] != current_user["username"]:
        raise HTTPException(status_code=403, detail="Not your project")
    background_tasks.add_task(
        _on_project_change,
        project_id, project["repo_path"], project["name"], current_user["username"],
    )
    return {"message": f"Analysis triggered for '{project['name']}'", "project_id": project_id}


# ── Notifications ─────────────────────────────────────────────────────────────

@app.get("/api/notifications")
async def get_notifications(
    unread_only: bool = False,
    current_user: dict = Depends(get_current_user),
):
    """Get notifications for the logged-in user."""
    notifications = checkpoint_storage.get_notifications(
        username=current_user["username"], unread_only=unread_only
    )
    unread_count = sum(1 for n in notifications if not n["read"])
    return {"notifications": notifications, "unread_count": unread_count}


@app.post("/api/notifications/{notification_id}/read")
async def mark_notification_read(
    notification_id: str,
    current_user: dict = Depends(get_current_user),
):
    found = checkpoint_storage.mark_notification_read(notification_id, current_user["username"])
    if not found:
        raise HTTPException(status_code=404, detail="Notification not found")
    return {"message": "Marked as read"}


@app.post("/api/notifications/read-all")
async def mark_all_notifications_read(current_user: dict = Depends(get_current_user)):
    checkpoint_storage.mark_all_notifications_read(current_user["username"])
    return {"message": "All notifications marked as read"}


# ── Analysis ──────────────────────────────────────────────────────────────────

@app.post("/api/analysis/start", response_model=AnalysisStatusResponse)
async def start_analysis(
    request: StartAnalysisRequest,
    background_tasks: BackgroundTasks,
    current_user: dict = Depends(get_current_user),
):
    """Start a new repository analysis in the background."""
    is_valid, result, message = validate_repo_path(request.repo_path)
    if not is_valid:
        raise HTTPException(status_code=400, detail={"error": result, "help": message,
                                                      "provided_path": request.repo_path})

    session_id = str(uuid.uuid4())
    checkpoint_storage.create_session(
        session_id=session_id,
        repo_path=result,
        original_path=request.repo_path,
        username=current_user["username"],
    )

    background_tasks.add_task(
        run_analysis_task,
        session_id, result,
        request.agent_filter, request.auto_approve, request.confidence_threshold,
    )
    return AnalysisStatusResponse(session_id=session_id, status="running",
                                   findings_count=0, pending_reviews=0)


async def run_analysis_task(
    session_id: str,
    repo_path: str,
    agent_filter: Optional[List[AgentType]],
    auto_approve: bool,
    confidence_threshold: float,
):
    """Background task to run analysis."""
    try:
        logger.info("Starting analysis session=%s repo=%s", session_id, repo_path)
        hitl_router.confidence_threshold = confidence_threshold

        result_state = run_analysis(repo_path=repo_path, session_id=session_id,
                                    agent_filter=agent_filter)

        findings = []
        if hasattr(result_state, "findings"):
            findings = result_state.findings
        elif isinstance(result_state, dict):
            findings = result_state.get("findings", [])
            if not findings:
                for value in result_state.values():
                    if isinstance(value, dict) and "findings" in value:
                        findings = value["findings"]
                        break

        if not findings:
            logger.warning("No findings extracted (session=%s)", session_id)

        coordinated_actions = hitl_router.process_findings(findings)
        pending_reviews = []

        for action in coordinated_actions:
            needs_review, reason = hitl_router.should_require_review(action)
            if needs_review:
                review_id = review_queue.add(action)
                pending_reviews.append(review_id)
            elif auto_approve:
                execution_result = execute_action(action, dry_run=False)
                checkpoint_storage.log_action(
                    checkpoint_id=f"{session_id}_action_{datetime.now().timestamp()}",
                    action_type=action.action_type.value,
                    action_data={"target": action.target, "description": action.description,
                                 "execution_result": execution_result},
                    confidence=action.confidence,
                    risk_level=action.risk_level.value,
                    was_approved=True,
                    reviewer="system_auto_approve",
                    review_notes=f"Auto-approved: {action.risk_level.value} risk",
                )
            else:
                review_id = review_queue.add(action)
                pending_reviews.append(review_id)

        checkpoint_storage.update_session(
            session_id,
            status="completed",
            findings=findings,
            pending_reviews=pending_reviews,
            completed_at=datetime.now().isoformat(),
        )
        logger.info("Analysis complete: %d findings, %d pending reviews (session=%s)",
                    len(findings), len(pending_reviews), session_id)

    except Exception as e:
        logger.exception("Analysis task failed (session=%s): %s", session_id, e)
        checkpoint_storage.update_session(session_id, status="error", error=str(e))


@app.get("/api/analysis/{session_id}", response_model=AnalysisStatusResponse)
async def get_analysis_status(
    session_id: str,
    current_user: dict = Depends(get_current_user),
):
    session = checkpoint_storage.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    if session.get("username") and session["username"] != current_user["username"]:
        raise HTTPException(status_code=403, detail="Not your session")
    return AnalysisStatusResponse(
        session_id=session_id,
        status=session["status"],
        findings_count=len(session.get("findings", [])),
        pending_reviews=len(session.get("pending_reviews", [])),
        completed_at=session.get("completed_at"),
    )


@app.get("/api/analysis/{session_id}/findings", response_model=List[Finding])
async def get_findings(
    session_id: str,
    current_user: dict = Depends(get_current_user),
):
    session = checkpoint_storage.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    if session.get("username") and session["username"] != current_user["username"]:
        raise HTTPException(status_code=403, detail="Not your session")
    return session.get("findings", [])


# ── Reviews ───────────────────────────────────────────────────────────────────

@app.get("/api/reviews", response_model=List[ReviewRequest])
async def get_pending_reviews(current_user: dict = Depends(get_current_user)):
    return review_queue.get_pending(username=current_user["username"])


@app.get("/api/reviews/grouped")
async def get_pending_reviews_grouped(current_user: dict = Depends(get_current_user)):
    groups = review_queue.get_pending_grouped(username=current_user["username"])
    result = []
    for group in groups:
        serialised = []
        for a in group["actions"]:
            req = a["req"]
            serialised.append({
                "id": req.id, "action": req.action.dict(),
                "status": req.status.value, "review_notes": req.review_notes,
                "created_at": req.created_at.isoformat(),
                "order": a["order"], "is_blocked": a["is_blocked"],
                "block_reason": a["block_reason"],
            })
        result.append({
            "file_path": group["file_path"], "file_name": group["file_name"],
            "has_blocked": group["has_blocked"], "actions": serialised,
            "total": len(serialised),
            "blocked": sum(1 for a in serialised if a["is_blocked"]),
            "ready": sum(1 for a in serialised if not a["is_blocked"]),
        })
    return result


@app.get("/api/reviews/history/reviewers")
async def list_reviewers(current_user: dict = Depends(get_current_user)):
    reviewers = checkpoint_storage.get_all_reviewers()
    return {"reviewers": reviewers, "total": len(reviewers)}


@app.get("/api/reviews/history/{reviewer}")
async def get_reviewer_history(
    reviewer: str,
    limit: int = 50,
    action_type: Optional[str] = None,
    current_user: dict = Depends(get_current_user),
):
    result = checkpoint_storage.get_reviewer_history(reviewer=reviewer, limit=limit,
                                                      action_type=action_type)
    if result["total"] == 0:
        raise HTTPException(status_code=404,
                            detail=f"No review history found for reviewer '{reviewer}'")
    return result


@app.get("/api/reviews/{request_id}", response_model=ReviewRequest)
async def get_review(request_id: str, current_user: dict = Depends(get_current_user)):
    req = review_queue.pending.get(request_id)
    if not req:
        raise HTTPException(status_code=404, detail="Review not found")
    if req.owner and req.owner != current_user["username"]:
        raise HTTPException(status_code=403, detail="Not your review")
    return req


@app.post("/api/reviews/{request_id}/decision")
async def submit_review_decision(
    request_id: str,
    decision: ReviewDecisionRequest,
    dry_run: bool = False,
    execute_immediately: bool = True,
    current_user: dict = Depends(get_current_user),
):
    """Approve or reject a review. Reviewer identity comes from JWT token."""
    reviewer = decision.reviewer or current_user["username"]

    logger.info("Review decision: request=%s decision=%s reviewer=%s dry_run=%s",
                request_id, decision.decision, reviewer, dry_run)

    if request_id not in review_queue.pending:
        raise HTTPException(status_code=404, detail="Review not found")

    request_obj = review_queue.pending[request_id]

    if decision.decision == "approve":
        review_queue.approve(request_id, reviewer, decision.notes)
        hitl_router.record_decision(action=request_obj.action, was_approved=True,
                                     reviewer=reviewer, notes=decision.notes)

        execution_result = None
        job_id = None

        if execute_immediately:
            action = request_obj.action

            if action.action_type in LLM_ACTIONS:
                job_id = str(uuid.uuid4())
                checkpoint_storage.create_job(
                    job_id=job_id,
                    action_type=action.action_type.value,
                    target=action.target,
                    description=action.description,
                    dry_run=dry_run,
                    review_id=request_id,
                    username=current_user["username"],
                )
                _thread_pool.submit(_run_execute_in_thread, action, dry_run, job_id)
                logger.info("LLM action queued as background job %s", job_id)
            else:
                execution_result = execute_action(action, dry_run=dry_run)
                checkpoint_storage.log_action(
                    checkpoint_id=f"approved_{request_id}_{datetime.now().timestamp()}",
                    action_type=action.action_type.value,
                    action_data={"target": action.target, "description": action.description,
                                 "execution_result": execution_result},
                    confidence=action.confidence,
                    risk_level=action.risk_level.value,
                    was_approved=True,
                    reviewer=reviewer,
                    review_notes=decision.notes,
                )
                if execution_result.get("success"):
                    if action.action_type.value in {"delete_function", "delete_import",
                                                     "delete_file", "refactor_code"}:
                        file_path = action.target.split("@")[0]
                        invalidated = review_queue.invalidate_stale(file_path)
                        if invalidated:
                            execution_result["invalidated_reviews"] = invalidated
                            execution_result["invalidated_count"] = len(invalidated)
                            for inv_id in invalidated:
                                checkpoint_storage.invalidate_review(inv_id)

        checkpoint_storage.complete_review(request_id)
        return {
            "message": "Review approved successfully",
            "executed": execute_immediately,
            "execution_result": execution_result,
            "job_id": job_id,
            "async": job_id is not None,
            "poll_url": f"/api/jobs/{job_id}" if job_id else None,
            "dry_run": dry_run,
        }

    elif decision.decision == "reject":
        review_queue.reject(request_id, reviewer, decision.notes or "Rejected")
        hitl_router.record_decision(action=request_obj.action, was_approved=False,
                                     reviewer=reviewer, notes=decision.notes)
        checkpoint_storage.log_action(
            checkpoint_id=f"rejected_{request_id}_{datetime.now().timestamp()}",
            action_type=request_obj.action.action_type.value,
            action_data={"target": request_obj.action.target,
                         "description": request_obj.action.description},
            confidence=request_obj.action.confidence,
            risk_level=request_obj.action.risk_level.value,
            was_approved=False,
            reviewer=reviewer,
            review_notes=decision.notes,
        )
        checkpoint_storage.complete_review(request_id)
        return {"message": "Review rejected", "reason": decision.notes}

    raise HTTPException(status_code=400, detail="decision must be 'approve' or 'reject'")


# ── Background jobs ───────────────────────────────────────────────────────────

@app.get("/api/jobs/{job_id}")
async def get_job_status(job_id: str, current_user: dict = Depends(get_current_user)):
    job = checkpoint_storage.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found")
    if job.get("username") and job["username"] != current_user["username"]:
        raise HTTPException(status_code=403, detail="Not your job")

    if job["status"] in ("done", "error") and not job.get("_attached"):
        review_id = job.get("review_id")
        if review_id and job.get("execution_result"):
            review_queue.attach_execution_result(
                request_id=review_id,
                execution_result=job["execution_result"],
                job_id=job_id,
            )
        checkpoint_storage.update_job(job_id, attached=True)

    return {
        "job_id": job_id,
        "status": job["status"],
        "action_type": job.get("action_type"),
        "description": job.get("description"),
        "dry_run": job.get("dry_run"),
        "created_at": job.get("created_at"),
        "completed_at": job.get("completed_at"),
        "execution_result": job.get("execution_result"),
        "total_steps": job.get("total_steps", 1),
        "current_step": job.get("current_step", 1),
        "current_step_label": job.get("current_step_label", ""),
        "steps": job.get("steps", []),
    }


@app.get("/api/jobs")
async def list_jobs(current_user: dict = Depends(get_current_user)):
    jobs = checkpoint_storage.list_jobs(username=current_user["username"])
    return {
        "jobs": jobs,
        "total": len(jobs),
        "running": sum(1 for j in jobs if j["status"] == "running"),
        "done": sum(1 for j in jobs if j["status"] == "done"),
        "errors": sum(1 for j in jobs if j["status"] == "error"),
    }


# ── Stats & checkpoints ───────────────────────────────────────────────────────

@app.get("/api/stats")
async def get_statistics(current_user: dict = Depends(get_current_user)):
    return {
        "review_queue": review_queue.stats(),
        "actions": checkpoint_storage.get_action_stats(),
        "total_sessions": checkpoint_storage.count_sessions(current_user["username"]),
    }


@app.get("/api/checkpoints/{session_id}")
async def get_session_checkpoints(
    session_id: str,
    current_user: dict = Depends(get_current_user),
):
    session = checkpoint_storage.get_session(session_id)
    if session and session.get("username") and session["username"] != current_user["username"]:
        raise HTTPException(status_code=403, detail="Not your session")
    checkpoints = checkpoint_storage.get_session_checkpoints(session_id)
    return {"session_id": session_id, "checkpoints": checkpoints}


@app.post("/api/actions/execute")
async def execute_action_endpoint(
    request_id: str,
    dry_run: bool = True,
    current_user: dict = Depends(get_current_user),
):
    """Execute an already-approved action."""
    completed = [r for r in review_queue.completed if r.id == request_id]
    if not completed:
        raise HTTPException(status_code=404, detail="Review not found")
    request_obj = completed[0]
    if request_obj.status != ReviewStatus.APPROVED:
        raise HTTPException(status_code=400, detail="Can only execute approved actions")

    result = execute_action(request_obj.action, dry_run=dry_run)
    reviewer = current_user["username"]
    checkpoint_storage.log_action(
        checkpoint_id=f"exec_{request_id}_{datetime.now().timestamp()}",
        action_type=request_obj.action.action_type.value,
        action_data={"target": request_obj.action.target, "execution_result": result},
        confidence=request_obj.action.confidence,
        risk_level=request_obj.action.risk_level.value,
        was_approved=True,
        reviewer=reviewer,
        review_notes=f"Executed via API (dry_run={dry_run})",
    )
    return {
        "execution_result": result,
        "dry_run": dry_run,
        "message": "Action executed" if result.get("success") else "Execution failed",
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("api.main:app", host="0.0.0.0", port=8000, reload=False, log_level="info")
