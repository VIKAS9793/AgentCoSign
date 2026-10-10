# Decision Log (ADRs)

Format: context → decision → alternatives rejected → consequences. Newest decisions that changed v1.0 are marked **(v1.1)**.

## ADR-001 The LLM never makes the final allow/deny
**Context:** Probabilistic components can be steered by attacker-controlled text. **Decision:** Models propose, extract, screen and explain; a pure-function policy engine decides. **Rejected:** LLM-as-judge for approvals (non-deterministic, un-auditable). **Consequence:** Less adaptive policy; far stronger audit story and testability (property tests are possible).

## ADR-002 Python + FastAPI gateway
**Context:** PayPal toolkit, Groq, NVIDIA NIM and test tooling are Python-first; one builder. **Decision:** Python 3.12+, FastAPI, Pydantic v2. **Rejected:** Node/TypeScript (more boilerplate, no ecosystem gain). **Consequence:** PayPal REST via `httpx` is the core client. The official `paypal-agent-toolkit` is an optional extra (it requires Python < 3.14 and pins `crewai-tools`, so it must never be required for hermetic mode, CI or deploy).

## ADR-003 Refunds first, disputes second, payouts conditional **(v1.0)**
**Context:** Toolkit has `create_refund` and `accept_dispute_claim` but no Payouts tool; Payouts activation applies to production, sandbox availability must be tested. **Decision:** P0 = refunds + dispute concession; payouts only if spike S4 passes. **Consequence:** A complete story survives an S4 failure.

## ADR-004 Two-plane resilience: decisions are always live, execution degrades **(v1.1)**
**Context:** Hosted demo must stay usable through Dec 15 on free tiers (Groq token caps, Render spin-down). **Decision:** Policy, evidence handling, approval and ledger always run real code; LLM and PayPal dependencies fall back to recorded real-sandbox evidence with a visible banner. **Rejected:** Mock server (would be a "non-functional demo"); live-only (dies on first 429). **Consequence:** Every mode is honest and labelled; recorded evidence must be sanitised and committed.

## ADR-005 Evidence-first trust boundary **(v1.1)**
**Context:** If the agent supplies captured amount or dispute state, R4/R5/R6 are bypassable. **Decision:** The gateway fetches facts from PayPal; agent values are claims compared against evidence (R14). Extraction is an independent second reading of the source text, used as a cross-check, never as the source of an authorising number. **Consequence:** An extra PayPal read per proposal; outages degrade to deny/escalate.

## ADR-006 Safety rules are invariants, not knobs; money is Decimal; PayPal pinned to sandbox **(v1.1)**
**Context:** v1.0 config allowed `fail_closed_on_error: false`, free-string enums and float money; a missing config file silently loaded defaults. **Decision:** R4, R6, R7, R11–R14 have no setting; config rejects unknown keys and nonsensical limits; missing config aborts startup; `paypal_mode` is `Literal["sandbox"]`; defaults are hermetic and loopback-bound. **Consequence:** Fewer ways to weaken safety by accident; one more place to change if production is ever targeted (deliberate).

## ADR-007 Public policy thresholds
**Context:** Hiding the $25 limit buys little. **Decision:** `/api/v1/config/policy` is public; safety relies on velocity, rolling daily cap and escalation. **Consequence:** Structuring is possible but bounded (T3).

## ADR-008 Mobile PWA approver with WebAuthn passkey co-sign; native Android is a stretch **(v1.1)**
**Context:** Reviewers cannot install an APK; native build is the largest solo schedule risk; the co-sign should be cryptographic. **Decision:** Installable PWA served by the gateway; approval is a WebAuthn assertion whose challenge is bound to the action hash; `session` assurance exists as a labelled fallback. **Rejected:** Native-only (untestable by judges); tap-to-approve only (weak assurance). **Consequence:** Needs HTTPS/localhost and a platform authenticator (validated in spike S8 on the video phone).

