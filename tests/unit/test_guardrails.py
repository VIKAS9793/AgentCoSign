"""The guardrail script must pass on this repo and catch each known violation.

Each parametrised case is one production failure class: if a rule silently stops
detecting its case, this suite fails, so the gates cannot rot unnoticed.
"""

import importlib.util
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "tools" / "guardrails.py"
spec = importlib.util.spec_from_file_location("guardrails", SCRIPT)
assert spec is not None and spec.loader is not None
guardrails = importlib.util.module_from_spec(spec)
sys.modules["guardrails"] = guardrails  # dataclasses resolve annotations via this
spec.loader.exec_module(guardrails)


def _write(root: Path, rel: str, body: str) -> Path:
    target = root / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(textwrap.dedent(body), encoding="utf-8")
    return root


def _package_violations(root: Path) -> dict[str, list[str]]:
    report = guardrails.Report()
    guardrails.check_package(root, report)
    violations: dict[str, list[str]] = report.violations
    return violations


def _repo_violations(root: Path) -> dict[str, list[str]]:
    report = guardrails.Report()
    guardrails.check_repo(root, report)
    violations: dict[str, list[str]] = report.violations
    return violations


def test_repository_is_clean() -> None:
    assert guardrails.run_check(ROOT) == 0


# --- package rules: (path inside agentcosign/, source, expected rule) --------------
PACKAGE_CASES = [
    ("core/x.py", 'def f() -> None:\n    """D."""\n    print(1)\n', "print-in-package"),
    ("core/x.py", "amount: float = 1.0\n", "float-annotation"),
    ("core/x.py", "x = float('1.0')\n", "float-conversion"),
    (
        "core/x.py",
        "from decimal import Decimal\nx = Decimal(0.1)\n",
        "decimal-from-float",
    ),
    (
        "core/x.py",
        'def f() -> None:\n    """D."""\n    try:\n        f()\n'
        "    except Exception:\n        raise\n",
        "blind-except",
    ),
    (
        "core/x.py",
        'def f() -> None:\n    """D."""\n    try:\n        f()\n'
        "    except ValueError:\n        pass\n",
        "swallowed-exception",
    ),
    (
        "core/x.py",
        'def f() -> str:\n    """D."""\n    try:\n        return "x"\n'
        "    except ValueError:\n        return Decision.ALLOW\n",
        "allow-on-error-path",
    ),
    ("core/x.py", 'MODEL = "openai/gpt-oss-120b"\n', "model-id-outside-config-yaml"),
    (
        "core/x.py",
        "import os\n\n\ndef f() -> str:\n    '''D.'''\n    return os.environ['X']\n",
        "env-read-outside-config",
    ),
    ("core/x.py", "def f() -> None:\n    pass\n", "public-function-without-docstring"),
    (
        "core/x.py",
        "def f(a: list[int] = []) -> None:\n    '''D.'''\n",
        "mutable-default-argument",
    ),
    ("core/x.py", "import requests\n", "sync-http-library"),
    ("core/x.py", "# TODO: later\n", "todo-marker"),
    ("core/x.py", "x = 1  # noqa: S101\n", "suppression-without-reason"),
    ("core/x.py", "x = 1  # type: ignore\n", "suppression-without-reason"),
    (
        "core/x.py",
        "x = 1  # guardrail: allow print-in-package\n",
        "pragma-without-reason",
    ),
    (
        "clients/x.py",
        "from agentcosign.services import policy_engine\n",
        "layer-violation",
    ),
    (
        "services/x.py",
        "from ..api import routes\n",
        "layer-violation",
    ),
    ("services/x.py", "import httpx\n", "network-library-outside-clients"),
    ("clients/x.py", "import fastapi\n", "web-framework-outside-api"),
    (
        "clients/x.py",
        "import httpx\n\n\ndef f() -> None:\n    '''D.'''\n    httpx.get('https://x')\n",
        "http-without-timeout",
    ),
    (
        "clients/x.py",
        "import httpx\n\n\ndef f() -> None:\n    '''D.'''\n"
        "    httpx.get('https://x', timeout=None)\n",
        "http-without-timeout",
    ),
    (
        "clients/x.py",
        "import httpx\n\n\ndef f() -> None:\n    '''D.'''\n"
        "    httpx.get('https://x', timeout=5, verify=False)\n",
        "tls-verify-disabled",
    ),
    (
        "services/x.py",
        "def f(db, name: str) -> None:\n    '''D.'''\n"
        "    db.execute(f'SELECT * FROM t WHERE n = {name}')\n",
        "sql-string-building",
    ),
    (
        "core/x.py",
        "import datetime\n\n\ndef f() -> object:\n    '''D.'''\n"
        "    return datetime.datetime.utcnow()\n",
        "naive-datetime",
    ),
    (
        "core/x.py",
        "import datetime\n\n\ndef f() -> object:\n    '''D.'''\n"
        "    return datetime.datetime.now()\n",
        "naive-datetime",
    ),
    (
        "core/x.py",
        "import hashlib\n\n\ndef f() -> object:\n    '''D.'''\n"
        "    return hashlib.md5(b'x')\n",
        "weak-hash",
    ),
    (
        "core/x.py",
        "import yaml\n\n\ndef f(s: str) -> object:\n    '''D.'''\n"
        "    return yaml.load(s)\n",
        "dangerous-call",
    ),
    (
        "services/x.py",
        "def f(api_key: str, expected: str) -> bool:\n    '''D.'''\n"
        "    return api_key == expected\n",
        "secret-compared-with-eq",
    ),
    (
        "services/x.py",
        "import logging\nlogger = logging.getLogger('x')\n\n\n"
        "def f(api_key: str) -> None:\n    '''D.'''\n"
        "    logger.info('k %s', api_key)\n",
        "secret-in-log",
    ),
    (
        "services/x.py",
        "def f(s) -> str:\n    '''D.'''\n    return s.get_secret_value()\n",
        "secret-unwrapped-outside-boundary",
    ),
    (
        "core/x.py",
        "from pydantic import BaseModel\n\n\nclass M(BaseModel):\n    '''D.'''\n\n"
        "    a: int\n",
        "model-without-extra-forbid",
    ),
    (
        "core/x.py",
        "from pydantic import BaseModel\n\n\nclass M(BaseModel):\n    '''D.'''\n\n"
        "    api_key: str\n",
        "secret-field-not-secretstr",
    ),
    (
        "core/x.py",
        'URL = "https://api-m.paypal.com/v2/x"\n',
        "production-paypal-endpoint",
    ),
    ("core/x.py", 'P = "pay_order"\n', "banned-paypal-surface"),
    (
        "clients/paypal_x.py",
        'P = "/v1/reporting/transactions"\n',
        "paypal-reporting-outside-reconciliation",
    ),
    ("core/x.py", 'HOST = "0.0.0.0"\n', "bind-all-interfaces"),
    (
        "core/x.py",
        "def f(x) -> bool:\n    '''D.'''\n    return x is 'a'\n",
        "is-compared-to-literal",
    ),
    (
        "services/policy_engine.py",
        "import sqlite3\n",
        "impure-policy-engine",
    ),
    (
        "services/policy_engine.py",
        "import time\n\n\ndef f() -> float:\n    '''D.'''\n    return time.time()\n",
        "impure-policy-engine",
    ),
]


