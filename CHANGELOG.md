# Changelog

## 1.0.0 (2026-10-31)

Initial public source release of AgentCoSign, a hackathon concept and learning project: dual control for AI that moves money on PayPal. PayPal sandbox only, no real funds. Not production software, with no independent security review.

### What is in it

- **Policy engine:** 18 deterministic rules with no model inside; the strictest outcome wins.
- **Evidence from PayPal:** order, captures, refunds and disputes are read from PayPal, never taken from the assistant.
- **AI checks:** masking, prompt guard, safety screening and a separate reading of the customer's words. Models propose and check; they never approve.
- **Merchant co-signature:** a phone app with WebAuthn passkeys, each signature bound to one exact refund.
- **Signed record:** SHA-256 chain with HMAC, a published record head and tamper checks.
- **Idempotent execution:** PayPal-Request-Id on every write, budget reserved atomically, PayPal read again before sending.
- **Emergency stop, reconciliation and verified PayPal webhooks.**
- **Hosted demo:** a private workspace per visitor, ready scenarios, your own customer message, and a behind-the-scenes panel of every step.

### Measured

- 1,348 offline tests, 95.9% coverage; 11 live checks against the PayPal sandbox and live models.
- On 60 synthetic threads written by the author: 0 of 40 attacks moved money without the merchant; screening alone caught 13 of 19 held-out attacks (68%, 95% CI 46% to 85%).
- 15 sandbox refunds read back from PayPal as COMPLETED.
- Twelve attack styles typed live on a real phone: results published in `docs/evidence/attacks/`, including a defect they found.
- Decision p95: 0.033 ms for the rules, 14.4 ms with the signed record write.

### Known limits

- A believable request with a plain reason under the automatic limit goes out on its own. The AI checks miss things; the limit caps the cost, it does not remove the risk.
- Sandbox only. Real merchant accounts need PayPal partner onboarding and production review.
- The impact case is reasoned, not yet validated with merchants.
- One person using several cardholder names can get the automatic limit per name, bounded by the daily cap.
- Visitor workspaces are demo isolation, not a production multi-tenant design, and are cleared when the free host restarts.
- Payouts, a native Android app and a full record browser for auditors are not built.