## ADR-009 Ledger: hash chain + HMAC held outside the DB + approver signature **(v1.1)**
**Context:** A plain hash chain can be fully recomputed by anyone with DB write access. **Decision:** Each entry carries an HMAC (key from env/KMS) and the approver's assertion; the signed head hash is published to the viewer. **Consequence:** Claim is precise: tamper-evident against edits and rewrites without the key; not tamper-proof.

## ADR-010 SQLite plus committed recorded evidence; no Postgres **(v1.1)**
**Context:** Render free Postgres expires after 30 days; free web-service disk is ephemeral. **Decision:** SQLite (WAL, single writer) re-seeded at boot; real sandbox runs recorded to `docs/evidence/`. **Consequence:** Hosted ledger resets on restart (documented); evidence persists in git.

## ADR-011 Screening failure escalates, never allows **(v1.1)**
**Context:** Free-tier 429s are likely. **Decision:** R15: unavailable/garbled screening → ESCALATE (config may choose DENY, never ALLOW). **Consequence:** Slightly higher escalation rate during outages; demo stays alive.

## ADR-012 Statistics reported with intervals on a held-out set **(v1.1)**
**Context:** "≥90% on 20 documents" has a wide interval and invites overfitting. **Decision:** 20 dev + 20 held-out attacks + 20 benign; report Wilson 95% CIs and the policy-only backstop number. **Consequence:** Smaller, defensible headline.

## ADR-013 Frozen `release` branch/tag during judging **(v1.1)**
**Context:** Judging runs Dec 1–15 and testing access must stay free and unrestricted. **Decision:** Deploy from a tagged `release` branch with auto-deploy off; only break-glass fixes thereafter. **Consequence:** `main` can keep evolving without risking the demo.

## ADR-014 Minimal, requirement-driven PayPal API surface; REST client over the toolkit **(v1.1)**
**Context:** Every extra API is code to build, secure and keep working through Dec 15. **Decision:** Adopt only OAuth, Orders (read/create/capture), Payments (capture, refund, refund details), Customer Disputes (list/get/accept-claim), Webhooks (signature verification and reconciliation events) and, if S4 passes, Payouts. Use Transaction Search only for P2 daily reconciliation (official docs: up to 3 h lag, 31-day range, separate scope) and never for live decisions; reject Balances/summary endpoints for decisions (same lag, aggregate only); reject Invoicing/pay_order (bypass paths), Subscriptions, Catalog, Tracking, PayPal Login. Use a small `httpx` REST client; the official toolkit stays an optional adapter. **Why not the toolkit:** ~150 transitive dependencies for four calls, Python < 3.14, pinned `crewai-tools`. **Consequence:** Smaller attack surface and test matrix; we own request signing, retries and idempotency, which we need to control anyway (see ARCHITECTURE section 7b).

## ADR-015 Approval window is 10 minutes, capped in code **(v1.1)**
**Context:** An escalated request waits for a human; the longer it waits, the more the order, dispute state or daily budget can change (R14). Expiry is harmless because nothing is sent and the assistant can ask again. **Reference points (secondary sources, check before quoting):** Monzo's in-app card-payment approval window is 10 minutes; out-of-band one-time-code lifetimes are commonly capped at 10 minutes for SMS and phone delivery and 5 minutes for time-based devices (a research-and-education authentication profile; NIST SP 800-63B is the usual source, verify the current wording); PSD2 caps payer inactivity after strong authentication at 5 minutes (a different control, similar order of magnitude). **Decision:** 10 minutes (`approval.ttl_seconds: 600`), with a hard maximum of 600 in the settings model so configuration cannot extend it. **Alternatives:** 5 minutes (stricter, more expiries for a busy owner), 15 minutes (the earlier default; more stale decisions). **Consequence:** Expired requests are re-asked by the assistant; the approver UI states "Open until HH:MM" and "Nothing is sent if it closes".

## Deliberately not decided yet
Business model (open-core vs hosted control plane), multi-tenancy design, KMS provider, native Android timing. Tracked in the project's planning notes.