@pytest.mark.parametrize(("rel", "body", "rule"), PACKAGE_CASES)
def test_package_rule_detects_violation(
    tmp_path: Path, rel: str, body: str, rule: str
) -> None:
    root = _write(tmp_path, f"agentcosign/{rel}", body)
    violations = _package_violations(root)
    assert rule in violations, f"{rule} not detected; got {sorted(violations)}"


def test_paypal_client_write_needs_request_id(tmp_path: Path) -> None:
    body = (
        "import httpx\n\n\ndef refund(c: httpx.Client) -> None:\n    '''D.'''\n"
        "    c.post('/v2/payments/captures/1/refund', timeout=5)\n"
    )
    root = _write(tmp_path, "agentcosign/clients/paypal_rest.py", body)
    assert "paypal-write-without-request-id" in _package_violations(root)
    fixed = body.replace("timeout=5", "timeout=5, headers={'PayPal-Request-Id': 'r'}")
    root = _write(tmp_path, "agentcosign/clients/paypal_rest.py", fixed)
    assert "paypal-write-without-request-id" not in _package_violations(root)


def test_long_and_complex_functions_are_caught(tmp_path: Path) -> None:
    long_body = 'def big() -> None:\n    """D."""\n' + "    x = 1\n" * 60
    root = _write(tmp_path, "agentcosign/core/long.py", long_body)
    assert "function-too-long" in _package_violations(root)
    branches = "".join(f"    if n == {i}:\n        return {i}\n" for i in range(14))
    complex_body = f'def cx(n: int) -> int:\n    """D."""\n{branches}    return 0\n'
    root = _write(tmp_path, "agentcosign/core/cx.py", complex_body)
    assert "function-too-complex" in _package_violations(root)


