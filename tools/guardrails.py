"""Project guardrails: deep, repo-specific checks that linters and type checkers miss.

Ruff, mypy and bandit cover generic quality. This script enforces the rules that
are specific to a payment-control gateway: fail closed,
Decimal money, secrets never logged or compared with ``==``, a pure policy engine,
layered imports, bounded network calls, a sandbox-only PayPal surface, safe web
output, and the repo and deploy wiring that keeps the demo alive.

    python tools/guardrails.py                  # check the repository
    python tools/guardrails.py --root DIR       # check another tree (tests)
    python tools/guardrails.py message FILE     # commit-msg hook: check a message
    python tools/guardrails.py history REV...   # pre-push hook: check commits

Exit code 1 means a violation. A reviewed exception is written on the offending
line (or the line above) as ``# guardrail: allow <rule> -- <reason>``; the reason
is mandatory, so every exception is visible in review.
"""

from __future__ import annotations

import ast
import io
import re
import subprocess
import sys
import tokenize
import tomllib
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path

MAX_FUNCTION_LINES = 50
MAX_COMPLEXITY = 12
MAX_COMMIT_SUBJECT = 100
MAX_TRACKED_BYTES = 1_000_000

PACKAGE = "agentcosign"
CONFIG_MODULE = "agentcosign/core/config.py"
# The longest a request may stay open (a day) and a fingerprint prompt may be
# answered (five minutes); config may lower them, never raise them.
APPROVAL_WINDOW_CEILINGS = {"open_seconds": 86_400, "fingerprint_seconds": 300}
ENGINE_MODULE = "agentcosign/services/policy_engine.py"

# Which package layers may import which.
LAYERS: dict[str, set[str]] = {
    "api": {"services", "schemas", "core", "models"},
    "services": {"clients", "models", "core"},
    "clients": {"core", "models"},
    "schemas": {"models"},
    "core": set(),
    "models": set(),
}
NETWORK_LIBS = {"httpx", "requests", "openai", "aiohttp", "urllib3"}
WEB_FRAMEWORKS = {"fastapi", "starlette"}
SECRET_UNWRAP_LAYERS = {"core", "clients", "api"}

# Names and calls
SECRET_NAME = re.compile(
    r"(secret|password|passwd|api_?key|token|hmac_?key|private_?key|authorization)",
    re.IGNORECASE,
)
SECRET_SAFE_SUFFIX = re.compile(
    r"_(len|length|count|present|set|url|uri|header|name|id|ttl|seconds|path)$"
)
TIMING_NAME = re.compile(
    r"(secret|password|api_?key|token|hmac|signature|(^|_)mac($|_))", re.IGNORECASE
)
LOGGER_NAMES = {"logger", "log", "logging", "_logger", "LOGGER", "LOG"}
LOG_METHODS = {"debug", "info", "warning", "error", "exception", "critical", "log"}
DANGEROUS_CALLS = {
    "eval",
    "exec",
    "compile",
    "__import__",
    "pickle.load",
    "pickle.loads",
    "marshal.load",
    "marshal.loads",
    "yaml.load",
    "yaml.unsafe_load",
    "os.system",
    "os.popen",
    "subprocess.getoutput",
    "ssl._create_unverified_context",
}
WEAK_HASHES = {"hashlib.md5", "hashlib.sha1"}
HTTP_CALLS = {
    f"{lib}.{verb}"
    for lib in ("httpx", "requests")
    for verb in ("get", "post", "put", "patch", "delete", "head", "request", "stream")
} | {"httpx.Client", "httpx.AsyncClient", "AsyncClient"}
SQL_METHODS = {"execute", "executemany", "executescript"}
MUTABLE_CALLS = {"list", "dict", "set", "defaultdict", "deque", "bytearray"}
PURE_BANNED_IMPORTS = {
    "httpx",
    "requests",
    "sqlite3",
    "socket",
    "subprocess",
    "random",
    "secrets",
    "os",
    "asyncio",
    "urllib",
    "openai",
}
PURE_BANNED_CALLS = {
    "open",
    "time.time",
    "time.monotonic",
    "time.perf_counter",
    "time.sleep",
    "uuid.uuid4",
    "uuid.uuid1",
}
PURE_BANNED_SUFFIXES = (".now", ".utcnow", ".today")
BRANCH_NODES = (
    ast.If,
    ast.For,
    ast.AsyncFor,
    ast.While,
    ast.ExceptHandler,
    ast.IfExp,
    ast.BoolOp,
    ast.comprehension,
    ast.match_case,
)

# Text patterns
MODEL_ID = re.compile(
    r"gpt-oss|palmyra|qwen|llama|safeguard|gemma|kimi|deepseek", re.IGNORECASE
)
PROD_PAYPAL = re.compile(r"api(-m)?\.paypal\.com|www\.paypal\.com/(v1|v2|cgi)")
BANNED_PAYPAL = re.compile(
    r"record_refund_for_invoice|record_payment_for_invoice|\bpay_order\b"
    r"|/v[12]/invoicing"
)
REPORTING_PAYPAL = re.compile(r"/v1/reporting/(transactions|balances)")
PRAGMA = re.compile(r"(?:#|//)\s*guardrail:\s*allow\s+([a-z0-9-]+)(?:\s+--\s+(\S.*))?")
SUPPRESSION = re.compile(
    r"#\s*(?:type:\s*ignore|noqa|nosec)\b(?:\[[^\]]*\]|:\s*[A-Z0-9, ]+)?(.*)$"
)
TODO_MARKER = re.compile(r"\b(TODO|FIXME|XXX|HACK)\b")
FORBIDDEN_TRACKED = re.compile(
    r"(^|/)(\.env$|.*\.db$|.*\.sqlite3?$|.*\.pyc$|__pycache__/|\.venv/|CLAUDE\.md$)"
)
INTERNAL_DOC_REF = re.compile(
    r"docs/(pm|internal|research|project-sop)\b|CLAUDE\.md|RISK-REGISTER|DEMO-DAY"
)
WEB_UNSAFE = re.compile(
    r"\.(innerHTML|outerHTML)\b|insertAdjacentHTML|document\.write|\beval\s*\("
    r"|new\s+Function\s*\(|javascript:|\son[a-z]+\s*=\s*[\"']"
)
WEB_EXTERNAL = re.compile(
    r"<(script|link|img|iframe)\b[^>]*\b(src|href)\s*=\s*[\"']https?://", re.IGNORECASE
)
EVIDENCE_LEAK = re.compile(
    r"Bearer\s+[A-Za-z0-9._-]{20,}|access_token|client_secret|"
    r"[A-Za-z0-9._%+-]+@(?!(?:[a-z0-9-]+\.)?example\.com)[A-Za-z0-9.-]+\.[a-z]{2,}",
    re.IGNORECASE,
)
ENV_SECRET_KEY = re.compile(r"(SECRET|PASSWORD|KEY|TOKEN)", re.IGNORECASE)
ENV_PLACEHOLDER = re.compile(r"^(|changeme|<.*>|your[_-].*|xxx+|.*example.*|\.\.\.)$")
RULE_ID = re.compile(r"\bR([1-9][0-9]?)\b")

