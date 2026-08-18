"""ThreadLang package."""

from .control import WorkerPool, process_one
from .dashboard import render_run_detail, render_run_list
from .ir import (
    IRCompileError,
    WorkflowIR,
    canonical_ir_bytes,
    compile_program,
    load_ir_bytes,
    program_from_ir,
    run_ir,
    workflow_fingerprint,
)
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
from .metrics import (
    AggregateMetrics,
    RunMetrics,
    aggregate,
    compute_metrics,
    trace_span_ms,
)
from .parser import ParseError, parse_program
from .probe import ProbeReport, ProbeRunData, StepProbe, probe_report
from .runtime import RuntimeError, RuntimeResult, run_program
from .server import make_server, serve
from .store import DurableRun, RunRecord, RunStore, RunStoreCapacityError, run_durable
from .tools import FunctionTool, Tool, ToolRegistry, ToolSpec, default_registry

__version__ = "0.13.2"

__all__ = [
    "parse_program",
    "ParseError",
    "compile_program",
    "load_ir_bytes",
    "program_from_ir",
    "run_ir",
    "canonical_ir_bytes",
    "workflow_fingerprint",
    "WorkflowIR",
    "IRCompileError",
    "run_program",
    "RuntimeError",
    "RuntimeResult",
    "RunStore",
    "RunStoreCapacityError",
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
    "ProbeReport",
    "ProbeRunData",
    "StepProbe",
    "probe_report",
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
    "__version__",
]
