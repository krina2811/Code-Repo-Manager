"""
Checkpoint Storage System — PostgreSQL backend.

Stores all agent state, actions, learning data, users, analysis sessions,
background jobs, and notifications.
"""

import json
import sys
from contextlib import contextmanager
from datetime import datetime, date
from pathlib import Path
from typing import Any, Dict, List, Optional

import psycopg2
import psycopg2.pool
import psycopg2.errors


def _json_dumps(obj) -> str:
    """json.dumps that handles datetime/date/Pydantic objects."""
    def default(o):
        if isinstance(o, (datetime, date)):
            return o.isoformat()
        if hasattr(o, "model_dump"):        # Pydantic v2
            return o.model_dump()
        if hasattr(o, "dict"):              # Pydantic v1
            return o.dict()
        raise TypeError(f"Object of type {type(o)} is not JSON serializable")
    return json.dumps(obj, default=default)

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from config.settings import settings
from core.logger import get_logger

logger = get_logger(__name__)


class CheckpointStorage:
    """PostgreSQL-backed storage for agent checkpoints, actions, and state."""

    def __init__(self):
        self._pool = psycopg2.pool.ThreadedConnectionPool(
            minconn=1,
            maxconn=10,
            host=settings.postgres_host,
            port=settings.postgres_port,
            dbname=settings.postgres_db,
            user=settings.postgres_user,
            password=settings.postgres_password,
        )
        self._init_db()

    @contextmanager
    def _conn(self):
        conn = self._pool.getconn()
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            self._pool.putconn(conn)

    def _init_db(self):
        with self._conn() as conn:
            cur = conn.cursor()

            cur.execute("""
                CREATE TABLE IF NOT EXISTS checkpoints (
                    id SERIAL PRIMARY KEY,
                    checkpoint_id TEXT UNIQUE NOT NULL,
                    session_id TEXT NOT NULL,
                    agent_type TEXT,
                    state_data TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
            """)

            cur.execute("""
                CREATE TABLE IF NOT EXISTS tool_calls (
                    id SERIAL PRIMARY KEY,
                    checkpoint_id TEXT NOT NULL,
                    tool_name TEXT NOT NULL,
                    arguments TEXT NOT NULL,
                    result TEXT NOT NULL,
                    duration_ms INTEGER,
                    created_at TEXT NOT NULL
                )
            """)

            cur.execute("""
                CREATE TABLE IF NOT EXISTS agent_actions (
                    id SERIAL PRIMARY KEY,
                    checkpoint_id TEXT NOT NULL,
                    action_type TEXT NOT NULL,
                    action_data TEXT NOT NULL,
                    confidence REAL,
                    risk_level TEXT,
                    was_approved BOOLEAN,
                    reviewer TEXT,
                    review_notes TEXT,
                    created_at TEXT NOT NULL
                )
            """)

            cur.execute("""
                CREATE TABLE IF NOT EXISTS learning_data (
                    id SERIAL PRIMARY KEY,
                    action_type TEXT NOT NULL,
                    context_hash TEXT NOT NULL,
                    was_approved BOOLEAN NOT NULL,
                    confidence REAL,
                    review_notes TEXT,
                    created_at TEXT NOT NULL
                )
            """)

            cur.execute("""
                CREATE TABLE IF NOT EXISTS projects (
                    project_id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    repo_path TEXT NOT NULL,
                    manager TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'watching',
                    last_analysis TEXT,
                    created_at TEXT NOT NULL
                )
            """)

            cur.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    id SERIAL PRIMARY KEY,
                    username TEXT UNIQUE NOT NULL,
                    email TEXT UNIQUE NOT NULL,
                    hashed_password TEXT NOT NULL,
                    role TEXT NOT NULL DEFAULT 'manager',
                    is_active BOOLEAN NOT NULL DEFAULT TRUE,
                    created_at TEXT NOT NULL
                )
            """)

            cur.execute("""
                CREATE TABLE IF NOT EXISTS analysis_sessions (
                    session_id TEXT PRIMARY KEY,
                    status TEXT NOT NULL DEFAULT 'running',
                    repo_path TEXT NOT NULL,
                    original_path TEXT,
                    project_id TEXT,
                    findings TEXT DEFAULT '[]',
                    pending_reviews TEXT DEFAULT '[]',
                    started_at TEXT NOT NULL,
                    completed_at TEXT,
                    error TEXT,
                    username TEXT
                )
            """)

            cur.execute("""
                CREATE TABLE IF NOT EXISTS background_jobs (
                    job_id TEXT PRIMARY KEY,
                    status TEXT NOT NULL DEFAULT 'queued',
                    action_type TEXT,
                    target TEXT,
                    description TEXT,
                    dry_run BOOLEAN DEFAULT FALSE,
                    review_id TEXT,
                    execution_result TEXT,
                    steps TEXT DEFAULT '[]',
                    total_steps INTEGER DEFAULT 1,
                    current_step INTEGER DEFAULT 1,
                    current_step_label TEXT DEFAULT '',
                    created_at TEXT NOT NULL,
                    completed_at TEXT,
                    attached BOOLEAN DEFAULT FALSE
                )
            """)

            cur.execute("""
                CREATE TABLE IF NOT EXISTS notifications (
                    id TEXT PRIMARY KEY,
                    project_id TEXT,
                    project_name TEXT,
                    session_id TEXT,
                    message TEXT NOT NULL,
                    pending_count INTEGER DEFAULT 0,
                    findings_count INTEGER DEFAULT 0,
                    read BOOLEAN DEFAULT FALSE,
                    username TEXT,
                    created_at TEXT NOT NULL
                )
            """)

            cur.execute("""
                CREATE TABLE IF NOT EXISTS reviews (
                    id TEXT PRIMARY KEY,
                    action_data TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    review_notes TEXT,
                    created_at TEXT NOT NULL,
                    project_id TEXT,
                    session_id TEXT
                )
            """)

            # Indexes
            cur.execute("CREATE INDEX IF NOT EXISTS idx_checkpoints_session ON checkpoints(session_id)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_tool_calls_checkpoint ON tool_calls(checkpoint_id)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_learning_action_type ON learning_data(action_type, context_hash)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_sessions_username ON analysis_sessions(username)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_notifications_username ON notifications(username)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_notifications_read ON notifications(username, read)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_jobs_status ON background_jobs(status)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_projects_manager ON projects(manager)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_reviews_status ON reviews(status)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_agent_actions_reviewer ON agent_actions(reviewer)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_agent_actions_type ON agent_actions(action_type)")

            # Schema migrations — add new columns to existing tables safely
            cur.execute("ALTER TABLE background_jobs ADD COLUMN IF NOT EXISTS username TEXT")
            cur.execute("ALTER TABLE reviews ADD COLUMN IF NOT EXISTS username TEXT")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_jobs_username ON background_jobs(username)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_reviews_username ON reviews(username)")

    # ── Checkpoints ───────────────────────────────────────────────────────────

    def save_checkpoint(
        self,
        checkpoint_id: str,
        session_id: str,
        agent_type: Optional[str],
        state_data: Dict[str, Any],
    ) -> bool:
        try:
            with self._conn() as conn:
                conn.cursor().execute("""
                    INSERT INTO checkpoints (checkpoint_id, session_id, agent_type, state_data, created_at)
                    VALUES (%s, %s, %s, %s, %s)
                    ON CONFLICT (checkpoint_id) DO UPDATE
                      SET state_data = EXCLUDED.state_data,
                          agent_type = EXCLUDED.agent_type
                """, (checkpoint_id, session_id, agent_type,
                      _json_dumps(state_data), datetime.now().isoformat()))
            return True
        except Exception as e:
            logger.exception("Error saving checkpoint %s: %s", checkpoint_id, e)
            return False

    def load_checkpoint(self, checkpoint_id: str) -> Optional[Dict[str, Any]]:
        with self._conn() as conn:
            cur = conn.cursor()
            cur.execute("""
                SELECT state_data, agent_type, created_at
                FROM checkpoints WHERE checkpoint_id = %s
            """, (checkpoint_id,))
            row = cur.fetchone()
            if row:
                return {"state_data": json.loads(row[0]), "agent_type": row[1], "created_at": row[2]}
        return None

    def get_session_checkpoints(self, session_id: str) -> List[Dict[str, Any]]:
        with self._conn() as conn:
            cur = conn.cursor()
            cur.execute("""
                SELECT checkpoint_id, agent_type, created_at
                FROM checkpoints WHERE session_id = %s
                ORDER BY created_at DESC
            """, (session_id,))
            return [{"checkpoint_id": r[0], "agent_type": r[1], "created_at": r[2]}
                    for r in cur.fetchall()]

    # ── Tool calls ────────────────────────────────────────────────────────────

    def log_tool_call(
        self,
        checkpoint_id: str,
        tool_name: str,
        arguments: Dict[str, Any],
        result: Any,
        duration_ms: Optional[int] = None,
    ):
        try:
            with self._conn() as conn:
                conn.cursor().execute("""
                    INSERT INTO tool_calls
                    (checkpoint_id, tool_name, arguments, result, duration_ms, created_at)
                    VALUES (%s, %s, %s, %s, %s, %s)
                """, (checkpoint_id, tool_name, _json_dumps(arguments),
                      _json_dumps(result), duration_ms, datetime.now().isoformat()))
        except Exception as e:
            logger.exception("Error logging tool call %s: %s", tool_name, e)

    # ── Agent actions ─────────────────────────────────────────────────────────

    def log_action(
        self,
        checkpoint_id: str,
        action_type: str,
        action_data: Dict[str, Any],
        confidence: float,
        risk_level: str,
        was_approved: Optional[bool] = None,
        reviewer: Optional[str] = None,
        review_notes: Optional[str] = None,
    ):
        try:
            with self._conn() as conn:
                conn.cursor().execute("""
                    INSERT INTO agent_actions
                    (checkpoint_id, action_type, action_data, confidence, risk_level,
                     was_approved, reviewer, review_notes, created_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                """, (checkpoint_id, action_type, _json_dumps(action_data),
                      confidence, risk_level, was_approved, reviewer,
                      review_notes, datetime.now().isoformat()))
        except Exception as e:
            logger.exception("Error logging action %s: %s", action_type, e)

    # ── Learning data ─────────────────────────────────────────────────────────

    def save_learning_data(
        self,
        action_type: str,
        context_hash: str,
        was_approved: bool,
        confidence: float,
        review_notes: Optional[str] = None,
    ):
        try:
            with self._conn() as conn:
                conn.cursor().execute("""
                    INSERT INTO learning_data
                    (action_type, context_hash, was_approved, confidence, review_notes, created_at)
                    VALUES (%s, %s, %s, %s, %s, %s)
                """, (action_type, context_hash, was_approved, confidence,
                      review_notes, datetime.now().isoformat()))
        except Exception as e:
            logger.exception("Error saving learning data for %s: %s", action_type, e)

    def get_similar_past_decisions(
        self,
        action_type: str,
        context_hash: str,
        limit: int = 10,
    ) -> List[Dict[str, Any]]:
        with self._conn() as conn:
            cur = conn.cursor()
            cur.execute("""
                SELECT was_approved, confidence, review_notes, created_at
                FROM learning_data
                WHERE action_type = %s AND context_hash = %s
                ORDER BY created_at DESC LIMIT %s
            """, (action_type, context_hash, limit))
            return [{"was_approved": bool(r[0]), "confidence": r[1],
                     "review_notes": r[2], "created_at": r[3]}
                    for r in cur.fetchall()]

    # ── Projects ──────────────────────────────────────────────────────────────

    def save_project(self, project_id: str, name: str, repo_path: str, manager: str) -> bool:
        try:
            with self._conn() as conn:
                conn.cursor().execute("""
                    INSERT INTO projects (project_id, name, repo_path, manager, status, created_at)
                    VALUES (%s, %s, %s, %s, 'watching', %s)
                    ON CONFLICT (project_id) DO UPDATE
                      SET name = EXCLUDED.name, repo_path = EXCLUDED.repo_path,
                          manager = EXCLUDED.manager
                """, (project_id, name, repo_path, manager, datetime.now().isoformat()))
            return True
        except Exception as e:
            logger.exception("Error saving project %s: %s", project_id, e)
            return False

    def update_project_status(self, project_id: str, status: str, last_analysis: Optional[str] = None):
        with self._conn() as conn:
            conn.cursor().execute("""
                UPDATE projects
                SET status = %s, last_analysis = COALESCE(%s, last_analysis)
                WHERE project_id = %s
            """, (status, last_analysis, project_id))

    def get_project(self, project_id: str) -> Optional[Dict[str, Any]]:
        with self._conn() as conn:
            cur = conn.cursor()
            cur.execute("""
                SELECT project_id, name, repo_path, manager, status, last_analysis, created_at
                FROM projects WHERE project_id = %s
            """, (project_id,))
            row = cur.fetchone()
            return self._project_row(row) if row else None

    def get_all_projects(self, manager: Optional[str] = None) -> List[Dict[str, Any]]:
        with self._conn() as conn:
            cur = conn.cursor()
            cur.execute("""
                SELECT project_id, name, repo_path, manager, status, last_analysis, created_at
                FROM projects
                WHERE (%s IS NULL OR manager = %s)
                ORDER BY created_at DESC
            """, (manager, manager))
            return [self._project_row(r) for r in cur.fetchall()]

    def delete_project(self, project_id: str) -> bool:
        with self._conn() as conn:
            cur = conn.cursor()
            cur.execute("DELETE FROM projects WHERE project_id = %s", (project_id,))
            return cur.rowcount > 0

    @staticmethod
    def _project_row(row) -> Dict[str, Any]:
        return {
            "project_id": row[0], "name": row[1], "repo_path": row[2],
            "manager": row[3], "status": row[4], "last_analysis": row[5], "created_at": row[6],
        }

    # ── Reviewer history ──────────────────────────────────────────────────────
    def get_reviewer_history(
        self,
        reviewer: str,
        limit: int = 50,
        action_type: Optional[str] = None,
    ) -> Dict[str, Any]:
        with self._conn() as conn:
            cur = conn.cursor()
            cur.execute("""
                SELECT action_type, action_data, confidence, risk_level,
                       was_approved, review_notes, created_at
                FROM agent_actions
                WHERE reviewer = %s
                  AND was_approved IS NOT NULL
                  AND (%s IS NULL OR action_type = %s)
                ORDER BY created_at DESC LIMIT %s
            """, (reviewer, action_type, action_type, limit))
            rows = cur.fetchall()

        history = [
            {"action_type": r[0], "action_data": json.loads(r[1]),
             "confidence": r[2], "risk_level": r[3],
             "was_approved": bool(r[4]), "review_notes": r[5], "created_at": r[6]}
            for r in rows
        ]
        total = len(history)
        approved = sum(1 for h in history if h["was_approved"])
        return {
            "reviewer": reviewer, "total": total, "approved": approved,
            "rejected": total - approved,
            "approval_rate": round(approved / total, 2) if total else 0,
            "history": history,
        }

    def get_all_reviewers(self) -> List[str]:
        with self._conn() as conn:
            cur = conn.cursor()
            cur.execute("""
                SELECT DISTINCT reviewer FROM agent_actions
                WHERE reviewer IS NOT NULL AND was_approved IS NOT NULL
                ORDER BY reviewer
            """)
            return [r[0] for r in cur.fetchall()]

    def get_action_stats(self, action_type: Optional[str] = None) -> Dict[str, Any]:
        with self._conn() as conn:
            cur = conn.cursor()
            if action_type:
                cur.execute("""
                    SELECT COUNT(*),
                           SUM(CASE WHEN was_approved THEN 1 ELSE 0 END),
                           SUM(CASE WHEN NOT was_approved THEN 1 ELSE 0 END),
                           AVG(confidence)
                    FROM agent_actions
                    WHERE action_type = %s AND was_approved IS NOT NULL
                """, (action_type,))
            else:
                cur.execute("""
                    SELECT COUNT(*),
                           SUM(CASE WHEN was_approved THEN 1 ELSE 0 END),
                           SUM(CASE WHEN NOT was_approved THEN 1 ELSE 0 END),
                           AVG(confidence)
                    FROM agent_actions WHERE was_approved IS NOT NULL
                """)
            row = cur.fetchone()
        if row and row[0]:
            total = row[0]
            approved = row[1] or 0
            return {
                "total": total, "approved": approved,
                "rejected": row[2] or 0,
                "approval_rate": round(approved / total, 2),
                "avg_confidence": round(row[3] or 0, 3),
            }
        return {"total": 0, "approved": 0, "rejected": 0, "approval_rate": 0, "avg_confidence": 0}

    # ── Users ─────────────────────────────────────────────────────────────────
    def create_user(
        self,
        username: str,
        hashed_password: str,
        email: Optional[str] = None,
        role: str = "manager",
    ) -> Optional[Dict[str, Any]]:
        try:
            with self._conn() as conn:
                cur = conn.cursor()
                cur.execute("""
                    INSERT INTO users (username, email, hashed_password, role, is_active, created_at)
                    VALUES (%s, %s, %s, %s, TRUE, %s)
                    RETURNING id, username, email, role, is_active, created_at
                """, (username, email, hashed_password, role, datetime.now().isoformat()))
                row = cur.fetchone()
                return {"id": row[0], "username": row[1], "email": row[2],
                        "role": row[3], "is_active": row[4], "created_at": row[5]}
        except psycopg2.errors.UniqueViolation:
            return None
        except Exception as e:
            logger.exception("Error creating user %s: %s", username, e)
            return None

    def get_user_by_username(self, username: str) -> Optional[Dict[str, Any]]:
        with self._conn() as conn:
            cur = conn.cursor()
            cur.execute("""
                SELECT id, username, email, hashed_password, role, is_active
                FROM users WHERE username = %s
            """, (username,))
            row = cur.fetchone()
            if row:
                return {"id": row[0], "username": row[1], "email": row[2],
                        "hashed_password": row[3], "role": row[4], "is_active": row[5]}
        return None

    # ── Analysis sessions ─────────────────────────────────────────────────────

    def create_session(
        self,
        session_id: str,
        repo_path: str,
        original_path: str = "",
        project_id: Optional[str] = None,
        username: Optional[str] = None,
    ):
        with self._conn() as conn:
            conn.cursor().execute("""
                INSERT INTO analysis_sessions
                (session_id, status, repo_path, original_path, project_id,
                 findings, pending_reviews, started_at, username)
                VALUES (%s, 'running', %s, %s, %s, '[]', '[]', %s, %s)
            """, (session_id, repo_path, original_path, project_id,
                  datetime.now().isoformat(), username))

    def update_session(self, session_id: str, **fields):
        """Update arbitrary session fields. Serialises findings/pending_reviews to JSON."""
        if not fields:
            return
        if "findings" in fields and not isinstance(fields["findings"], str):
            fields["findings"] = _json_dumps(
                [f.model_dump() if hasattr(f, "model_dump") else
                 f.dict()       if hasattr(f, "dict")       else f
                 for f in fields["findings"]]
            )
        if "pending_reviews" in fields and not isinstance(fields["pending_reviews"], str):
            fields["pending_reviews"] = _json_dumps(fields["pending_reviews"])

        _ALLOWED = {"status", "findings", "pending_reviews", "completed_at", "error", "username"}
        fields = {k: v for k, v in fields.items() if k in _ALLOWED}
        if not fields:
            return
        set_clause = ", ".join(f"{k} = %s" for k in fields)
        values = list(fields.values()) + [session_id]
        with self._conn() as conn:
            conn.cursor().execute(
                f"UPDATE analysis_sessions SET {set_clause} WHERE session_id = %s", values
            )

    def get_session(self, session_id: str) -> Optional[Dict[str, Any]]:
        with self._conn() as conn:
            cur = conn.cursor()
            cur.execute("""
                SELECT session_id, status, repo_path, original_path, project_id,
                       findings, pending_reviews, started_at, completed_at, error, username
                FROM analysis_sessions WHERE session_id = %s
            """, (session_id,))
            row = cur.fetchone()
            if row:
                return {
                    "session_id": row[0], "status": row[1], "repo_path": row[2],
                    "original_path": row[3], "project_id": row[4],
                    "findings": json.loads(row[5] or "[]"),
                    "pending_reviews": json.loads(row[6] or "[]"),
                    "started_at": row[7], "completed_at": row[8],
                    "error": row[9], "username": row[10],
                }
        return None

    def count_sessions(self, username: Optional[str] = None) -> int:
        with self._conn() as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT COUNT(*) FROM analysis_sessions WHERE (%s IS NULL OR username = %s)",
                (username, username),
            )
            return cur.fetchone()[0]

    # ── Background jobs ───────────────────────────────────────────────────────
    def create_job(
        self,
        job_id: str,
        action_type: str,
        target: str,
        description: str,
        dry_run: bool,
        review_id: str,
        username: Optional[str] = None,
    ):
        with self._conn() as conn:
            conn.cursor().execute("""
                INSERT INTO background_jobs
                (job_id, status, action_type, target, description,
                 dry_run, review_id, steps, created_at, username)
                VALUES (%s, 'queued', %s, %s, %s, %s, %s, '[]', %s, %s)
            """, (job_id, action_type, target, description, dry_run,
                  review_id, datetime.now().isoformat(), username))

    def update_job(self, job_id: str, **fields):
        if not fields:
            return
        if "steps" in fields and not isinstance(fields["steps"], str):
            fields["steps"] = _json_dumps(fields["steps"])
        if "execution_result" in fields and not isinstance(fields["execution_result"], str):
            fields["execution_result"] = _json_dumps(fields["execution_result"])
        _ALLOWED = {"status", "steps", "execution_result", "completed_at", "error", "username"}
        fields = {k: v for k, v in fields.items() if k in _ALLOWED}
        if not fields:
            return
        set_clause = ", ".join(f"{k} = %s" for k in fields)
        values = list(fields.values()) + [job_id]
        with self._conn() as conn:
            conn.cursor().execute(
                f"UPDATE background_jobs SET {set_clause} WHERE job_id = %s", values
            )

    def get_job(self, job_id: str) -> Optional[Dict[str, Any]]:
        with self._conn() as conn:
            cur = conn.cursor()
            cur.execute("""
                SELECT job_id, status, action_type, target, description, dry_run,
                       review_id, execution_result, steps, total_steps,
                       current_step, current_step_label, created_at, completed_at,
                       attached, username
                FROM background_jobs WHERE job_id = %s
            """, (job_id,))
            row = cur.fetchone()
            if row:
                return {
                    "job_id": row[0], "status": row[1], "action_type": row[2],
                    "target": row[3], "description": row[4], "dry_run": row[5],
                    "review_id": row[6],
                    "execution_result": json.loads(row[7]) if row[7] else None,
                    "steps": json.loads(row[8] or "[]"),
                    "total_steps": row[9], "current_step": row[10],
                    "current_step_label": row[11] or "",
                    "created_at": row[12], "completed_at": row[13],
                    "_attached": bool(row[14]),
                    "username": row[15],
                }
        return None

    def list_jobs(self, username: Optional[str] = None) -> List[Dict[str, Any]]:
        with self._conn() as conn:
            cur = conn.cursor()
            cur.execute("""
                SELECT job_id, status, action_type, description, created_at, completed_at
                FROM background_jobs
                WHERE (%s IS NULL OR username = %s)
                ORDER BY created_at DESC
            """, (username, username))
            return [{"job_id": r[0], "status": r[1], "action_type": r[2],
                     "description": (r[3] or "")[:80], "created_at": r[4], "completed_at": r[5]}
                    for r in cur.fetchall()]

    # ── Notifications ─────────────────────────────────────────────────────────

    def create_notification(
        self,
        notif_id: str,
        project_id: str,
        project_name: str,
        session_id: str,
        message: str,
        pending_count: int,
        findings_count: int,
        username: Optional[str] = None,
    ):
        with self._conn() as conn:
            conn.cursor().execute("""
                INSERT INTO notifications
                (id, project_id, project_name, session_id, message,
                 pending_count, findings_count, read, username, created_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, FALSE, %s, %s)
            """, (notif_id, project_id, project_name, session_id, message,
                  pending_count, findings_count, username, datetime.now().isoformat()))

    def get_notifications(self, username: Optional[str] = None, unread_only: bool = False) -> List[Dict[str, Any]]:
        with self._conn() as conn:
            cur = conn.cursor()
            cur.execute("""
                SELECT id, project_id, project_name, session_id, message,
                       pending_count, findings_count, read, username, created_at
                FROM notifications
                WHERE (%s IS NULL OR username = %s)
                  AND (NOT %s OR read = FALSE)
                ORDER BY created_at DESC
                LIMIT 100
            """, (username, username, unread_only))
            return [
                {
                    "id": r[0], "project_id": r[1], "project_name": r[2],
                    "session_id": r[3], "message": r[4], "pending_count": r[5],
                    "findings_count": r[6], "read": bool(r[7]),
                    "username": r[8], "created_at": r[9],
                }
                for r in cur.fetchall()
            ]

    def mark_notification_read(self, notif_id: str, username: Optional[str] = None) -> bool:
        with self._conn() as conn:
            cur = conn.cursor()
            cur.execute("""
                UPDATE notifications SET read = TRUE
                WHERE id = %s AND (%s IS NULL OR username = %s)
            """, (notif_id, username, username))
            return cur.rowcount > 0

    def mark_all_notifications_read(self, username: Optional[str] = None):
        with self._conn() as conn:
            conn.cursor().execute("""
                UPDATE notifications SET read = TRUE
                WHERE (%s IS NULL OR username = %s)
            """, (username, username))

    # ── Persistent review queue ───────────────────────────────────────────────

    def save_review(self, review_id: str, action_data: dict,
                    review_notes: str = "", created_at: str = "",
                    project_id: str = "", session_id: str = "",
                    username: Optional[str] = None):
        with self._conn() as conn:
            conn.cursor().execute("""
                INSERT INTO reviews
                (id, action_data, status, review_notes, created_at, project_id, session_id, username)
                VALUES (%s, %s, 'pending', %s, %s, %s, %s, %s)
                ON CONFLICT (id) DO NOTHING
            """, (review_id, _json_dumps(action_data), review_notes,
                  created_at or datetime.now().isoformat(), project_id, session_id, username))

    def get_pending_reviews_db(self) -> List[Dict[str, Any]]:
        with self._conn() as conn:
            cur = conn.cursor()
            cur.execute("""
                SELECT id, action_data, review_notes, created_at, project_id, session_id, username
                FROM reviews WHERE status = 'pending'
                ORDER BY created_at ASC
            """)
            return [
                {
                    "id": r[0], "action_data": json.loads(r[1]),
                    "review_notes": r[2], "created_at": r[3],
                    "project_id": r[4], "session_id": r[5],
                    "username": r[6],
                }
                for r in cur.fetchall()
            ]

    def complete_review(self, review_id: str):
        with self._conn() as conn:
            conn.cursor().execute(
                "UPDATE reviews SET status = 'completed' WHERE id = %s", (review_id,)
            )

    def invalidate_review(self, review_id: str):
        with self._conn() as conn:
            conn.cursor().execute(
                "UPDATE reviews SET status = 'invalidated' WHERE id = %s", (review_id,)
            )


# Global singleton — connects at import time
checkpoint_storage = CheckpointStorage()