# Comment hygiene: a comment explains the code in plain words. It never cites
# internal documents, rule or decision numbers, tickets, versions or project history.
META_REFERENCE = re.compile(
    r"\bADR[-\s]?\d+|\bPRD\b|\bSOP\b|\bRISK-\w+|\bG-P\d+|\bG[1-4]\b|\bP[0-2]\b"
    r"|\bR[1-9][0-9]?\b|\bS[1-9]\b|\bv\d+\.\d+\b|§\s*\d+"
    r"|CLAUDE\.md|\bdocs/[\w./-]+|requires-human-signoff"
    r"|\b(?:section|invariant|rule)\s+\d+\b"
    r"|\b(?:bug\s?fix|bugfix|hotfix|fix|issue|ticket|finding|gap|item)\s*[#-]?\s*\d+\b"
    r"|\bspikes?\s+[a-z]?\d+|\bhackathon\b|\bjudges?\b",
    re.IGNORECASE,
)
META_NARRATION = re.compile(
    r"\b(previously|used to (?:be|do|have|work|return|call)|no longer|changed from"
    r"|as discussed|owner convention"
    r"|per the (owner|spec|brief)|was changed|we changed)\b",
    re.IGNORECASE,
)
COMMENT_BLOCKS = re.compile(r"/\*.*?\*/|<!--.*?-->|(?<![:\"'])//[^\n]*", re.DOTALL)
COMMENT_SUFFIXES = {
    ".py",
    ".js",
    ".css",
    ".html",
    ".yml",
    ".yaml",
    ".toml",
    ".sh",
    ".ps1",
}
COMMENT_NAMES = {
    "Dockerfile",
    ".dockerignore",
    ".env.example",
    "pre-commit",
    "pre-push",
    "commit-msg",
}
COMMENT_SKIP_DIRS = {"docs", "node_modules", "__pycache__", "data", "out"}

# Commit messages
CONVENTIONAL = re.compile(
    r"^(feat|fix|chore|docs|test|ci|refactor|perf|build|style|revert)"
    r"(\([a-z0-9_\-/, ]+\))?!?: \S.*$"
)
EXEMPT_SUBJECTS = ("Merge ", 'Revert "', "fixup! ", "squash! ")
AI_TRAILER = re.compile(
    r"co-authored-by:.*(claude|anthropic)|generated with.*claude|\U0001f916",
    re.IGNORECASE,
)
SIGNOFF_TAG = "#requires-human-signoff"
SIGNOFF_PATHS = (
    "render.yaml",
    "Dockerfile",
    ".dockerignore",
    ".github/workflows/",
    "tools/hooks/",
    "tools/guardrails.py",
    ".env.example",
)
# Every product code change names the rules it touches and the tests that prove
# them, links the screen spec when the merchant screen changes, and says whether
# the README was updated or checked for stale facts.
PRODUCT_PATHS = ("agentcosign/",)
SCREEN_PATHS = ("agentcosign/web/",)
SPEC_PREFIX = "docs/pm/ux/"
TRAILER = re.compile(
    r"^(Constraints|Evidence|Edge cases|Spec|README):[ \t]*(.*)$", re.MULTILINE
)
# New behaviour and fixes must name edge-case tests, not only success cases.
EDGE_REQUIRED = re.compile(r"^(feat|fix)(\(|!|:)")
# Edge-case tests may never be quietly removed: the count only goes up.
EDGE_FLOOR = 49
EDGE_MARK = "@pytest.mark.edge"
CONSTRAINT_ID = re.compile(
    r"^(?:I(?:1[01]|[1-9])|R[1-9][0-9]?|H(?:10|[1-9])|G(?:10|[1-9])"
    r"|G-P[1-8]|F-\d{2}|none)$"
)
EVIDENCE_NODE = re.compile(r"^(tests/[\w/]+\.py)::(test_\w+)$")
README_VALUES = ("updated", "checked")


@dataclass
class Report:
    """Collected violations, keyed by rule name."""

    violations: dict[str, list[str]] = field(default_factory=dict)

    def add(self, rule: str, where: str) -> None:
        """Record one violation."""
        self.violations.setdefault(rule, []).append(where)


@dataclass
class FileCtx:
    """One parsed package file plus the facts rules need about it."""

    rel: str
    layer: str | None
    lines: list[str]
    tree: ast.Module
    report: Report
    allowed: dict[int, set[str]] = field(default_factory=dict)
    docstrings: set[int] = field(default_factory=set)
    model_classes: set[str] = field(default_factory=set)
    strict_classes: set[str] = field(default_factory=set)
    strict_configs: set[str] = field(default_factory=set)

    def __post_init__(self) -> None:
        self._index_pragmas()
        self._index_docstrings()
        self._index_models()

    @property
    def is_root(self) -> bool:
        """True for files directly under the package (the composition root)."""
        return self.layer is None

    @property
    def is_policy_engine(self) -> bool:
        """True for the pure decision module."""
        return self.rel == ENGINE_MODULE

    def flag(self, rule: str, line: int, detail: str = "") -> None:
        """Report a violation unless a reviewed pragma allows it on that line."""
        if rule in self.allowed.get(line, set()):
            return
        suffix = f"  {detail}" if detail else ""
        self.report.add(rule, f"{self.rel}:{line}{suffix}")

    def _index_pragmas(self) -> None:
        for number, text in enumerate(self.lines, 1):
            match = PRAGMA.search(text)
            if not match:
                continue
            target = number + 1 if text.strip().startswith(("#", "//")) else number
            self.allowed.setdefault(target, set()).add(match.group(1))
            if not match.group(2):
                self.report.add("pragma-without-reason", f"{self.rel}:{number}")

    def _index_docstrings(self) -> None:
        for node in ast.walk(self.tree):
            doc = docstring_node(node)
            if doc is not None:
                self.docstrings.add(id(doc))

    def _index_models(self) -> None:
        for stmt in self.tree.body:
            if isinstance(stmt, ast.Assign) and _is_forbid_config(stmt.value, set()):
                for target in stmt.targets:
                    if isinstance(target, ast.Name):
                        self.strict_configs.add(target.id)
        for stmt in self.tree.body:
            if not isinstance(stmt, ast.ClassDef):
                continue
            bases = {dotted(b) for b in stmt.bases}
            if bases & ({"BaseModel", "pydantic.BaseModel"} | self.model_classes):
                self.model_classes.add(stmt.name)
                if _declares_forbid(stmt, self) or bases & self.strict_classes:
                    self.strict_classes.add(stmt.name)


Rule = Callable[[FileCtx, ast.AST], None]


def dotted(node: ast.AST) -> str:
    """Return ``a.b.c`` for a name or attribute chain, else an empty string."""
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        return ".".join(reversed(parts))
    return ""


def identifiers(node: ast.AST) -> Iterator[str]:
    """Yield every name and attribute identifier inside an expression."""
    for child in ast.walk(node):
        if isinstance(child, ast.Name):
            yield child.id
        elif isinstance(child, ast.Attribute):
            yield child.attr


