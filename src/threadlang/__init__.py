"""ThreadLang package."""

from .parser import ParseError, parse_program
from .runtime import RuntimeError, run_program

__all__ = ["parse_program", "ParseError", "run_program", "RuntimeError"]
