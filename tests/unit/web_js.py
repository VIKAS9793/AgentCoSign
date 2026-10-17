"""Read and run functions from the web pages' scripts in tests."""

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

WEB = Path(__file__).parents[2] / "agentcosign" / "web"


def script_function(name: str, script: str = "approve.js") -> str:
    """The source of one function, up to (not including) its closing brace."""
    text = (WEB / script).read_text(encoding="utf-8")
    start = text.index(f"function {name}(")
    return text[start : text.index("\n  }\n", start)]


def script_constant(name: str, script: str = "approve.js") -> str:
    """The declaration of one page-level constant, exactly as the page has it."""
    text = (WEB / script).read_text(encoding="utf-8")
    start = text.index(f"var {name} = ")
    return text[start : text.index(";", start) + 1]


def run_js(
    functions: list[str],
    expression: str,
    script: str = "approve.js",
    constants: tuple[str, ...] = (),
) -> Any:
    """Evaluate page functions in Node and return the expression's value."""
    node = shutil.which("node")
    assert node, "Node is needed to test the pages' decisions"
    declared = [script_constant(name, script) for name in constants]
    bodies = [script_function(n, script) + "\n}" for n in functions]
    source = "\n".join([*declared, *bodies])
    program = f"{source}\nprocess.stdout.write(JSON.stringify({expression}));"
    result = subprocess.run(  # noqa: S603 -- fixed interpreter, test-owned code
        [node, "-"], input=program, capture_output=True, text=True, check=True
    )
    return json.loads(result.stdout)