def docstring_node(node: ast.AST) -> ast.Constant | None:
    """Return the string constant holding a scope's docstring, if it has one."""
    scopes = (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
    if not isinstance(node, scopes) or not node.body:
        return None
    first = node.body[0]
    if not isinstance(first, ast.Expr) or not isinstance(first.value, ast.Constant):
        return None
    return first.value if isinstance(first.value.value, str) else None


def _is_log_call(node: ast.Call) -> bool:
    return (
        isinstance(node.func, ast.Attribute)
        and node.func.attr in LOG_METHODS
        and dotted(node.func.value) in LOGGER_NAMES
    )


def _is_true(node: ast.AST) -> bool:
    return isinstance(node, ast.Constant) and node.value is True


def _is_forbid_config(value: ast.AST, strict_names: set[str]) -> bool:
    if isinstance(value, ast.Name):
        return value.id in strict_names
    if isinstance(value, ast.Call):
        return any(
            kw.arg == "extra"
            and isinstance(kw.value, ast.Constant)
            and kw.value.value == "forbid"
            for kw in value.keywords
        )
    return False


def _declares_forbid(cls: ast.ClassDef, ctx: FileCtx) -> bool:
    for stmt in cls.body:
        if not isinstance(stmt, ast.Assign):
            continue
        if any(
            isinstance(t, ast.Name) and t.id == "model_config" for t in stmt.targets
        ):
            return _is_forbid_config(stmt.value, ctx.strict_configs)
    return False


def _secret_like(name: str) -> bool:
    return bool(SECRET_NAME.search(name)) and not SECRET_SAFE_SUFFIX.search(name)


def _imported_modules(node: ast.AST, rel: str) -> list[str]:
    if isinstance(node, ast.Import):
        return [alias.name for alias in node.names]
    if not isinstance(node, ast.ImportFrom):
        return []
    if node.level:
        package = rel.removesuffix(".py").split("/")[:-1]
        base = package[: len(package) - (node.level - 1)]
        if node.module:
            return [".".join([*base, node.module])]
        return [".".join([*base, alias.name]) for alias in node.names]
    if node.module == PACKAGE:
        return [f"{PACKAGE}.{alias.name}" for alias in node.names]
    return [node.module or ""]


# --------------------------------------------------------------------------- rules


def rule_imports(ctx: FileCtx, node: ast.AST) -> None:
    """Layering, framework and network-library boundaries."""
    if not isinstance(node, (ast.Import, ast.ImportFrom)):
        return
    for module in _imported_modules(node, ctx.rel):
        parts = module.split(".")
        top = parts[0]
        if top == "requests":
            ctx.flag("sync-http-library", node.lineno, module)
        if top in NETWORK_LIBS and ctx.layer != "clients" and not ctx.is_root:
            ctx.flag("network-library-outside-clients", node.lineno, module)
        if top in WEB_FRAMEWORKS and ctx.layer != "api" and not ctx.is_root:
            ctx.flag("web-framework-outside-api", node.lineno, module)
        if top == PACKAGE and len(parts) > 1 and ctx.layer in LAYERS:
            target = parts[1]
            forbidden = target in LAYERS and target != ctx.layer
            if forbidden and target not in LAYERS[ctx.layer]:
                ctx.flag("layer-violation", node.lineno, f"{ctx.layer}->{target}")
        if ctx.is_policy_engine and top in PURE_BANNED_IMPORTS:
            ctx.flag("impure-policy-engine", node.lineno, f"imports {module}")
        if top == "os" and len(parts) > 1 and parts[1] == "path":
            ctx.flag("os-path-use-pathlib", node.lineno)


def rule_calls(ctx: FileCtx, node: ast.AST) -> None:
    """Dangerous, unbounded or unsafe calls."""
    if not isinstance(node, ast.Call):
        return
    name = dotted(node.func)
    if name == "print":
        ctx.flag("print-in-package", node.lineno)
    if name in DANGEROUS_CALLS:
        ctx.flag("dangerous-call", node.lineno, name)
    if name in WEAK_HASHES:
        ctx.flag("weak-hash", node.lineno, name)
    for kw in node.keywords:
        constant = kw.value.value if isinstance(kw.value, ast.Constant) else object()
        if kw.arg == "shell" and _is_true(kw.value):
            ctx.flag("shell-true", node.lineno)
        if kw.arg == "verify" and constant is False:
            ctx.flag("tls-verify-disabled", node.lineno)
        if kw.arg in {"debug", "reload"} and _is_true(kw.value):
            ctx.flag("debug-or-reload-enabled", node.lineno, kw.arg)
    if name in HTTP_CALLS:
        _check_timeout(ctx, node, name)
    if name.startswith("subprocess.") and not node.keywords:
        ctx.flag("subprocess-without-explicit-args", node.lineno, name)


def _check_timeout(ctx: FileCtx, node: ast.Call, name: str) -> None:
    if any(kw.arg is None for kw in node.keywords):  # **kwargs: cannot know
        return
    timeout = next((kw for kw in node.keywords if kw.arg == "timeout"), None)
    if timeout is None:
        ctx.flag("http-without-timeout", node.lineno, name)
        return
    unbounded = isinstance(timeout.value, ast.Constant) and timeout.value.value is None
    if unbounded:
        ctx.flag("http-without-timeout", node.lineno, name)


def rule_money(ctx: FileCtx, node: ast.AST) -> None:
    """Money is Decimal: no float annotations, conversions or Decimal(float)."""
    candidates: list[ast.expr | None] = []
    if isinstance(node, (ast.AnnAssign, ast.arg)):
        candidates.append(node.annotation)
    elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        candidates.append(node.returns)
    for annotation in (c for c in candidates if c is not None):
        if any(
            isinstance(n, ast.Name) and n.id == "float" for n in ast.walk(annotation)
        ):
            ctx.flag("float-annotation", annotation.lineno)
    if not isinstance(node, ast.Call):
        return
    name = dotted(node.func)
    if name == "float":
        ctx.flag("float-conversion", node.lineno)
    if name in {"Decimal", "decimal.Decimal"} and node.args:
        arg = node.args[0]
        is_float_const = isinstance(arg, ast.Constant) and isinstance(arg.value, float)
        if is_float_const or (
            isinstance(arg, ast.Call) and dotted(arg.func) == "float"
        ):
            ctx.flag("decimal-from-float", node.lineno)


def rule_time(ctx: FileCtx, node: ast.AST) -> None:
    """Naive datetimes and impure clock reads in the policy engine."""
    if not isinstance(node, ast.Call):
        return
    name = dotted(node.func)
    has_args = bool(node.args or node.keywords)
    if name.endswith((".utcnow", ".utcfromtimestamp")):
        ctx.flag("naive-datetime", node.lineno, name)
    if name.endswith((".now", ".today")) and not has_args:
        ctx.flag("naive-datetime", node.lineno, name)
    if ctx.is_policy_engine and (
        name in PURE_BANNED_CALLS or name.endswith(PURE_BANNED_SUFFIXES)
    ):
        ctx.flag("impure-policy-engine", node.lineno, f"calls {name}")


def rule_environment(ctx: FileCtx, node: ast.AST) -> None:
    """Environment access only through Settings (SSOT)."""
    if ctx.rel == CONFIG_MODULE:
        return
    if isinstance(node, ast.Attribute) and dotted(node) in {
        "os.environ",
        "os.getenv",
        "os.putenv",
    }:
        ctx.flag("env-read-outside-config", node.lineno)
    if (
        isinstance(node, ast.ImportFrom)
        and node.module == "os"
        and any(alias.name in {"environ", "getenv"} for alias in node.names)
    ):
        ctx.flag("env-read-outside-config", node.lineno)
    if isinstance(node, ast.Call) and dotted(node.func).endswith("load_dotenv"):
        ctx.flag("env-read-outside-config", node.lineno)


def rule_exceptions(ctx: FileCtx, node: ast.AST) -> None:
    """Fail closed: no blanket, swallowed or allow-on-error handlers."""
    if not isinstance(node, ast.ExceptHandler):
        return
    if node.type is None:
        ctx.flag("bare-except", node.lineno)
    else:
        caught = node.type.elts if isinstance(node.type, ast.Tuple) else [node.type]
        if any(dotted(t) in {"Exception", "BaseException"} for t in caught):
            ctx.flag("blind-except", node.lineno)
    inert = (ast.Pass, ast.Continue)
    if all(
        isinstance(s, inert)
        or (isinstance(s, ast.Expr) and isinstance(s.value, ast.Constant))
        for s in node.body
    ):
        ctx.flag("swallowed-exception", node.lineno)
    for child in ast.walk(node):
        # An error path must deny, never allow.
        is_attribute = isinstance(child, ast.Attribute) and child.attr == "ALLOW"
        is_name = isinstance(child, ast.Name) and child.id == "ALLOW"
        if (is_attribute or is_name) and hasattr(child, "lineno"):
            ctx.flag("allow-on-error-path", child.lineno)


def rule_secrets(ctx: FileCtx, node: ast.AST) -> None:
    """Secrets: never compared with ==, never logged, unwrapped only at the edge."""
    outside_boundary = ctx.layer is not None and ctx.layer not in SECRET_UNWRAP_LAYERS
    if (
        isinstance(node, ast.Attribute)
        and node.attr == "get_secret_value"
        and outside_boundary
    ):
        ctx.flag("secret-unwrapped-outside-boundary", node.lineno)
    if isinstance(node, ast.Compare):
        _check_secret_compare(ctx, node)
    if isinstance(node, ast.Call) and _is_log_call(node):
        _check_secret_log(ctx, node)


def _check_secret_compare(ctx: FileCtx, node: ast.Compare) -> None:
    if not any(isinstance(op, (ast.Eq, ast.NotEq)) for op in node.ops):
        return
    operands = [node.left, *node.comparators]
    for operand in operands:
        if isinstance(operand, ast.Constant) and isinstance(
            operand.value, (str, bool, int, type(None))
        ):
            return  # emptiness or sentinel check, not a secret-to-secret compare
        if isinstance(operand, ast.Call) and dotted(operand.func) == "len":
            return
    if any(TIMING_NAME.search(i) for o in operands for i in identifiers(o)):
        ctx.flag("secret-compared-with-eq", node.lineno)  # use hmac.compare_digest


def _check_secret_log(ctx: FileCtx, node: ast.Call) -> None:
    for arg in [*node.args, *(kw.value for kw in node.keywords)]:
        if any(_secret_like(i) or i == "get_secret_value" for i in identifiers(arg)):
            ctx.flag("secret-in-log", node.lineno)
            return


def rule_database(ctx: FileCtx, node: ast.AST) -> None:
    """SQL is parameterised: no string building passed to execute."""
    if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
        return
    if node.func.attr not in SQL_METHODS or not node.args:
        return
    query = node.args[0]
    built = isinstance(query, ast.JoinedStr)
    built = built or (
        isinstance(query, ast.BinOp) and isinstance(query.op, (ast.Add, ast.Mod))
    )
    built = built or (
        isinstance(query, ast.Call)
        and isinstance(query.func, ast.Attribute)
        and query.func.attr == "format"
    )
    if built:
        ctx.flag("sql-string-building", node.lineno)


def rule_constants(ctx: FileCtx, node: ast.AST) -> None:
    """String constants: model IDs, production PayPal, banned PayPal surface."""
    if not (isinstance(node, ast.Constant) and isinstance(node.value, str)):
        return
    if id(node) in ctx.docstrings:
        return
    value, line = node.value, node.lineno
    if MODEL_ID.search(value):
        ctx.flag("model-id-outside-config-yaml", line)
    if PROD_PAYPAL.search(value):
        ctx.flag("production-paypal-endpoint", line)
    if BANNED_PAYPAL.search(value):
        ctx.flag("banned-paypal-surface", line)  # paths that bypass the gateway
    if REPORTING_PAYPAL.search(value) and "reconcil" not in ctx.rel:
        ctx.flag("paypal-reporting-outside-reconciliation", line)
    if value == "0.0.0.0":  # noqa: S104 -- detecting, not binding
        ctx.flag("bind-all-interfaces", line)


def rule_functions(ctx: FileCtx, node: ast.AST) -> None:
    """Small, documented, simple functions with safe defaults."""
    if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return
    length = (node.end_lineno or node.lineno) - node.lineno + 1
    if length > MAX_FUNCTION_LINES:
        ctx.flag("function-too-long", node.lineno, f"{node.name} ({length} lines)")
    branches = sum(isinstance(n, BRANCH_NODES) for n in ast.walk(node))
    if branches + 1 > MAX_COMPLEXITY:
        ctx.flag("function-too-complex", node.lineno, f"{node.name} ({branches + 1})")
    public = not node.name.startswith("_")
    if public and not ast.get_docstring(node) and not ctx.rel.endswith("__init__.py"):
        ctx.flag("public-function-without-docstring", node.lineno, node.name)
    defaults = [*node.args.defaults, *(d for d in node.args.kw_defaults if d)]
    for default in defaults:
        literal = isinstance(default, (ast.List, ast.Dict, ast.Set))
        call = isinstance(default, ast.Call) and dotted(default.func) in MUTABLE_CALLS
        if literal or call:
            ctx.flag("mutable-default-argument", node.lineno, node.name)


def rule_classes(ctx: FileCtx, node: ast.AST) -> None:
    """Documented classes, strict models, secrets typed as SecretStr."""
    if not isinstance(node, ast.ClassDef):
        return
    if not node.name.startswith("_") and not ast.get_docstring(node):
        ctx.flag("public-class-without-docstring", node.lineno, node.name)
    if node.name in ctx.model_classes and node.name not in ctx.strict_classes:
        ctx.flag("model-without-extra-forbid", node.lineno, node.name)
    for stmt in node.body:
        if not (isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name)):
            continue
        annotation = ast.unparse(stmt.annotation)
        is_text = "str" in annotation and "SecretStr" not in annotation
        if _secret_like(stmt.target.id) and is_text:
            ctx.flag("secret-field-not-secretstr", stmt.lineno, stmt.target.id)


