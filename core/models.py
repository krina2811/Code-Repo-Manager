"""
Data models for the code repository manager.
All identifiers use Pydantic v2-compatible style.
"""

from enum import Enum
from datetime import datetime
from typing import Optional, Dict, Any, List
from pydantic import BaseModel, Field, EmailStr, field_validator, model_validator


class RiskLevel(str, Enum):
    LOW      = "low"
    MEDIUM   = "medium"
    HIGH     = "high"
    CRITICAL = "critical"


class ActionType(str, Enum):
    DELETE_FILE        = "delete_file"
    DELETE_FUNCTION    = "delete_function"
    DELETE_IMPORT      = "delete_import"
    MOVE_FILE          = "move_file"
    RESTRUCTURE        = "restructure"
    ADD_DOCSTRING      = "add_docstring"
    FIX_SECURITY       = "fix_security"
    UPDATE_DEPENDENCY  = "update_dependency"
    REFACTOR_CODE      = "refactor_code"


class ReviewStatus(str, Enum):
    PENDING            = "pending"
    APPROVED           = "approved"
    PARTIALLY_APPROVED = "partially_approved"   # some sub-actions succeeded
    REJECTED           = "rejected"
    MODIFIED           = "modified"
    AUTO_APPROVED      = "auto_approved"
    INVALIDATED        = "invalidated"   # stale — target no longer exists or condition gone
    BLOCKED            = "blocked"       # waiting for higher-priority action on same file


class AgentType(str, Enum):
    DEAD_CODE     = "dead_code"
    SECURITY      = "security"
    DOCUMENTATION = "documentation"
    STRUCTURE     = "structure"


class Finding(BaseModel):
    """A code-quality finding produced by an agent."""

    id:             str = Field(default_factory=lambda: f"finding_{datetime.now().timestamp()}")
    agent_type:     AgentType
    action_type:    ActionType
    title:          str
    description:    str
    file_path:      str
    line_number:    Optional[int] = None

    # Severity for display; execution uses action_type + confidence
    severity:       str = "medium"   # low | medium | high
    confidence:     float = Field(ge=0.0, le=1.0)

    reasoning:      str
    impact_analysis: Dict[str, Any]
    suggested_fix:  Optional[str] = None

    # Raw source snippet captured at analysis time (used as fingerprint
    # during execution to re-locate the target even after line shifts)
    code_snippet:   Optional[str] = None

    created_at:     datetime = Field(default_factory=datetime.now)


class Action(BaseModel):
    """
    A planned modification derived from one or more Findings.

    target format (symbol-based):
        "file/path.py@entity_type:entity_name"
        e.g.  "/repo/utils.py@function:parse_config"

    For security issues (line-specific, never merged):
        "file/path.py@line:42"
    """

    id:              str = Field(default_factory=lambda: f"action_{datetime.now().timestamp()}")
    finding_id:      str
    action_type:     ActionType
    description:     str
    target:          str          # symbol-based — NO raw line numbers
    confidence:      float = Field(ge=0.0, le=1.0)
    risk_level:      RiskLevel
    reasoning:       str
    impact_analysis: Dict[str, Any]
    suggested_changes: Optional[str] = None
    created_at:      datetime = Field(default_factory=datetime.now)


class ReviewRequest(BaseModel):
    """An action awaiting human approval."""

    id:              str = Field(default_factory=lambda: f"review_{datetime.now().timestamp()}")
    action:          Action
    status:          ReviewStatus = ReviewStatus.PENDING
    reviewer:        Optional[str] = None
    review_timestamp: Optional[datetime] = None
    review_notes:    Optional[str] = None
    modified_action: Optional[Action] = None
    owner:           Optional[str] = None   # manager username who owns this review
    created_at:      datetime = Field(default_factory=datetime.now)


class AnalysisRequest(BaseModel):
    repo_path:            str
    agent_filter:         Optional[List[AgentType]] = None
    auto_approve:         bool = False
    confidence_threshold: float = 0.7
    dry_run:              bool = False


class AnalysisResult(BaseModel):
    id:              str = Field(default_factory=lambda: f"result_{datetime.now().timestamp()}")
    repo_path:       str
    findings:        List[Finding]
    actions_taken:   List[Action]
    pending_reviews: List[ReviewRequest]
    stats:           Dict[str, Any]
    completed_at:    datetime = Field(default_factory=datetime.now)


class AgentState(BaseModel):
    """Mutable state passed between LangGraph nodes."""

    repo_path:       str
    findings:        List[Finding] = Field(default_factory=list)
    actions:         List[Action]  = Field(default_factory=list)
    pending_reviews: List[ReviewRequest] = Field(default_factory=list)
    metadata:        Dict[str, Any] = Field(default_factory=dict)
    current_agent:   Optional[AgentType] = None

    class Config:
        arbitrary_types_allowed = True

class RegisterRequest(BaseModel):
    email: EmailStr
    password: str
    confirm_password: str

    @field_validator("password")
    @classmethod
    def validate_password(cls, v: str) -> str:
        if len(v) < 8:
            raise ValueError("Password must be at least 8 characters")
        if not any(c.isdigit() for c in v):
            raise ValueError("Password must contain at least one number")
        if not any(c.isalpha() for c in v):
            raise ValueError("Password must contain at least one letter")
        return v

    @model_validator(mode="after")
    def passwords_match(self) -> "RegisterRequest":
        if self.password != self.confirm_password:
            raise ValueError("Passwords do not match")
        return self


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class ProjectRequest(BaseModel):
    name: str
    repo_path: str


class StartAnalysisRequest(BaseModel):
    repo_path: str
    agent_filter: Optional[List[AgentType]] = None
    auto_approve: bool = False
    confidence_threshold: float = 0.7


class AnalysisStatusResponse(BaseModel):
    session_id: str
    status: str
    findings_count: int
    pending_reviews: int
    completed_at: Optional[str] = None


class ReviewDecisionRequest(BaseModel):
    decision: str       # "approve" or "reject"
    notes: Optional[str] = None
    reviewer: Optional[str] = None  # falls back to JWT username