def test_clean_code_with_reasoned_pragma_passes(tmp_path: Path) -> None:
    body = (
        "def f(t: float) -> float:  "
        "# guardrail: allow float-annotation -- sampling temperature, not money\n"
        "    '''D.'''\n    return t\n"
    )
    root = _write(tmp_path, "agentcosign/core/ok.py", body)
    assert "float-annotation" not in _package_violations(root)
    assert "pragma-without-reason" not in _package_violations(root)


def test_secret_checks_do_not_flag_safe_patterns(tmp_path: Path) -> None:
    body = (
        "import hmac\nimport logging\nlogger = logging.getLogger('x')\n\n\n"
        "def f(token: str, expected: str, token_length: int) -> bool:\n    '''D.'''\n"
        "    logger.info('length %s', token_length)\n"
        "    if token == '':\n        return False\n"
        "    return hmac.compare_digest(token, expected)\n"
    )
    root = _write(tmp_path, "agentcosign/api/ok.py", body)
    violations = _package_violations(root)
    assert "secret-compared-with-eq" not in violations
    assert "secret-in-log" not in violations


# --- repository rules -------------------------------------------------------------
def test_unsafe_web_output_is_caught(tmp_path: Path) -> None:
    root = _write(
        tmp_path,
        "agentcosign/web/app.js",
        "el.innerHTML = reason;\n// — comment is fine\nconst t = 'a — b';\n",
    )
    violations = _repo_violations(root)
    assert "unsafe-dom-or-eval" in violations
    assert violations["em-dash-in-ui-copy"] == ["agentcosign/web/app.js:3"]


def test_external_web_resource_is_caught(tmp_path: Path) -> None:
    root = _write(
        tmp_path,
        "agentcosign/web/index.html",
        '<script src="https://cdn.example.org/x.js"></script>\n',
    )
    assert "external-web-resource" in _repo_violations(root)


def test_deploy_invariants(tmp_path: Path) -> None:
    _write(
        tmp_path, "render.yaml", "services:\n  - branch: main\n    autoDeploy: true\n"
    )
    _write(tmp_path, "Dockerfile", "FROM python:latest\nCMD []\n")
    violations = _repo_violations(tmp_path)
    for rule in (
        "render-autodeploy-not-disabled",
        "render-not-pinned-to-release-branch",
        "container-runs-as-root",
        "container-without-healthcheck",
        "container-base-image-unpinned",
        "dockerignore-missing-env",
    ):
        assert rule in violations, rule


def test_dependency_without_upper_bound_is_caught(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "pyproject.toml",
        '[project]\nname = "x"\ndependencies = ["fastapi>=0.1", "pydantic>=2,<3"]\n',
    )
    assert _repo_violations(tmp_path)["dependency-without-upper-bound"] == [
        "fastapi>=0.1"
    ]


def test_env_example_with_real_looking_value_is_caught(tmp_path: Path) -> None:
    fake_value = "q" * 30  # built at runtime so no key-like literal is committed
    _write(
        tmp_path,
        ".env.example",
        f"GROQ_API_KEY={fake_value}\nPAYPAL_CLIENT_SECRET=\n"
        "AGENTCOSIGN_AGENT_KEY=changeme\n",
    )
    assert _repo_violations(tmp_path)["env-example-has-value"] == [
        ".env.example:1 GROQ_API_KEY"
    ]


def test_public_doc_referencing_internal_doc_is_caught(tmp_path: Path) -> None:
    _write(tmp_path, "README.md", "See docs/pm/PRD.md for details.\n")
    assert "public-doc-references-internal" in _repo_violations(tmp_path)


