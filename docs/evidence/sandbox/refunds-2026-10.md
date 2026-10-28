# Live PayPal sandbox refunds, read back from PayPal

Refunds sent by AgentCoSign to the PayPal sandbox during acceptance runs on Oct 26 and 27, 2026, each read back from PayPal on Oct 28 with `GET /v2/payments/refunds/{id}` (read-only). The status, amount and time below are PayPal's own answer, not our record. Sandbox only: no real money.

| Day | Refund id (PayPal) | How it was sent | Where | PayPal status | Amount | Created at PayPal |
|---|---|---|---|---|---|---|
| 2026-10-26 | `11G57376RY4368245` | sent on its own | laptop, local server | COMPLETED | 9.00 USD | 2026-10-26T07:05:29-07:00 |
| 2026-10-26 | `04X513008L913373R` | after the owner's PIN | laptop, local server | COMPLETED | 40.00 USD | 2026-10-26T07:09:17-07:00 |
| 2026-10-26 | `13H65438V5017335V` | sent on its own | laptop, local server | COMPLETED | 9.00 USD | 2026-10-26T07:25:05-07:00 |
| 2026-10-26 | `1LB797780F3622230` | after the owner's PIN | laptop, local server | COMPLETED | 40.00 USD | 2026-10-26T07:27:28-07:00 |
| 2026-10-27 | `509243129D3920541` | sent on its own | phone, local server | COMPLETED | 9.00 USD | 2026-10-27T07:30:44-07:00 |
| 2026-10-27 | `2RR7029644429264V` | after the owner's fingerprint | phone, local server | COMPLETED | 40.00 USD | 2026-10-27T07:40:51-07:00 |
| 2026-10-27 | `6YB708398H1220608` | sent on its own | laptop, hosted demo | COMPLETED | 9.00 USD | 2026-10-27T08:36:00-07:00 |

**7 of 7** confirmed COMPLETED by PayPal. Every refund sent on its own was within the $25 automatic limit; every $40.00 refund went out only after the merchant signed that exact refund with a passkey (PIN on the laptop, fingerprint on the phone).

## How to check it yourself

- Each id appears in the app's outcome screen as the PayPal reference, and in the behind-the-scenes panel on the `POST /v2/payments/captures/{id}/refund` step (`paypal_id`, `paypal_status`).
- With the sandbox app's credentials, `GET https://api-m.sandbox.paypal.com/v2/payments/refunds/{id}` returns the same status and amount.
- In the PayPal developer dashboard, the sandbox business account's activity lists the same refunds.

## Notes

- One id was first copied by hand from a phone screenshot with a digit missing, and PayPal answered 404; the id kept in our own record of that run was correct and PayPal confirmed it. Ids in this file come from the record or from PayPal, not from screenshots.
- The release gate asks for at least 10 recorded sandbox actions; this file has 7 refunds (each also created and captured a sandbox order first). The hosted walkthrough adds the rest.
