from pathlib import Path
import sys

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from threadlang.parser import parse_program
from threadlang.runtime import run_program


def test_golden_hello() -> None:
    source = (REPO_ROOT / "examples" / "hello.thread").read_text(encoding="utf-8")
    program = parse_program(source)
    result = run_program(program, inputs={"name": "world"})
    assert result.output == "Hello, world!"