def test_unsanitised_evidence_is_caught(tmp_path: Path) -> None:
    _write(tmp_path, "docs/evidence/run.json", '{"payer": "real.person@gmail.com"}\n')
    assert "evidence-not-sanitised" in _repo_violations(tmp_path)
    clean = tmp_path / "docs" / "evidence" / "run.json"
    clean.write_text('{"payer": "sb-1@personal.example.com"}\n', encoding="utf-8")
    assert "evidence-not-sanitised" not in _repo_violations(tmp_path)


@pytest.mark.edge
@pytest.mark.parametrize(
    ("open_cap", "prompt_cap", "caught"),
    [
        ("86_400", "300", False),  # exactly the ceilings
        ("86_401", "300", True),  # open window raised by a second
        ("86_400", "301", True),  # fingerprint prompt raised by a second
        (None, "300", True),  # open window cap removed
        ("86_400", None, True),  # prompt cap removed
    ],
)
def test_approval_window_caps_cannot_be_raised(
    tmp_path: Path, open_cap: str | None, prompt_cap: str | None, caught: bool
) -> None:
    lines = ['paypal_mode: Literal["sandbox"]', 'extra="forbid"']
    if open_cap is not None:
        lines.append(f"open_seconds: int = Field(ge=300, le={open_cap})")
    if prompt_cap is not None:
        lines.append(f"fingerprint_seconds: int = Field(ge=30, le={prompt_cap})")
    _write(tmp_path, "agentcosign/core/config.py", "\n".join(lines) + "\n")
    found = "approval-window-cap-missing-or-raised" in _repo_violations(tmp_path)
    assert found is caught


def test_policy_rule_without_test_is_caught(tmp_path: Path) -> None:
    _write(tmp_path, "agentcosign/services/policy_engine.py", "# R4 and R6\n")
    _write(tmp_path, "tests/unit/test_policy.py", "# covers R4 only\n")
    assert _repo_violations(tmp_path)["policy-rule-without-test"] == ["R6"]


# --- commit messages ----------------------------------------------------------------
@pytest.mark.parametrize(
    ("message", "files", "expected"),
    [
        ("feat(policy): add R5 velocity rule\n", [], []),
        ("Merge branch 'x' into main\n", [], []),
        ("update stuff\n", [], ["not a Conventional Commit"]),
        ("fix: stop.\n", [], ["must not end with a period"]),
        ("feat: x\n\nCo-Authored-By: Claude <noreply@anthropic.com>\n", [], ["AI"]),
        ("ci: add job\n", [".github/workflows/ci.yml"], ["requires-human-signoff"]),
        (
            "ci: add job\n\nSignoff: #requires-human-signoff\n",
            [".github/workflows/ci.yml"],
            [],
        ),
        ("fix: x\n\n# a git comment line\n", ["agentcosign/main.py"], []),
    ],
)
def test_commit_message_rules(
    message: str, files: list[str], expected: list[str]
) -> None:
    problems = guardrails.check_message(message, files)
    assert len(problems) == len(expected)
    for needle, problem in zip(expected, problems, strict=True):
        assert needle in problem


def test_history_check_flags_bad_commits_in_a_range(tmp_path: Path) -> None:
    def git(*args: str) -> None:
        subprocess.run(
            ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
            cwd=tmp_path,
            check=True,
            capture_output=True,
        )

    git("init", "-q")
    git("commit", "--allow-empty", "-q", "-m", "feat: good one")
    git("commit", "--allow-empty", "-q", "-m", "wip things")
    assert guardrails.run_history(["HEAD"], tmp_path) == 1
    assert guardrails.run_history(["HEAD~1"], tmp_path) == 0


# --- merchant screen: the mechanically checkable product UX gates ------------------
GOOD_CSS = """
:root { color-scheme: only light; }
body { font-size: 16px; }
button {
  min-height: 56px;
}
"""


def _merchant(
    tmp_path: Path, js: str = "", html: str = "", css: str = GOOD_CSS
) -> dict[str, list[str]]:
    web = "agentcosign/web/"
    _write(tmp_path, web + "approve.html", html or "<h1>Refund requests</h1>\n")
    _write(tmp_path, web + "approve.js", js)
    _write(tmp_path, web + "approve.css", css)
    report = guardrails.Report()
    guardrails.check_merchant_screen(tmp_path, report)
    violations: dict[str, list[str]] = report.violations
    return violations