def rule_comparisons(ctx: FileCtx, node: ast.AST) -> None:
    """Value comparisons use ==, not ``is``."""
    if not isinstance(node, ast.Compare):
        return
    if not any(isinstance(op, (ast.Is, ast.IsNot)) for op in node.ops):
        return
    for operand in node.comparators:
        if isinstance(operand, ast.Constant) and operand.value not in (
            None,
            True,
            False,
        ):
            ctx.flag("is-compared-to-literal", node.lineno)


NODE_RULES: tuple[Rule, ...] = (
    rule_imports,
    rule_calls,
    rule_money,
    rule_time,
    rule_environment,
    rule_exceptions,
    rule_secrets,
    rule_database,
    rule_constants,
    rule_functions,
    rule_classes,
    rule_comparisons,
)


# --------------------------------------------------------------------- file checks


def scan_text(ctx: FileCtx) -> None:
    """Line-level rules: TODO markers and reasonless suppressions."""
    for number, text in enumerate(ctx.lines, 1):
        if TODO_MARKER.search(text):
            ctx.flag("todo-marker", number)
        match = SUPPRESSION.search(text)
        if match and not match.group(1).strip(" -#:"):
            ctx.flag("suppression-without-reason", number)
    for rule_check in (_check_payload_logging,):
        rule_check(ctx)


