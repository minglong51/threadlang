"""ThreadLang v0.1 package."""

from .parser import parse_program
from .runtime import run_program

__all__ = ["parse_program", "run_program"]
