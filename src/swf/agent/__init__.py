"""The coding agent (M4): tools, per-phase model, task channel and `WebAgent`."""

from swf.agent.channel import (
    AgentExit,
    ApprovalOutcome,
    PgTaskChannel,
    RetriesExhausted,
    SetupForbidden,
    TaskChannel,
    TaskStopped,
    TaskSubmitted,
    WaitTimedOut,
    wait_for,
)
from swf.agent.model import (
    DEFAULT_PHASE_MODELS,
    PORTKEY_BASE_URL,
    AgentModel,
    MissingApiKey,
    PhaseModel,
    PhaseModelConfig,
)
from swf.agent.tools import (
    TOOL_SCHEMAS,
    Plan,
    ToolEnv,
    ToolError,
    resolve_repo_path,
    run_tool,
    tool_schemas,
)
from swf.agent.web_agent import AcceptAllEvidence, EvidenceGate, WebAgent, WebAgentConfig

__all__ = [
    "DEFAULT_PHASE_MODELS",
    "PORTKEY_BASE_URL",
    "TOOL_SCHEMAS",
    "AcceptAllEvidence",
    "AgentExit",
    "AgentModel",
    "ApprovalOutcome",
    "EvidenceGate",
    "MissingApiKey",
    "PgTaskChannel",
    "PhaseModel",
    "PhaseModelConfig",
    "Plan",
    "RetriesExhausted",
    "SetupForbidden",
    "TaskChannel",
    "TaskStopped",
    "TaskSubmitted",
    "ToolEnv",
    "ToolError",
    "WaitTimedOut",
    "WebAgent",
    "WebAgentConfig",
    "resolve_repo_path",
    "run_tool",
    "tool_schemas",
    "wait_for",
]
