# What our acceptance review found, and why it matters

*Plain-language record of the first quality and acceptance (UAT) review of AgentCoSign, October 10, 2026. Written for anyone: no programming knowledge needed.*

## 1. The product in one picture

Think of AgentCoSign as a bank teller with a strict supervisor:

- The **AI assistant** is the teller. It reads a customer's message and asks to do something, such as "refund this customer $40".
- The **rules** are the bank's policy book. Small, clean refunds go through automatically. Bigger or unusual ones need a supervisor. Some are never allowed.
- **You, the merchant**, are the supervisor. You approve the bigger requests with your fingerprint (a passkey) on your phone.
- **PayPal** is the vault where the money actually moves.
- A **tamper-evident diary** records every step, so anyone can later prove what happened and that nobody changed the record.

The promise of the product is one sentence: *the AI can never move money without the rules agreeing and, when needed, a person approving.*

## 2. What the review was

A quality and acceptance review is a professional sceptic trying every trick to break that promise: repeating requests, cutting the network at the worst moment, changing the amount after approval, sending confusing instructions, editing records. It checks the product the way a real merchant, a careless user and a dishonest one would use it, not just whether each piece of code runs.

None of the problems below ever caused harm: the product was not yet connected to the internet, and only PayPal's test environment (the sandbox) is used, never real money. But each one would have become a live risk the moment the first screen was connected.

## 3. The problems found and how they were fixed

| # | The problem, in simple words | What could have happened | What we changed |
|---|---|---|---|
| 1 | **The cashier paid whoever handed in a slip.** The payment step did not check whether the supervisor had approved the request. | A refund still waiting for approval could be paid out. A request approved at $10 could be paid at $90. | Before any payment, the system checks its own records: is this exact request, with this exact amount, order and recipient, approved? If not, no money moves. |
| 2 | **A lost receipt caused confusion.** PayPal paid a refund, but its reply was lost on the network. On retry, the system saw money already refunded, assumed someone else had changed the order, and asked the supervisor again. | PayPal showed $20 refunded while our records said "expired". The merchant would be asked to approve money already gone, and the daily limit would undercount. | A retry now uses the same request number. PayPal recognises it and returns the original result instead of paying twice, so the record ends as "paid once". |
| 3 | **Our request number was too long for PayPal.** PayPal accepts at most 38 characters for the number that prevents double payments; we allowed 128. A hidden line break could also corrupt the message sent to PayPal. | A request could pass our checks and then fail or behave unpredictably at PayPal. | The request number is now limited to 38 safe characters, checked in three separate places. |
| 4 | **Two kinds of give-backs were not added together.** The rule "never give back more than the customer paid" counted refunds but not dispute settlements. | On a $100 order, a $60 refund and a $60 dispute settlement could both be approved: $120 returned on a $100 sale. | Refunds and dispute settlements now share one limit, including ones still waiting for approval. |
| 5 | **Customers paying with a PayPal account were invisible** to the "too many refunds for one customer" rule, which only recognised card payers. | Six refunds in a row to the same customer, all approved automatically. | Those customers are now recognised by their PayPal payer id, stored only in scrambled form. |
| 6 | **A dispute with missing details was read as "no dispute".** | A refund on a disputed order could skip the supervisor, risking paying twice: once as a refund and again as a chargeback. | Missing details now count as "there is a dispute". When information is missing, the system takes the cautious path. |
| 7 | **Part of the record could be quietly edited.** The diary of events was tamper-evident, but the summary table of decisions was not checked against it. | Someone with access to the database could change "denied" to "approved" in the summary. | A new check compares every summary entry with the tamper-evident diary and flags any difference. |

### How each fix was proven

For every problem the same four steps were followed:

1. Write an automated test that recreates the problem.
2. Run it and watch it fail, which proves the problem is real.
3. Fix the product.
4. Run it again and watch it pass.

Those tests now run automatically on every change, so a problem cannot quietly come back. After this review the product has more than 420 automated checks, all passing. The fixes are in the project history as `a898a4e` (problem 3) and `8443be1` (problems 1, 2 and 4 to 7).

## 4. What is not covered yet (said honestly)

- **The approval screen and fingerprint step are not built yet**, so "only the merchant can approve" cannot be tested yet. It is planned and will be tested when built.
- **Removing the most recent diary entries** cannot be detected from inside the database alone. The remedy is to publish the diary's latest fingerprint somewhere outside it, planned together with the audit viewer.
- **The hosted demo forgets its running totals when it restarts.** That is acceptable for a demo, but it means daily limits restart from zero after a restart.
- **Some business choices belong to the owner:** whether unknown payees are blocked or only need approval, whether waiting approvals are re-checked when limits change, and whether daily limits must survive a restart.

## 5. Why acceptance testing is part of building software

Software is usually built in stages: understand the need, design, build, test, release, operate. Testing happens at several levels:

| Level | Question it answers |
|---|---|
| Unit tests | Does each small piece do its job? |
| Integration tests | Do the pieces work together and with outside services such as PayPal? |
| **Acceptance testing (UAT)** | **Does the product do what the business and its users actually need, safely, in real situations?** |

Every piece of AgentCoSign "worked" on its own before this review. The problems lived in the gaps between pieces: what happens on a retry, between approval and payment, or when two rules overlap. That is exactly where acceptance testing looks, because that is where real users and real failures go.

## 6. Why it matters even more when money is involved

- **Mistakes cost real money and often cannot be undone.** A wrong refund or a double payment leaves the business. Getting it back means chasing customers, disputes and fees.
- **Trust is the product.** A merchant lets an AI near their money only if they believe the safeguards. One visible failure can end that trust.
- **Failures are normal, not rare.** Networks drop, replies get lost, requests get sent twice, people change their minds. A payment system must stay correct through all of it, so these situations are tested on purpose.
- **Money systems must be able to prove what happened.** Disputes, audits and customer complaints need a reliable record. Testing that the record cannot be quietly changed is as important as testing the payment itself.
- **Bugs are cheapest to fix early.** All seven problems above were found before any real screen existed. Found after launch, the same bugs would mean lost money, upset merchants and a damaged reputation.
- **Clear evidence supports good decisions.** A review like this tells the owner what is proven, what is still blocked, and which decisions are theirs, so "ready" or "not ready" is a decision based on evidence rather than hope.

## 7. In one sentence

Acceptance testing turned the product's main promise, *no money moves without the rules and a person agreeing*, from a claim into something checked automatically on every change, and it found seven ways that promise could have been broken before any of them could hurt anyone.