@pytest.mark.parametrize(
    ("js", "html", "css", "rule"),
    [
        (
            'say("Escalated under R2 by the policy engine");\n',
            "",
            GOOD_CSS,
            "merchant-jargon",
        ),
        ("", "<p>Approve with your passkey</p>\n", GOOD_CSS, "merchant-jargon"),
        (
            'title = "Refund sent. Money is on its way";\n',
            "",
            GOOD_CSS,
            "success-without-paypal-confirmation",
        ),
        (
            "setInterval(tick, 1000);\n",
            "",
            GOOD_CSS,
            "ticking-timer-on-merchant-screen",
        ),
        (
            "",
            "",
            GOOD_CSS.replace("only light", "light dark"),
            "merchant-screen-not-fixed-light",
        ),
        ("", "", GOOD_CSS + ".note { font-size: 12px; }\n", "text-below-14px"),
        ("", "", GOOD_CSS.replace("56px", "40px"), "button-below-48px"),
    ],
)
def test_merchant_screen_gates_catch_each_breach(
    tmp_path: Path, js: str, html: str, css: str, rule: str
) -> None:
    assert rule in _merchant(tmp_path, js, html, css)


def test_confirmed_success_and_owner_words_pass(tmp_path: Path) -> None:
    js = (
        'if (outcome.state === "executed" && outcome.paypal_status === "COMPLETED") {\n'
        '  return ["sent", "Refund sent. It is on its way to Test Buyer."];\n'
        "}\n"
    )
    assert _merchant(tmp_path, js) == {}


# --- traceability: product changes name their rules, proof and README check -------
SOURCE = {
    "tests/unit/test_x.py": (
        "def test_real_one() -> None:\n    assert True\n\n\n"
        "@pytest.mark.edge\ndef test_race() -> None:\n    assert True\n"
    ),
    # the rules the engine defines, which commit messages may name
    guardrails.ENGINE_MODULE: 'A = "R4"\nB = "R16"\n',
}
GOOD = (
    "fix(policy): x\n\n"
    "Constraints: R4, H1\n"
    "Evidence: tests/unit/test_x.py::test_real_one\n"
    "Edge cases: tests/unit/test_x.py::test_race\n"
    "README: checked\n"
)


def _trace(message: str, files: list[str], fresh: bool = True) -> list[str]:
    problems: list[str] = guardrails.check_traceability(
        message, files, SOURCE.get, lambda _spec: fresh
    )
    return problems


def test_a_traced_product_commit_passes() -> None:
    assert _trace(GOOD, ["agentcosign/services/x.py"]) == []


def test_non_product_commits_need_no_trace() -> None:
    assert _trace("docs(readme): x\n", ["README.md"]) == []


@pytest.mark.parametrize(
    ("message", "files", "needle"),
    [
        (
            GOOD.replace("Constraints: R4, H1\n", ""),
            ["agentcosign/a.py"],
            "Constraints",
        ),
        (GOOD.replace("R4, H1", "R99"), ["agentcosign/a.py"], "no such rule"),
        (
            GOOD.replace("test_real_one", "test_made_up"),
            ["agentcosign/a.py"],
            "does not exist",
        ),
        (
            GOOD.replace("Evidence: tests/unit/test_x.py::test_real_one\n", ""),
            ["agentcosign/a.py"],
            "Evidence",
        ),
        (GOOD.replace("README: checked\n", ""), ["agentcosign/a.py"], "README"),
        (
            GOOD.replace("checked", "updated"),
            ["agentcosign/a.py"],
            "README.md is not in",
        ),
        (GOOD, ["agentcosign/web/approve.js"], "Spec"),
    ],
)
def test_untraced_product_commits_are_refused(
    message: str, files: list[str], needle: str
) -> None:
    assert any(needle in p for p in _trace(message, files))


def test_a_screen_change_needs_a_spec_updated_since_the_last_commit() -> None:
    message = GOOD + "Spec: docs/pm/ux/REFUND-APPROVAL-FLOW-SPEC.md\n"
    files = ["agentcosign/web/approve.js"]
    assert _trace(message, files, fresh=True) == []
    assert any("not updated" in p for p in _trace(message, files, fresh=False))