def _check_payload_logging(ctx: FileCtx) -> None:
    """Clients must not log whole request or response payloads."""
    if ctx.layer != "clients":
        return
    for node in ast.walk(ctx.tree):
        if not (isinstance(node, ast.Call) and _is_log_call(node)):
            continue
        names = {i for a in node.args for i in identifiers(a)}
        if names & {"payload", "response", "body", "request", "json"}:
            ctx.flag("payload-in-log", node.lineno)


def _check_paypal_request_id(ctx: FileCtx) -> None:
    """PayPal writes must send PayPal-Request-Id so a retry cannot duplicate."""
    if ctx.layer != "clients" or "paypal" not in ctx.rel:
        return
    writes = [
        n
        for n in ast.walk(ctx.tree)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and n.func.attr in {"post", "put", "patch"}
    ]
    has_header = any(
        isinstance(n, ast.Constant)
        and isinstance(n.value, str)
        and n.value.lower() == "paypal-request-id"
        for n in ast.walk(ctx.tree)
    )
    if writes and not has_header:
        ctx.flag("paypal-write-without-request-id", writes[0].lineno)


def check_package(root: Path, report: Report) -> None:
    """Run every file-level rule over the shipped package."""
    package = root / PACKAGE
    if not package.is_dir():
        return
    for path in sorted(package.rglob("*.py")):
        rel = path.relative_to(root).as_posix()
        parts = path.relative_to(package).parts
        text = path.read_text(encoding="utf-8")
        try:
            tree = ast.parse(text)
        except SyntaxError as exc:
            report.add("syntax-error", f"{rel}:{exc.lineno}")
            continue
        ctx = FileCtx(
            rel=rel,
            layer=parts[0] if len(parts) > 1 else None,
            lines=text.splitlines(),
            tree=tree,
            report=report,
        )
        scan_text(ctx)
        _check_paypal_request_id(ctx)
        for node in ast.walk(tree):
            for rule in NODE_RULES:
                rule(ctx, node)


# -------------------------------------------------------------------- repo checks


def _read(root: Path, rel: str) -> str | None:
    path = root / rel
    return path.read_text(encoding="utf-8") if path.is_file() else None


def _git(root: Path, *args: str) -> list[str]:
    try:
        out = subprocess.run(
            ["git", *args], cwd=root, capture_output=True, text=True, check=True
        )
    except (OSError, subprocess.CalledProcessError):
        return []
    return out.stdout.splitlines()


def check_tracked_files(root: Path, report: Report) -> None:
    """Nothing secret, generated or huge is tracked."""
    staged = _git(root, "diff", "--cached", "--name-only", "--diff-filter=AM")
    for rel in sorted({*_git(root, "ls-files"), *staged}):
        if FORBIDDEN_TRACKED.search(rel):
            report.add("forbidden-tracked-file", rel)
        path = root / rel
        if path.is_file() and path.stat().st_size > MAX_TRACKED_BYTES:
            report.add("oversized-tracked-file", rel)


def check_config_pins(root: Path, report: Report) -> None:
    """The safety pins that must never be edited away."""
    source = _read(root, CONFIG_MODULE)
    if source is None:
        return
    if 'paypal_mode: Literal["sandbox"]' not in source:
        report.add("paypal-mode-not-pinned-to-sandbox", CONFIG_MODULE)
    if 'extra="forbid"' not in source:
        report.add("config-accepts-unknown-keys", CONFIG_MODULE)
    for name, ceiling in APPROVAL_WINDOW_CEILINGS.items():
        cap = re.search(rf"\b{name}\b[^\n]*\ble\s*=\s*([\d_]+)", source)
        if cap is None or int(cap.group(1).replace("_", "")) > ceiling:
            report.add("approval-window-cap-missing-or-raised", CONFIG_MODULE)


def check_web_assets(root: Path, report: Report) -> None:
    """Untrusted text stays text; no external code; owner copy style."""
    web = root / PACKAGE / "web"
    if not web.is_dir():
        return
    for path in sorted(
        p for p in web.rglob("*") if p.suffix in {".html", ".js", ".css"}
    ):
        rel = path.relative_to(root).as_posix()
        for number, text in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if PRAGMA.search(text) or PRAGMA.search(text.rstrip() + " "):
                continue
            stripped = text.lstrip()
            if "—" in text and not stripped.startswith(("//", "/*", "*", "<!--")):
                report.add("em-dash-in-ui-copy", f"{rel}:{number}")
            if WEB_UNSAFE.search(text):
                report.add("unsafe-dom-or-eval", f"{rel}:{number}")
            if WEB_EXTERNAL.search(text):
                report.add("external-web-resource", f"{rel}:{number}")


MERCHANT_SCREEN = ("approve.html", "approve.js", "approve.css")
JS_LITERAL = re.compile(r'"((?:[^"\\]|\\.)*)"')
HTML_TEXT = re.compile(r">([^<>]+)<")
MERCHANT_JARGON = re.compile(
    r"\bR\d{1,2}\b|(?i:\b(?:agent|reject\w*|passkey|ledger|escalat\w*|"
    r"webauthn|policy|rule|error|exception|payload|hash|undefined|null)\b)"
)
CONFIRMED_SUCCESS = 'paypal_status === "COMPLETED"'
CSS_FONT = re.compile(r"font-size:\s*([\d.]+)(px|rem|em)")
BUTTON_HEIGHT = re.compile(r"(?s)\nbutton\s*\{[^}]*?min-height:\s*(\d+)px")
MIN_TEXT_PX = 14
MIN_BUTTON_PX = 48


def _merchant_copy(path: Path, text: str) -> list[tuple[int, str]]:
    """Human-readable strings on the merchant screen, with their line numbers."""
    pattern = JS_LITERAL if path.suffix == ".js" else HTML_TEXT
    found: list[tuple[int, str]] = []
    for number, line in enumerate(text.splitlines(), 1):
        if line.lstrip().startswith(("//", "/*", "*", "<!--")):
            continue
        for match in pattern.finditer(line):
            copy = match.group(1).strip()
            if " " in copy:  # words for people, not ids, paths or class names
                found.append((number, copy))
    return found


def _check_merchant_words(root: Path, report: Report) -> None:
    """Owner words only, and success is only claimed when PayPal confirmed it."""
    web = root / PACKAGE / "web"
    for name in ("approve.html", "approve.js"):
        path = web / name
        if not path.is_file():
            continue
        rel = path.relative_to(root).as_posix()
        text = path.read_text(encoding="utf-8")
        lines = text.splitlines()
        for number, copy in _merchant_copy(path, text):
            if PRAGMA.search(lines[number - 1]):
                continue
            if MERCHANT_JARGON.search(copy):
                report.add("merchant-jargon", f"{rel}:{number}")
            if "Refund sent" in copy or "refunded to" in copy:
                window = "\n".join(lines[max(0, number - 4) : number])
                if CONFIRMED_SUCCESS not in window:
                    report.add("success-without-paypal-confirmation", f"{rel}:{number}")
        if path.suffix == ".js" and "setInterval" in text:
            report.add("ticking-timer-on-merchant-screen", rel)


def _check_merchant_style(root: Path, report: Report) -> None:
    """Fixed light palette, readable text and thumb-sized buttons."""
    css = root / PACKAGE / "web" / "approve.css"
    if not css.is_file():
        return
    rel = css.relative_to(root).as_posix()
    text = css.read_text(encoding="utf-8")
    if "color-scheme: only light" not in text or "prefers-color-scheme" in text:
        report.add("merchant-screen-not-fixed-light", rel)
    for match in CSS_FONT.finditer(text):
        size, unit = float(match.group(1)), match.group(2)
        px = size if unit == "px" else size * 16
        if px < MIN_TEXT_PX:
            report.add("text-below-14px", f"{rel}:{match.group(0)}")
    button = BUTTON_HEIGHT.search(text)
    if button is None or int(button.group(1)) < MIN_BUTTON_PX:
        report.add("button-below-48px", rel)


