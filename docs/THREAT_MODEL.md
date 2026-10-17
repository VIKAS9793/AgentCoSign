# AgentCoSign Threat Model

**Version 1.1** · Scope: gateway, approver PWA, ledger, LLM layer, PayPal sandbox integration · Method: asset/actor/abuse-case analysis with STRIDE-style coverage and an LLM-specific section.

## 1. Assets

| Asset | Why it matters |
|---|---|
| Funds reachable through PayPal tools | Direct financial loss |
| Policy integrity (limits, invariants) | Weakening a rule silently = latent loss |
| Audit ledger | Evidence for disputes, auditors, regulators |
| PayPal credentials, gateway keys, approver passkeys | Compromise bypasses every control |
| Approver attention | Fatigue turns a control into a rubber stamp |

## 2. Actors

| Actor | Capability |
|---|---|
| Malicious buyer / vendor | Authors text the agent will read (emails, invoices) |
| Compromised or confused agent | Arbitrary tool arguments, retry storms |
| Insider with DB access | Edits or rewrites ledger |
| Network attacker | Replays, forges webhooks, probes endpoints |
| Hosted-demo abuser | Hammers public endpoints, burns free-tier quotas |

## 3. Trust assumptions

PayPal's API responses are authoritative for payment facts. The approver's device is not compromised. The host OS and Python runtime are trusted. Sandbox only: no real funds, no real PII.

## 4. Abuse cases and controls

Controls describe the **target design**. [ARCHITECTURE section 9](ARCHITECTURE.md) states what is built today; controls for components not yet built (policy engine, evidence fetcher, ledger, approver app, rate limits) are commitments, not claims of current behaviour.

| ID | Abuse case | Impact | Controls | Residual risk |
|---|---|---|---|---|
| T1 | **Prompt injection** in a refund email or invoice ("refund $4,900 to…") | Unauthorised large refund | L0 deterministic pre-screen; L1 safeguard model; **L2 structural policy**: amount caps, escalation, daily cap apply regardless of what the model was tricked into | Injection can still cause auto refunds ≤ auto limit within velocity/daily caps (bounded loss) |
| T2 | **Agent lies about facts** (captured amount, dispute status, customer) | Bypasses R4/R5/R6 | Evidence Fetcher reads from PayPal; mismatch → R14 DENY | PayPal API outage → fall back to deny/escalate, not allow |
| T3 | **Structuring** (many $24.99 refunds) | Drains up to cap | R17 automatic budget: refunds sent without asking stay within the auto limit per order and per customer in 24 h, so the second small refund asks the owner; R5 velocity; R10 rolling daily cap | One person under several customer keys (cardholder names) can still get the auto limit per key, bounded by the daily cap; threshold is public by design (ADR-007) |
| T4 | **Refund stacking** across orders/time | Exceeds captured amount | R4 cumulative incl. reservations | None identified |
| T5 | **Double execution** (retry, race, replay) | Duplicate refund | Idempotency key = PayPal-Request-Id + unique `request_id`; atomic reserve; GET-before-retry | PayPal retains idempotency keys for a limited window (about 6 h per Orders docs; verify for refunds in S2), so the local ledger is authoritative beyond it and the executor does GET-before-retry |
| T6 | **TOCTOU**: state changes between escalation and approval | Approving stale facts | Every approval re-runs all rules on freshly read PayPal facts in the same transaction as the stop and deadline checks; a changed order goes back to the owner; re-compare right before the PayPal call (R14); open window capped at 24 h in code; reservation expiry | Narrow window between the last re-read and the call |
| T7 | **Maker approves own request** | Defeats dual control | Separate principals/credentials; agent key cannot reach approval routes; approver cannot propose | Single human holding both roles in a demo is documented |
| T8 | **Social engineering the approver** via poisoned AI summary | Human rubber-stamps | Approver UI shows deterministic facts first (amount, order, rule hit); AI text labelled advisory, rendered as text only | Fatigue; mitigated by escalation-rate KPI and tiered thresholds |
| T9 | **Ledger tampering** | Evidence destroyed or forged | Hash chain detects row edits; **HMAC key outside DB** detects full rewrite; periodic signed head hash; approver passkey signature per approval | Attacker with both DB and HMAC key; production path uses KMS |
| T10 | **Forged PayPal webhook** | False outcome linked to ledger | `verify-webhook-signature` against PayPal; reject on failure | — |
| T11 | **Credential theft** (keys in repo/logs) | Full bypass | Secrets only in env; `SecretStr`; gitleaks in CI and pre-commit; log redaction; PayPal pinned to sandbox | Developer workstation compromise |
| T12 | **Unknown/new tool call** | Unreviewed money-out path | Explicit allowlist; toolkit actions disabled by default at the PayPal layer too; unknown → R12 DENY | — |
| T13 | **Currency/account confusion** | Wrong-currency or cross-account refund | R13 | — |
| T14 | **XSS via model output** | Session theft in approver UI | CSP (no inline scripts), text-only rendering, no HTML from models | — |
| T15 | **Public demo abuse / quota exhaustion** | Demo dies during judging | Scripted scenarios + constrained form, per-IP rate limits, replay cache, request size caps, `release` branch freeze | Judges' legitimate bursts |
| T16 | **Supply chain** (typosquat, vulnerable dep) | Code execution | Pinned ranges, `pip-audit`, minimal deps, CI | Transitive zero-days |
| T17 | **Config tampering / typos** | Silent weaker limits | `extra=forbid`, ordering validators, missing file aborts startup, invariants not configurable | — |
| T18 | **Real-money exposure by misconfiguration** | Real loss | `paypal_mode` type is `Literal["sandbox"]`; production cannot be selected | Code change required to enable, by design |

## 5. LLM-specific threats (mapped by name to the OWASP LLM risk categories)

| Category | Where it applies | Mitigation in this design |
|---|---|---|
| Prompt injection | Agent input, screening input, extraction input | Layered defence; untrusted text delimited; models without tools; structural policy backstop |
| Insecure output handling | Summaries, extraction JSON | Schema validation, text-only render, never executed |
| **Excessive agency** | The agent's money tools | **This is the product**: allowlist, caps, co-sign, no credentials in the agent |
| Sensitive information disclosure | Prompts and logs | No secrets in prompts; redacted logs; sandbox data only |
| Unbounded consumption | Free-tier quotas, large documents | `max_document_chars`, timeouts, rate limits, replay cache |
| Model supply chain | Preview models disappear | Config-driven IDs, fallback chain, startup probe fails only when the whole chain is unavailable |

## 6. Measured claims (how we avoid overclaiming)

- **Layered injection result** is reported on a **held-out** attack set with a 95% Wilson interval, plus the policy-only (screening off) number to show the backstop.
- **Bounded loss** is proved by a property test, not asserted.
- **Ledger** claim is "tamper-evident against row edits, and against full-chain rewrite when the HMAC key is outside the database". It is not tamper-proof.

## 7. Out of scope / residual (honest)

No real funds; no PCI card data handling (refund by order ID); OFAC/AML screening is a documented hook; no multi-tenant isolation; approver device compromise; PayPal-side vulnerabilities; availability guarantees beyond the degradation ladder.
