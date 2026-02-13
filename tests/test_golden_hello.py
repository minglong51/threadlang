from pathlib import Path

from threadlang.parser import parse_program
from threadlang.runtime import run_program


def test_golden_hello() -> None:
    source = Path("examples/hello.thread").read_text(encoding="utf-8")
    program = parse_program(source)
    result = run_program(program, inputs={"name": "world"})

    assert result.output == "Hello, world!"
    assert [event.name for event in result.trace] == ["parse_ok", "context_set", "emit"]
