"""Fail-closed resource policy for the single-node ThreadLang runtime.

The values are intentionally conservative defaults, not distributed-runtime
service-level guarantees. Applications that need larger workloads should split
programs or put an authenticated admission layer in front of the server.
"""

MAX_SOURCE_BYTES = 256 * 1024
MAX_STRING_CHARS = 64 * 1024
MAX_AGENT_ITERS = 32
MAX_REGEX_PATTERN_CHARS = 512
MAX_REGEX_INPUT_CHARS = 64 * 1024
REGEX_TIMEOUT_SECONDS = 1.0
MAX_REQUEST_BYTES = 1024 * 1024
MAX_INPUTS = 128
MAX_INPUT_KEY_CHARS = 128
MAX_INPUT_VALUE_CHARS = 64 * 1024
DEFAULT_MAX_PENDING_RUNS = 1_000
DEFAULT_MAX_RETAINED_RUNS = 10_000
DEFAULT_LIST_LIMIT = 100
MAX_LIST_LIMIT = 1_000