def check_merchant_screen(root: Path, report: Report) -> None:
    """The mechanically checkable product UX gates for the merchant's screen."""
    if not (root / PACKAGE / "web" / MERCHANT_SCREEN[0]).is_file():
        return
    _check_merchant_words(root, report)
    _check_merchant_style(root, report)


README_SCENARIOS = re.compile(r"(\d+)-row scenario table")
README_LIMITS = re.compile(r"defaults \$([\d,]+) / \$([\d,]+)")
README_PYTHON = re.compile(r"Python-(\d+\.\d+)%2B")
SCENARIO_ROW = re.compile(r"^    Scenario\(", re.MULTILINE)


def _yaml_money(config: str, key: str) -> str | None:
    match = re.search(rf'^\s*{key}:\s*"?([\d.]+)"?', config, re.MULTILINE)
    return match.group(1) if match else None


def check_edge_floor(root: Path, report: Report) -> None:
    """Edge-case tests may be added but never removed below the recorded floor."""
    tests = root / "tests"
    if not tests.is_dir():
        return
    count = sum(
        p.read_text(encoding="utf-8").count(EDGE_MARK) for p in tests.rglob("*.py")
    )
    if count < EDGE_FLOOR:
        report.add("edge-tests-below-floor", f"{count} edge tests, floor {EDGE_FLOOR}")


def check_readme_facts(root: Path, report: Report) -> None:
    """Numbers the README states must match the code they describe."""
    readme_path = root / "README.md"
    if not readme_path.is_file():
        return
    readme = readme_path.read_text(encoding="utf-8")
    table = root / "tests" / "scenarios" / "policy_scenarios.py"
    stated = README_SCENARIOS.search(readme)
    if stated and table.is_file():
        actual = len(SCENARIO_ROW.findall(table.read_text(encoding="utf-8")))
        if int(stated.group(1)) != actual:
            report.add(
                "readme-stale-fact",
                f"scenario table: README {stated.group(1)}, code {actual}",
            )
    config_path = root / "config.yaml"
    limits = README_LIMITS.search(readme)
    if limits and config_path.is_file():
        config = config_path.read_text(encoding="utf-8")
        pairs = zip(
            limits.groups(), ("refund_auto_limit", "daily_outflow_cap"), strict=True
        )
        for said, key in pairs:
            value = _yaml_money(config, key)
            if value is None or float(said.replace(",", "")) != float(value):
                report.add(
                    "readme-stale-fact", f"{key}: README ${said}, config {value}"
                )
    pyproject = root / "pyproject.toml"
    badge = README_PYTHON.search(readme)
    if badge and pyproject.is_file():
        wanted = re.search(
            r'requires-python\s*=\s*">=(\d+\.\d+)"',
            pyproject.read_text(encoding="utf-8"),
        )
        if wanted is None or wanted.group(1) != badge.group(1):
            report.add(
                "readme-stale-fact", "Python version badge differs from pyproject"
            )


def check_public_docs(root: Path, report: Report) -> None:
    """Public docs never point at gitignored internal material."""
    docs = [root / "README.md", *sorted((root / "docs").glob("*.md"))]
    for path in docs:
        if not path.is_file():
            continue
        for number, text in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if INTERNAL_DOC_REF.search(text):
                rel = path.relative_to(root).as_posix()
                report.add("public-doc-references-internal", f"{rel}:{number}")


def check_dependency_ranges(root: Path, report: Report) -> None:
    """Dependencies are bounded ranges so an upstream major cannot break deploys."""
    source = _read(root, "pyproject.toml")
    if source is None:
        return
    project = tomllib.loads(source).get("project", {})
    requirements = list(project.get("dependencies", []))
    for group in project.get("optional-dependencies", {}).values():
        requirements.extend(group)
    for requirement in requirements:
        if not re.search(r"<|==|~=", requirement):
            report.add("dependency-without-upper-bound", requirement)


def check_wiring(root: Path, report: Report) -> None:
    """The guardrails must stay wired into CI and every hook."""
    expectations = {
        ".github/workflows/ci.yml": "tools/guardrails.py",
        "tools/hooks/pre-commit": "guardrails.py",
        "tools/hooks/pre-push": "guardrails.py",
        "tools/hooks/commit-msg": "guardrails.py",
    }
    for rel, needle in expectations.items():
        source = _read(root, rel)
        if source is not None and needle not in source:
            report.add("guardrail-not-wired", rel)


def check_deploy_files(root: Path, report: Report) -> None:
    """Demo-day invariants for the container and the Render blueprint."""
    render = _read(root, "render.yaml")
    if render is not None:
        if not re.search(r"^\s*autoDeploy:\s*false\b", render, re.MULTILINE):
            report.add("render-autodeploy-not-disabled", "render.yaml")
        if not re.search(r"^\s*branch:\s*release\b", render, re.MULTILINE):
            report.add("render-not-pinned-to-release-branch", "render.yaml")
    dockerfile = _read(root, "Dockerfile")
    if dockerfile is not None:
        if not re.search(r"^USER\s+(?!root\b)\S+", dockerfile, re.MULTILINE):
            report.add("container-runs-as-root", "Dockerfile")
        if "HEALTHCHECK" not in dockerfile:
            report.add("container-without-healthcheck", "Dockerfile")
        if re.search(r"^FROM\s+\S+:latest\b", dockerfile, re.MULTILINE):
            report.add("container-base-image-unpinned", "Dockerfile")
        ignore = _read(root, ".dockerignore")
        if ignore is None or ".env" not in ignore:
            report.add("dockerignore-missing-env", ".dockerignore")


def check_env_example(root: Path, report: Report) -> None:
    """.env.example documents keys; it never carries real values."""
    source = _read(root, ".env.example")
    if source is None:
        return
    for number, text in enumerate(source.splitlines(), 1):
        key, _, value = text.partition("=")
        if text.startswith("#") or not key.strip():
            continue
        value = value.split("#", 1)[0].strip().strip("\"'")
        if ENV_SECRET_KEY.search(key) and not ENV_PLACEHOLDER.match(value.lower()):
            report.add("env-example-has-value", f".env.example:{number} {key.strip()}")


def check_policy_coverage(root: Path, report: Report) -> None:
    """Every rule the engine implements must be named in a test."""
    engine = _read(root, ENGINE_MODULE)
    if engine is None:
        return
    tests = "\n".join(
        p.read_text(encoding="utf-8") for p in (root / "tests").rglob("*.py")
    )
    implemented = set(RULE_ID.findall(engine))
    tested = set(RULE_ID.findall(tests))
    for rule_number in sorted(implemented - tested, key=int):
        report.add("policy-rule-without-test", f"R{rule_number}")


def check_evidence(root: Path, report: Report) -> None:
    """Committed sandbox evidence carries no tokens, secrets or real personal data."""
    evidence = root / "docs" / "evidence"
    if not evidence.is_dir():
        return
    for path in sorted(p for p in evidence.rglob("*") if p.is_file()):
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        if EVIDENCE_LEAK.search(text):
            report.add("evidence-not-sanitised", path.relative_to(root).as_posix())


