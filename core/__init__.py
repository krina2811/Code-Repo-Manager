"""
Core Module

Core business logic for the Code Repository Manager:
- Data models (Pydantic schemas)
- HITL routing and confidence scoring
- Action execution engine
"""

from core.models import (
    Action,
    ActionType,
    AgentState,
    AgentType,
    AnalysisRequest,
    AnalysisResult,
    Finding,
    ReviewRequest,
    ReviewStatus,
    RiskLevel,
)
from core.hitl import (
    HITLRouter,
    ReviewQueue,
    hitl_router,
    review_queue,
)
from core.executor import (
    BackupManager,
    LocalLLM,
    RegistryActionExecutor
)
from core.path_validator import (
    validate_repo_path,
    get_helpful_path_message,
    diagnose_path_issue
)
from core.action_coordinator import (
EntityGroup, 
ActionCoordinator
)
                                     

__all__ = [
    # Models
    "Action",
    "ActionType",
    "AgentState",
    "AgentType",
    "AnalysisRequest",
    "AnalysisResult",
    "Finding",
    "ReviewRequest",
    "ReviewStatus",
    "RiskLevel",
    # HITL
    "HITLRouter",
    "ReviewQueue",
    "hitl_router",
    "review_queue",
    # Executor
    "BackupManager",
    "LocalLLM",
    "RegistryActionExecutor"
    # Path Validator
    "validate_repo_path",
    "get_helpful_path_message",
    "diagnose_path_issue",
    ##action coordinator
    "EntityGroup", 
    "ActionCoordinator"



]