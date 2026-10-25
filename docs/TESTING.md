# Testing and Reproducibility

Everything below runs on a fresh clone with **no credentials**. Live checks are opt-in.

## Quick start (any machine)

```bash
git clone https://github.com/VIKAS9793/AgentCoSign.git && cd AgentCoSign
python -m venv .venv && source .venv/bin/activate     # Windows: .venv\Scripts\Activate.ps1
pip install -e ".[dev]"
pytest                                               # offline, hermetic, deterministic
uvicorn agentcosign.main:app --port 8000             # http://127.0.0.1:8000/docs
```

Or with containers (no Python needed):

```bash
docker build -t agentcosign . && docker run -p 8000:8000 agentcosign
```

## Test layers

| Layer | Command | Needs network/keys? | Purpose |
|---|---|---|---|
| Unit (policy scenarios planned) | `pytest` | No | Config, policy rules, ledger, API |
| Property (bounded loss, planned) | `pytest -k property` | No | Auto-executed outflow ≤ daily cap and ≤ auto limit for any proposal sequence |
| Injection benchmark (planned) | `python -m scripts.run_injection_eval` | Replay by default | Block rates per layer on dev and held-out sets, with 95% Wilson intervals |
| Latency (planned) | `pytest -m slow -k latency` | No | p95 policy decision time |
| Live integration | `pytest -m integration` | PayPal sandbox / Groq keys | Auth handshakes, model availability against configured IDs |

## Run modes

See [ARCHITECTURE §5](ARCHITECTURE.md). The default is **hermetic**: LLM answers and PayPal execution come from recorded real-sandbox evidence while the policy engine, approval flow and audit ledger run as real code. To use your own PayPal sandbox, copy `.env.example` to `.env`, set the PayPal keys and `AGENTCOSIGN_PAYPAL_EXECUTION=sandbox`.

## Quality gates

`ruff check .` · `ruff format --check .` · `mypy agentcosign/ tools/guardrails.py` · `bandit -r agentcosign/` · `python tools/guardrails.py` · `pytest --cov=agentcosign --cov-fail-under=85` · `gitleaks detect`. CI runs the same on Python 3.12 and 3.13.

### Guardrails

`tools/guardrails.py` enforces the rules generic linters cannot express. It runs in the pre-commit and pre-push hooks and in CI, and each rule has a test that proves it still detects its violation.

| Area | What it rejects |
|---|---|
| Fail closed | Bare or blanket `except`, swallowed exceptions, an `ALLOW` outcome on an error path |
| Money | `float` annotations or conversions, `Decimal(float)` |
| Secrets | Secrets compared with `==`, logged, typed as plain `str`, or unwrapped outside the edge layers |
| Policy engine | Network, database, clock, randomness or environment access (it must stay a pure function) |
| Layering | Imports that cross the layer boundaries; HTTP libraries outside `clients/`; web framework outside `api/` |
| Network and data | HTTP calls without a timeout, disabled TLS verification, SQL built from strings, naive datetimes |
| PayPal surface | Production endpoints, money-moving paths that bypass the gateway, writes without an idempotency header |
| Web output | `innerHTML` and `eval`, inline handlers, external scripts, em dashes in UI copy |
| Repo and deploy | Secrets or databases tracked, unbounded dependency ranges, a Render blueprint that auto-deploys, a container that runs as root, real values in `.env.example`, unsanitised evidence |
| Comments | Citations of internal documents, rule or decision numbers, tickets, versions or project history |
| Commits | Non-Conventional-Commit subjects, AI attribution, deploy or hook changes without a human sign-off tag |

A reviewed exception is written on the line as `# guardrail: allow <rule> -- <reason>`; the reason is mandatory.

The pre-push hook also refuses force-pushes to and deletion of `main` and `release`, scans exactly the commits being pushed for secrets and re-checks their messages, so a commit made with `--no-verify` is still caught before it leaves the machine.

## Acceptance review

What the first quality and acceptance (UAT) review found, how each problem was fixed and proven, and why acceptance testing matters for money-moving software, in plain language: [UAT-FINDINGS](UAT-FINDINGS.md).

## Methodology notes

- Injection attacks are **synthetic**; they are split into a development set (used for prompt tuning) and a held-out set (never used for tuning). Reports include the policy-only (screening disabled) result.
- Recorded evidence contains sandbox identifiers only and is sanitised before commit.
- Results are published in the README with the commit hash and date they were produced.