def _python_comments(text: str) -> Iterator[tuple[int, str]]:
    try:
        for token in tokenize.generate_tokens(io.StringIO(text).readline):
            if token.type == tokenize.COMMENT:
                yield token.start[0], token.string
        tree = ast.parse(text)
    except (tokenize.TokenError, SyntaxError):
        return
    for node in ast.walk(tree):
        doc = docstring_node(node)
        if doc is None:
            continue
        for offset, line in enumerate(str(doc.value).splitlines()):
            yield doc.lineno + offset, line


def _c_style_comments(text: str) -> Iterator[tuple[int, str]]:
    for match in COMMENT_BLOCKS.finditer(text):
        start = text.count("\n", 0, match.start()) + 1
        for offset, line in enumerate(match.group(0).splitlines()):
            yield start + offset, line


def _hash_comments(text: str) -> Iterator[tuple[int, str]]:
    for number, line in enumerate(text.splitlines(), 1):
        match = re.search(r"(?:^|\s)#(.*)$", line)
        if match:
            yield number, match.group(1)


def _comment_files(root: Path) -> Iterator[Path]:
    for path in sorted(root.rglob("*")):
        parts = path.relative_to(root).parts
        if any(p in COMMENT_SKIP_DIRS or p.endswith(".egg-info") for p in parts[:-1]):
            continue
        if any(p.startswith(".") and p != ".github" for p in parts[:-1]):
            continue
        if path.is_file() and (
            path.suffix in COMMENT_SUFFIXES or path.name in COMMENT_NAMES
        ):
            yield path


def check_comment_hygiene(root: Path, report: Report) -> None:
    """Comments explain the code; they never cite internal documents or history."""
    for path in _comment_files(root):
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        if path.suffix == ".py":
            comments = _python_comments(text)
        elif path.suffix in {".js", ".css", ".html"}:
            comments = _c_style_comments(text)
        else:
            comments = _hash_comments(text)
        rel = path.relative_to(root).as_posix()
        for number, comment in comments:
            if META_REFERENCE.search(comment):
                report.add("internal-reference-in-comment", f"{rel}:{number}")
            elif META_NARRATION.search(comment):
                report.add("process-narration-in-comment", f"{rel}:{number}")


# ------------------------------------------------- hard-coding and fake data

PLACEHOLDER = re.compile(
    r"\b(TODO|FIXME|XXX|TBD|HACK)\b|lorem ipsum|\[your [a-z ]+\]|"
    r"\bexample\.(com|org|net)\b|\bacme\b|\bdummy\b",
    re.IGNORECASE,
)
FAKE_DATA = re.compile(
    r"\b(Jane|John) (Buyer|Doe)\b|\bTest Buyer\b|\b4111[ -]?1111[ -]?1111[ -]?1111\b|"
    r"\b4[0-9]{15}\b|[\w.+-]+@[\w-]+\.(com|org|net|in|io)\b|\bORD-[0-9]+\b"
)
# "24 hours" and "24-hour" alike
DURATION_IN_COPY = re.compile(
    r"\b\d+[\s-]+(second|seconds|minute|minutes|hour|hours|day|days)\b",
    re.IGNORECASE,
)
MONEY_IN_COPY = re.compile(r"\$\s?\d")
BARE_NUMBER = re.compile(r"(?<![\w.$#-])\d{4,}(?![\w])")
NAMED_CONSTANT = re.compile(r"^\s*var [A-Z][A-Z0-9_]* = ")
JS_STRING = re.compile(r'"(?:[^"\\]|\\.)*"' + r"|'(?:[^'\\]|\\.)*'")
CODE_SUFFIXES = {".py", ".js", ".html", ".css"}
# Python modules whose string literals are words people read
WORDING_MODULES = (
    "services/customer_copy.py",
    "services/approval_view.py",
    "cli.py",
)


