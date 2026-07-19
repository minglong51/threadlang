"""ThreadLang package."""

from .llm import (
    AgentLLMClient,
    AgentTurn,
    AnthropicClient,
    DryRunClient,
    LLMClient,
    LLMError,
    OpenAICompatClient,
    RouteLLMClient,
    ToolCall,
)
from .control import WorkerPool, process_one
from .dashboard import render_run_detail, render_run_list
from .metrics import (
    AggregateMetrics,
    RunMetrics,
    aggregate,
    compute_metrics,
    trace_span_ms,
)
from .parser import ParseError, parse_program
from .runtime import RuntimeError, RuntimeResult, run_program
from .server import make_server, serve
from .store import DurableRun, RunRecord, RunStore, run_durable
from .tools import FunctionTool, Tool, ToolRegistry, ToolSpec, default_registry

__all__ = [
    "parse_program",
    "ParseError",
    "run_program",
    "RuntimeError",
    "RuntimeResult",
    "RunStore",
    "RunRecord",
    "DurableRun",
    "run_durable",
    "WorkerPool",
    "process_one",
    "make_server",
    "serve",
    "render_run_list",
    "render_run_detail",
    "RunMetrics",
    "AggregateMetrics",
    "compute_metrics",
    "aggregate",
    "trace_span_ms",
    "LLMClient",
    "AgentLLMClient",
    "RouteLLMClient",
    "AnthropicClient",
    "OpenAICompatClient",
    "DryRunClient",
    "LLMError",
    "AgentTurn",
    "ToolCall",
    "Tool",
    "ToolSpec",
    "FunctionTool",
    "ToolRegistry",
    "default_registry",
]