def test_the_guardrails_themselves_need_human_signoff() -> None:
    problems = guardrails.check_message(
        "fix(tools): loosen a check\n", ["tools/guardrails.py"]
    )
    assert any("#requires-human-signoff" in p for p in problems)


# --- README facts must match the code --------------------------------------------
def test_stale_readme_numbers_are_caught(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "README.md",
        "An 80-row scenario table. Limits (defaults $25 / $1,000).\n",
    )
    _write(
        tmp_path,
        "tests/scenarios/policy_scenarios.py",
        "    Scenario(1)\n    Scenario(2)\n",
    )
    _write(
        tmp_path,
        "config.yaml",
        'refund_auto_limit: "25.00"\ndaily_outflow_cap: "500.00"\n',
    )
    report = guardrails.Report()
    guardrails.check_readme_facts(tmp_path, report)
    found = report.violations.get("readme-stale-fact", [])
    assert any("scenario table" in f for f in found)
    assert any("daily_outflow_cap" in f for f in found)
    assert not any("refund_auto_limit" in f for f in found)


# --- golden rule: fixes and features must name edge-case tests --------------------
EDGE_SOURCE = SOURCE
TRACED = GOOD + "Edge cases: tests/unit/test_x.py::test_race\n"


def _edge(message: str) -> list[str]:
    problems: list[str] = guardrails.check_traceability(
        message, ["agentcosign/services/x.py"], EDGE_SOURCE.get, lambda _s: True
    )
    return problems


def test_a_fix_naming_a_marked_edge_test_passes() -> None:
    assert _edge(TRACED) == []


def test_a_fix_without_edge_cases_is_refused() -> None:
    message = GOOD.replace("Edge cases: tests/unit/test_x.py::test_race\n", "")
    assert any("Edge cases" in p for p in _edge(message))


def test_a_happy_path_test_cannot_pose_as_an_edge_case() -> None:
    message = GOOD.replace("::test_race", "::test_real_one")
    assert any("not marked @pytest.mark.edge" in p for p in _edge(message))


def test_refactors_and_docs_need_no_edge_cases() -> None:
    message = GOOD.replace("fix(policy): x", "refactor(policy): x")
    assert _edge(message) == []


def test_removing_edge_tests_below_the_floor_is_caught(tmp_path: Path) -> None:
    _write(tmp_path, "tests/unit/test_a.py", "@pytest.mark.edge\ndef test_a(): ...\n")
    report = guardrails.Report()
    guardrails.check_edge_floor(tmp_path, report)
    assert "edge-tests-below-floor" in report.violations


# --- hard-coding and fake data in shipped code ------------------------------------
def _hardcoding(root: Path) -> dict[str, list[str]]:
    report = guardrails.Report()
    guardrails.check_no_hardcoding(root, report)
    violations: dict[str, list[str]] = report.violations
    return violations


HARDCODING_CASES = [
    ("agentcosign/core/x.py", "# TODO tidy this\n", "placeholder-in-code"),
    (
        "agentcosign/web/approve.html",
        "<p>[YOUR STORE NAME]</p>\n",
        "placeholder-in-code",
    ),
    (
        "agentcosign/core/x.py",
        'URL = "https://example.com/hook"\n',
        "placeholder-in-code",
    ),
    ("agentcosign/core/x.py", 'NAME = "Jane Buyer"\n', "fake-data-in-code"),
    ("agentcosign/core/x.py", 'CARD = "4111 1111 1111 1111"\n', "fake-data-in-code"),
    ("agentcosign/core/x.py", 'WHO = "merchant@shop.com"\n', "fake-data-in-code"),
    ("agentcosign/core/x.py", 'ORDER = "ORD-1"\n', "fake-data-in-code"),
    (
        "agentcosign/web/approve.js",
        'say("Signed out after 10 minutes.");\n',
        "duration-in-copy",
    ),
    (
        "agentcosign/web/approve.js",
        'el("p", "Refunds over $25.00 need approval");\n',
        "money-in-copy",
    ),
    ("agentcosign/web/approve.js", "var wait = 30000;\n", "bare-number-in-script"),
    (
        "agentcosign/services/customer_copy.py",
        'LINE = "We will reply within 2 hours."\n',
        "duration-in-copy",
    ),
    (
        "agentcosign/services/approval_view.py",
        'TITLE = "On hold: the 24-hour refund limit"\n',
        "duration-in-copy",
    ),
]


