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
| Live integration | `pytest -m integration` | PayPal sandbox / Groq / NIM keys | Auth handshakes, model availability against configured IDs |

## Run modes

See [ARCHITECTURE §5](ARCHITECTURE.md). The default is **hermetic**: LLM answers and PayPal execution come from recorded real-sandbox evidence while the policy engine, approval flow and audit ledger run as real code. To use your own PayPal sandbox, copy `.env.example` to `.env`, set the PayPal keys and `AGENTCOSIGN_PAYPAL_EXECUTION=sandbox`.

## Quality gates

`ruff check .` · `ruff format --check .` · `mypy agentcosign/` · `bandit -r agentcosign/` · `pytest --cov=agentcosign` · `gitleaks detect`. CI runs the same on Python 3.12 and 3.13.

## Methodology notes

- Injection attacks are **synthetic**; they are split into a development set (used for prompt tuning) and a held-out set (never used for tuning). Reports include the policy-only (screening disabled) result.
- Recorded evidence contains sandbox identifiers only and is sanitised before commit.
- Results are published in the README with the commit hash and date they were produced.