def _string_literals(text: str) -> list[tuple[int, str]]:
    """Every plain string constant in Python source, with its line number."""
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return []
    return [
        (node.lineno, node.value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    ]


def _scan_lines(
    rel: str, lines: list[str], pattern: re.Pattern[str], rule: str, report: Report
) -> None:
    for number, line in enumerate(lines, 1):
        if pattern.search(line) and not PRAGMA.search(line):
            report.add(rule, f"{rel}:{number}")


def _check_script_numbers(rel: str, lines: list[str], report: Report) -> None:
    """Bare numbers in the phone script must be named constants."""
    inside_icons = False
    for number, line in enumerate(lines, 1):
        if "var ICONS = {" in line:
            inside_icons = True
        if inside_icons:
            inside_icons = line.strip() != "};"
            continue
        if NAMED_CONSTANT.match(line) or PRAGMA.search(line):
            continue
        # numbers inside strings are words or addresses, checked by the copy rules
        if BARE_NUMBER.search(JS_STRING.sub('""', line)):
            report.add("bare-number-in-script", f"{rel}:{number}")


def check_no_hardcoding(root: Path, report: Report) -> None:
    """No placeholders, sample data, or values typed into words the server owns."""
    package = root / PACKAGE
    if not package.is_dir():
        return
    for path in sorted(p for p in package.rglob("*") if p.suffix in CODE_SUFFIXES):
        rel = path.relative_to(root).as_posix()
        lines = path.read_text(encoding="utf-8").splitlines()
        _scan_lines(rel, lines, PLACEHOLDER, "placeholder-in-code", report)
        _scan_lines(rel, lines, FAKE_DATA, "fake-data-in-code", report)
    web = package / "web"
    for name in ("approve.html", "approve.js"):
        path = web / name
        if not path.is_file():
            continue
        rel = path.relative_to(root).as_posix()
        text = path.read_text(encoding="utf-8")
        lines = text.splitlines()
        for number, copy in _merchant_copy(path, text):
            if PRAGMA.search(lines[number - 1]):
                continue
            if DURATION_IN_COPY.search(copy):
                report.add("duration-in-copy", f"{rel}:{number}")
            if MONEY_IN_COPY.search(copy):
                report.add("money-in-copy", f"{rel}:{number}")
        if path.suffix == ".js":
            _check_script_numbers(rel, lines, report)
    for module in WORDING_MODULES:
        path = package / module
        if not path.is_file():
            continue
        rel = path.relative_to(root).as_posix()
        text = path.read_text(encoding="utf-8")
        lines = text.splitlines()
        for number, value in _string_literals(text):
            if DURATION_IN_COPY.search(value) and not PRAGMA.search(lines[number - 1]):
                report.add("duration-in-copy", f"{rel}:{number}")


REPO_CHECKS: tuple[Callable[[Path, Report], None], ...] = (
    check_comment_hygiene,
    check_tracked_files,
    check_config_pins,
    check_web_assets,
    check_merchant_screen,
    check_no_hardcoding,
    check_edge_floor,
    check_readme_facts,
    check_public_docs,
    check_dependency_ranges,
    check_wiring,
    check_deploy_files,
    check_env_example,
    check_policy_coverage,
    check_evidence,
)


def check_repo(root: Path, report: Report) -> None:
    """Run every repository-level check."""
    for check in REPO_CHECKS:
        check(root, report)


# ----------------------------------------------------------------- commit messages


def check_message(message: str, files: Sequence[str]) -> list[str]:
    """Return problems with a commit message given the files it changes."""
    lines = [ln for ln in message.splitlines() if not re.match(r"#(\s|$)", ln)]
    subject = next((ln for ln in lines if ln.strip()), "")
    problems: list[str] = []
    if not subject.startswith(EXEMPT_SUBJECTS):
        if not CONVENTIONAL.match(subject):
            problems.append(f"subject is not a Conventional Commit: {subject!r}")
        if len(subject) > MAX_COMMIT_SUBJECT:
            problems.append(f"subject longer than {MAX_COMMIT_SUBJECT} characters")
        if subject.endswith("."):
            problems.append("subject must not end with a period")
    if AI_TRAILER.search(message):
        problems.append("AI attribution found (owner convention: authored alone)")
    touched = [f for f in files if f.startswith(SIGNOFF_PATHS)]
    tagged = any(re.search(rf"\S.*{SIGNOFF_TAG}", ln) for ln in lines)
    if touched and not tagged:
        problems.append(
            f"touches {', '.join(sorted(touched)[:3])}: add a line such as "
            f'"Signoff: {SIGNOFF_TAG}" (a human must approve this change)'
        )
    return problems


def _node_problems(
    label: str, value: str, read: Callable[[str], str | None], edge: bool
) -> list[str]:
    """Each named test must exist in the committed code (and be marked edge)."""
    nodes = [n for n in re.split(r"[,\s]+", value) if n]
    if not nodes:
        return [f"{label}: name at least one test as tests/...py::test_name"]
    problems: list[str] = []
    for node in nodes:
        match = EVIDENCE_NODE.match(node)
        if match is None:
            problems.append(f"{label}: {node!r} is not tests/...py::test_name")
            continue
        source = read(match.group(1)) or ""
        name = match.group(2)
        defined = re.compile(rf"^\s*(?:async\s+)?def {name}\(", re.M)
        marked = re.compile(
            rf"^@pytest\.mark\.edge\b[^\n]*\n(?:@[^\n]*\n)*(?:async\s+)?def {name}\(",
            re.M,
        )
        if not defined.search(source):
            problems.append(f"{label}: {node} does not exist in the commit")
        elif edge and not marked.search(source):
            problems.append(f"{label}: {node} is not marked @pytest.mark.edge")
    return problems


def _evidence_problems(value: str, read: Callable[[str], str | None]) -> list[str]:
    """Each named test must exist in the committed code, by file and name."""
    return _node_problems("Evidence", value, read, edge=False)


def check_traceability(
    message: str,
    files: Sequence[str],
    read: Callable[[str], str | None],
    spec_is_fresh: Callable[[str], bool] | None = None,
) -> list[str]:
    """Product changes must name their rules, their proof and the README check.

    ``read`` returns a file's text as committed (None if absent). ``spec_is_fresh``
    is only available locally, because screen specs are internal and not tracked.
    """
    subject = next((ln for ln in message.splitlines() if ln.strip()), "")
    if subject.startswith(EXEMPT_SUBJECTS):
        return []
    if not any(f.startswith(PRODUCT_PATHS) for f in files):
        return []
    found = {key: value.strip() for key, value in TRAILER.findall(message)}
    problems: list[str] = []
    ids = [i for i in re.split(r"[,\s]+", found.get("Constraints", "")) if i]
    if not ids:
        problems.append("add 'Constraints: <ids>' naming the rules this change touches")
    problems += [
        f"Constraints: unknown id {i!r}" for i in ids if not CONSTRAINT_ID.match(i)
    ]
    # a rule id must be one the policy engine actually defines
    rule_ids = [i for i in ids if i.startswith("R") and i[1:].isdigit()]
    engine = read(ENGINE_MODULE)
    if rule_ids and engine is None:
        problems.append("Constraints: rule ids cannot be checked without the engine")
    elif engine is not None:
        defined = {f"R{n}" for n in RULE_ID.findall(engine)}
        problems += [
            f"Constraints: no such rule {i!r}" for i in rule_ids if i not in defined
        ]
    problems += _evidence_problems(found.get("Evidence", ""), read)
    if EDGE_REQUIRED.match(subject):
        edge = found.get("Edge cases", "")
        problems += _node_problems("Edge cases", edge, read, edge=True)
    if any(f.startswith(SCREEN_PATHS) for f in files):
        spec = found.get("Spec", "")
        if not (spec.startswith(SPEC_PREFIX) and spec.endswith(".md")):
            problems.append(f"screen change: add 'Spec: {SPEC_PREFIX}<file>.md'")
        elif spec_is_fresh is not None and not spec_is_fresh(spec):
            problems.append(
                f"Spec: {spec} is missing or not updated since the last commit"
            )
    readme = found.get("README", "")
    if not readme.startswith(README_VALUES):
        problems.append("add 'README: updated' or 'README: checked' (stale facts)")
    elif readme.startswith("updated") and "README.md" not in files:
        problems.append("README: updated, but README.md is not in this commit")
    return problems


def _local_spec_is_fresh(root: Path) -> Callable[[str], bool]:
    head = _git(root, "log", "-1", "--format=%ct")
    since = float(head[0]) if head else 0.0

    def fresh(rel: str) -> bool:
        path = root / rel
        return path.is_file() and path.stat().st_mtime > since

    return fresh


def run_message(path: Path, root: Path) -> int:
    """commit-msg hook entry: check the pending message against staged files."""
    staged = _git(root, "diff", "--cached", "--name-only", "--diff-filter=ACMRD")
    message = path.read_text(encoding="utf-8")

    def read(rel: str) -> str | None:
        lines = _git(root, "show", f":{rel}")
        return "\n".join(lines) if lines else None

    problems = check_message(message, staged) + check_traceability(
        message, staged, read, _local_spec_is_fresh(root)
    )
    for problem in problems:
        sys.stderr.write(f"commit-msg: {problem}\n")
    return 1 if problems else 0


def run_history(revs: Sequence[str], root: Path) -> int:
    """pre-push hook entry: check every commit in the range being pushed."""
    failed = 0
    for sha in _git(root, "rev-list", *revs):
        message = "\n".join(_git(root, "show", "-s", "--format=%B", sha))
        files = _git(root, "diff-tree", "--no-commit-id", "--name-only", "-r", sha)

        def read(rel: str, sha: str = sha) -> str | None:
            lines = _git(root, "show", f"{sha}:{rel}")
            return "\n".join(lines) if lines else None

        problems = check_message(message, files) + check_traceability(
            message, files, read
        )
        for problem in problems:
            sys.stderr.write(f"pre-push: commit {sha[:7]}: {problem}\n")
            failed = 1
    return failed


# ---------------------------------------------------------------------------- CLI


def run_check(root: Path) -> int:
    """Check the repository and print a summary; return the exit code."""
    report = Report()
    check_package(root, report)
    check_repo(root, report)
    if not report.violations:
        sys.stdout.write("guardrails: OK\n")
        return 0
    sys.stdout.write("guardrails: FAILED\n")
    for rule, places in sorted(report.violations.items()):
        sys.stdout.write(f"  {rule}: {len(places)}\n")
        for place in places[:10]:
            sys.stdout.write(f"    {place}\n")
    return 1


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point."""
    args = list(sys.argv[1:] if argv is None else argv)
    root = Path(__file__).resolve().parent.parent
    if "--root" in args:
        index = args.index("--root")
        root = Path(args[index + 1]).resolve()
        del args[index : index + 2]
    if args[:1] == ["message"] and len(args) == 2:
        return run_message(Path(args[1]), root)
    if args[:1] == ["history"] and len(args) > 1:
        return run_history(args[1:], root)
    if args:
        sys.stderr.write(__doc__ or "")
        return 2
    return run_check(root)


if __name__ == "__main__":
    sys.exit(main())