@pytest.mark.parametrize(("rel", "body", "rule"), HARDCODING_CASES)
def test_hardcoding_and_fake_data_are_caught(
    tmp_path: Path, rel: str, body: str, rule: str
) -> None:
    root = _write(tmp_path, rel, body)
    violations = _hardcoding(root)
    assert rule in violations, f"{rule} not detected; got {sorted(violations)}"


def test_values_from_the_server_and_named_constants_pass(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "agentcosign/web/approve.js",
        "var RECONNECT_MAX_MS = 30000;\n"
        'say("Signed out after " + minutes + " minutes.");\n'
        'el("p", "Refunds over " + money(limit) + " need approval");\n',
    )
    _write(
        tmp_path,
        "agentcosign/services/customer_copy.py",
        'def line(hours: int) -> str:\n    return f"within {hours} hours"\n',
    )
    assert _hardcoding(tmp_path) == {}


def test_a_reviewed_exception_is_allowed(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "agentcosign/core/x.py",
        'NAME = "Jane Buyer"  # guardrail: allow fake-data-in-code -- docs sample\n',
    )
    assert _hardcoding(tmp_path) == {}


def test_numbers_inside_script_strings_are_left_to_the_copy_rules(
    tmp_path: Path,
) -> None:
    _write(
        tmp_path,
        "agentcosign/web/approve.js",
        'var svgNs = "http://www.w3.org/2000/svg";\n',
    )
    assert "bare-number-in-script" not in _hardcoding(tmp_path)


# --- rule ids are read from the engine, never capped by a number in a pattern ------
def test_a_comment_citing_any_rule_number_is_caught(tmp_path: Path) -> None:
    _write(tmp_path, "agentcosign/core/x.py", "X = 1  # refused when R16 applies\n")
    report = guardrails.Report()
    guardrails.check_comment_hygiene(tmp_path, report)
    assert "internal-reference-in-comment" in report.violations


def test_a_comment_in_plain_words_passes(tmp_path: Path) -> None:
    _write(tmp_path, "agentcosign/core/x.py", "X = 1  # refused while refunds stop\n")
    report = guardrails.Report()
    guardrails.check_comment_hygiene(tmp_path, report)
    assert report.violations == {}


def test_every_engine_rule_needs_a_test_however_high_its_number(
    tmp_path: Path,
) -> None:
    _write(tmp_path, "agentcosign/services/policy_engine.py", 'RULE_X = "R16"\n')
    _write(tmp_path, "tests/unit/test_x.py", "def test_x() -> None:\n    assert True\n")
    report = guardrails.Report()
    guardrails.check_policy_coverage(tmp_path, report)
    assert report.violations == {"policy-rule-without-test": ["R16"]}


def test_commits_may_only_name_rules_the_engine_defines() -> None:
    source = {**SOURCE, guardrails.ENGINE_MODULE: 'A = "R4"\nB = "R16"\n'}
    files = ["agentcosign/services/x.py"]

    def trace(message: str) -> list[str]:
        problems: list[str] = guardrails.check_traceability(
            message, files, source.get, lambda _spec: True
        )
        return problems

    assert trace(GOOD.replace("Constraints: R4, H1", "Constraints: R16, H1")) == []
    refused = trace(GOOD.replace("Constraints: R4, H1", "Constraints: R99, H1"))
    assert any("no such rule 'R99'" in problem for problem in refused)


def test_rule_ids_are_refused_when_the_engine_cannot_be_read() -> None:
    no_engine = {k: v for k, v in SOURCE.items() if k != guardrails.ENGINE_MODULE}
    problems: list[str] = guardrails.check_traceability(
        GOOD, ["agentcosign/services/x.py"], no_engine.get, lambda _spec: True
    )
    assert any("cannot be checked" in problem for problem in problems)


@pytest.mark.edge
@pytest.mark.parametrize(
    ("ident", "accepted"),
    [("F-07", True), ("F-105", True), ("F-1", False), ("F-1000", False)],
)
def test_finding_ids_have_two_or_three_digits(ident: str, accepted: bool) -> None:
    assert bool(guardrails.CONSTRAINT_ID.match(ident)) is accepted
