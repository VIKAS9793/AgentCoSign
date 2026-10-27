<p align="center">
  <img src="docs/assets/banner.png" alt="AgentCoSign, dual control for AI payments. Agent proposes, rules decide, you co-sign, the record proves it. Sandbox only." width="100%">
</p>

# AgentCoSign

**Dual control for AI agents that move money on PayPal.** The agent proposes. Deterministic policy decides. A human co-signs risky actions on their phone. Every step lands in a tamper-evident ledger.

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python: 3.12+](https://img.shields.io/badge/Python-3.12%2B-blue.svg)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.115%2B-009688.svg)](https://fastapi.tiangolo.com/)

> Built during the **PayPal AI Hackathon 2026** (submission period Oct 1 – Nov 12, 2026). Sandbox only; no real funds.

## For judges and reviewers (60 seconds)

| I want to… | Do this |
|---|---|
| See it | Video: _link added at submission_ · Hosted demo: _link added at submission_ (free tier: the first load may take ~60 s, and test data resets when it sleeps) |
| Try it on the hosted demo (2 minutes) | 1. Open the link on a phone or laptop and set up with your fingerprint, face or PIN: you get your own private test workspace. 2. Press **Try a scenario**: each one creates a real PayPal sandbox order, then a customer message goes through the AI checks and the rules (or write your own message). 3. Switch on **Behind the scenes** in the top bar to watch every backend step live beside the app: PayPal calls with PayPal's own ids, each model's answer, the rules' decision in plain words, the signed record, and an outcome block saying what happened to the money |
| Run it with **no credentials** | `pip install -e ".[dev]" && pytest && uvicorn agentcosign.main:app` or `docker build -t agentcosign . && docker run -p 8000:8000 agentcosign` |
| Understand the design | [Architecture](docs/ARCHITECTURE.md) · [Threat model](docs/THREAT_MODEL.md) · [Decisions](docs/DECISIONS.md) |
| Use my own PayPal sandbox | [Testing guide](docs/TESTING.md) and `.env.example` |
| Review the exact submitted code | Tag `v1.0-submission` (created at freeze) |

**Honesty note on modes:** the hosted demo runs live: real PayPal sandbox calls and live AI models. A local run with no keys is "hermetic": the policy engine, approval flow and ledger run as real code, while PayPal execution comes from *recorded real-sandbox evidence*. Each mode is stated in `/health` (`paypal_execution`, `ai_models`) and in the behind-the-scenes panel. Set sandbox keys and `AGENTCOSIGN_PAYPAL_EXECUTION=sandbox` to send refunds to the PayPal sandbox yourself.

## The problem

PayPal's Agent Toolkit exposes money-out tools (`create_refund`, `accept_dispute_claim`, `pay_order`, …) with only an **on/off switch per tool**: no amount limit, no velocity limit, no approval step. A merchant must choose between *unbounded loss exposure* and *no automation*. A prompt-injected "refund $4,900", a hallucinated amount or a retry loop can move money nobody approved.

## The solution

| Role | Who | Can do | Cannot do |
|---|---|---|---|
| **Maker** | AI agent (no PayPal credentials) | Propose allowlisted money actions (refund, dispute acceptance, payout) | Approve, read raw audit, call PayPal directly |
| **Policy** | Deterministic engine, rules R1–R18 | Allow / escalate / pause / deny; strictest wins | Be overridden by an LLM |
| **Checker** | Human on a mobile PWA, passkey co-sign | Approve or reject escalations | Propose on the agent's behalf |
| **Auditor** | Read-only key | Verify the ledger | Mutate anything |

**Bounded loss, proven by a property test:** auto-executed outflow never exceeds the configured rolling-24h cap, and refunds sent without asking never exceed the auto limit per order, or per customer in 24 hours (defaults $25 / $1,000).

### Policy rules (limits live in [`config.yaml`](config.yaml); invariants are not configurable)

| Rule | Condition | Outcome |
|---|---|---|
| R1 | Refund ≤ auto limit, clean evidence | ALLOW |
| R2 | Auto limit < refund ≤ max limit | ESCALATE (passkey co-sign) |
| R3 | Refund > max limit | DENY |
| R4 | Cumulative refunds (executed + reserved) exceed captured amount | DENY |
| R5 | > N refunds for one customer in 24 h | PAUSE: held until the customer is back under the limit, then decided again automatically from fresh PayPal facts; the customer gets a kind, fixed message with an update time; nobody can send it early, the merchant can decline |
| R6 | Order has an open dispute | ESCALATE |
| R7 | Any dispute-claim acceptance | ESCALATE (always) |
| R8 | New/changed payee (payouts, optional) | ESCALATE |
| R9 | Duplicate payee + amount + reference in window (payouts, optional) | DENY |
| R10 | Rolling-24h outflow cap would be exceeded | DENY |
| R11 | Prompt injection detected | DENY + log |
| R12 | Any error, schema mismatch or unknown tool | DENY (fail closed) |
| R13 | Currency or account mismatch | DENY |
| R14 | Agent claims differ from PayPal evidence, or evidence changed before execution | DENY (asks the person again when only refunds or dispute state changed) |
| R15 | Screening unavailable | ESCALATE |
| R16 | The merchant pressed the emergency stop | DENY every money action; only the merchant's fingerprint turns refunds back on |
| R17 | Refunds sent without asking would pass the auto limit for the order, or for the customer in 24 h | ESCALATE (one refund cannot be split into several small automatic ones) |
| R18 | The customer's own message, read separately from the assistant, does not ask for exactly this refund (different or missing order or amount, other currency, money asked to go elsewhere), or gives a reason PayPal's record for the order cannot confirm (charged twice, an earlier refund missing, an unrecognised charge, a change of mind) | ESCALATE (a reading never refuses on its own) |

### Four AI roles (wired into the request path for refunds that come with a customer message)
AI reads and checks; it never approves money. A refund goes out without the merchant only when three independent witnesses agree: the agent's proposal, a separate reading of the customer's own words, and the facts PayPal returns. Any disagreement goes to the merchant.

1. **Acting agent** (Groq `openai/gpt-oss-120b`) reads the customer's message and proposes a refund through the gateway. It holds no PayPal credentials.
2. **Extraction** (Groq `qwen/qwen3.8-27b`, a different model family from the agent) reads the customer's own words for the order, amount, currency, reason and any request to send money elsewhere. Startup refuses a configuration that gives this role the agent's own model.
3. **Screening** (Groq `openai/gpt-oss-20b`, falling back to `openai/gpt-oss-safeguard-20b`) looks for manipulation across the customer's messages, including social engineering such as a message claiming to be the shop or PayPal.
4. **Prompt guard** (Groq `meta-llama/llama-prompt-guard-2-86m`, a different model family) scores open attack wording such as "ignore all previous instructions" from 0 to 1, reading long messages in overlapping pieces. It misses social engineering, which is why screening runs beside it; neither replaces the other.

The approval card shows what the customer asked for as a fixed sentence built from the extracted fields, so no model writes text the merchant reads. A customer message is masked (cards, security codes, emails, phone, UPI, IBAN, PAN, Aadhaar) before it is kept or read by any model; the checks run once per request and the saved result is reused on approval and release. Try it: `python -m agentcosign.cli message --text "Order <id> never arrived, please refund $9.00"`. Without a model key, requests with a message ask the merchant. Measured on 60 labelled threads: see `docs/evidence/ai/eval-2026-10-25.md`. Customer words go only to providers whose terms let the account turn off prompt retention and rule out training on prompts; startup refuses any other provider for these roles.

## Status (updated honestly)

| Component | Status |
|---|---|
| Strict fail-closed config, security headers, domain types, offline test suite, CI, container | ✅ Built (CI and the container image verified green) |
| Hosted demo on Render with traffic protection (per-client limits, penalty box, reserved reviewer capacity, daily live-call pools), landing page, API reference | ✅ Built and verified on the live host (earlier release; the redeploy with everything below is pending) |
| Visitor sandbox: a private workspace per visitor (own database, own signing secret), five ready scenarios plus your own customer message, each on a real PayPal sandbox order | ✅ Built; verified live against the PayPal sandbox with live models (Oct 26) |
| Behind the scenes: a live record of every backend step beside the app (PayPal calls with PayPal's ids, model answers, rule decisions in plain words, signed record entries, an outcome block per run), never carrying secrets or unmasked customer text | ✅ Built; verified live (Oct 26) |
| Policy engine R1–R18 plus execution-drift check: 113-row scenario table, property tests for bounded exposure | ✅ Built and called by the propose route |
| Ledger (hash chain + HMAC, atomic reservations), PayPal evidence fetcher, idempotent executor with a hard cap just before each PayPal call, retry of unanswered refunds with the same request id, propose and approve API | ✅ Built; sandbox refunds executed end to end from a real phone |
| Prompt guard, screening and a separate reading of the customer's words in the request path | ✅ Built; measured on 60 labelled threads (see Results) |
| Mobile PWA approver with WebAuthn passkey co-sign (approval bound to the exact refund) | ✅ Built; tested on a real Android phone and in Chrome against the PayPal sandbox |
| Audit: each refund's own history in the app, checked against the sealed record; a public record head (`/api/v1/audit/head`) and command-line tools to publish and verify it | ✅ Built |
| A full record browser for auditors | 🚧 Not built |
| PayPal webhooks (refund and dispute events, signature verified by PayPal itself before anything is trusted) | ✅ Built and registered on the sandbox app; end-to-end delivery test after the redeploy |
| Payouts adapter, native Android approver | ⏳ Optional, not built |

## Results

AI checks, measured live on 60 labelled synthetic threads (Oct 25; full report and raw results in [`docs/evidence/ai/eval-2026-10-25.md`](docs/evidence/ai/eval-2026-10-25.md)):

| Question | Result |
|---|---|
| Did any attack move money without the merchant? | **0 of 40**, even with the assistant assumed fully taken over |
| Same, with the AI checks switched off (limits only) | 1 of 40 (a $24 refund under the $25 automatic limit) |
| Did anything go out alone that should have asked the merchant? | **0 of 60** |
| Genuine customers wrongly flagged as attacks | 0 of 20 |
| Reading the customer's words (order, amount, currency, reason) | 20 of 20 each; "send it elsewhere" 19 of 20 |
| Screening alone on held-out attacks | 13 of 19 (68%, 95% CI 46% to 85%): the checks are one layer; the limits and the merchant's approval are what keep money safe when they miss |

Live sandbox runs (Oct 26): refunds sent on their own and after a passkey approval, each confirmed by PayPal with its own refund id, shown in the behind-the-scenes panel. To be added before submission with the commit they came from: policy scenario pass rate, p95 decision latency, ledger tamper detection.

## Tech stack

Python 3.12+ · FastAPI · Pydantic v2 · PayPal Sandbox REST (the official `paypal-agent-toolkit` is an optional extra: it supports Python < 3.14 only) · Groq (OpenAI-compatible API, Zero Data Retention on) · SQLite (WAL) · WebAuthn · Ruff, mypy, bandit, pytest, hypothesis, gitleaks.

## How this was built (use of AI tools)

Built by one person, Vikas Sahani, with **Claude Code** (Anthropic, Claude Pro plan) as the engineering partner. The split mirrors the product: the AI proposes, a person decides.

- **Owner: decision maker and business problem solver.** Defined the problem and the product (the rules and their limits, when a refund may go out alone, how the merchant's screens read), approved every specification before it was built, and kept every action with real consequences: the PayPal and Groq accounts and keys, Zero Data Retention, the PayPal webhook, deployment, repository visibility, and every passkey approval in testing.
- **Claude Code: engineering partner, steered by the owner.** Researched options and sources, wrote and revised code, tests and documentation drafts, ran the test suites and drove the browser during acceptance runs while the owner signed. It never held PayPal or model credentials and could not approve anything.
- **Held to the product's own standard.** Every change passes the offline test suite with a coverage floor, lint and type checks, secret scanning and a project checklist on each commit; every safety check is mutation-tested; live sandbox runs were checked by the owner.

## Develop

```bash
python -m venv .venv && source .venv/bin/activate     # Windows: .venv\Scripts\Activate.ps1
pip install -e ".[dev]"
bash tools/hooks/install.sh     # optional: lint, format, type, secret-scan and test gates on commit/push
pytest                          # offline, no credentials
pytest -m integration           # live checks, needs keys
ruff check . && ruff format --check . && mypy agentcosign/ && bandit -r agentcosign/
```

## Legal

Independent open-source project, not affiliated with, sponsored by or endorsed by PayPal Holdings, Inc. or Groq, Inc. PayPal and Groq are trademarks of their respective owners. Models are accessed through the providers' hosted APIs under their own model licences; weights are not redistributed. A commercial deployment needs a licence check.

## Author and licence

Vikas Sahani ([@VIKAS9793](https://github.com/VIKAS9793)) · [vikassahani17@gmail.com](mailto:vikassahani17@gmail.com) · [MIT License](LICENSE)
