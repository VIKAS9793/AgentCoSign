# Decision speed (measured Oct 28, 2026, commit cc575fa)

Run it yourself, offline and with no keys: `python -m tests.benchmarks.decision_latency` (raw output: `decision-latency-2026-10-28.json`). Both numbers exclude the AI checks and the person, as the target defines.

| What was timed (226 decisions each: the 113-row scenario table, twice) | p50 | p95 | max | Target p95 |
|---|---|---|---|---|
| The rules alone (pure policy engine) | 0.019 ms | **0.033 ms** | 0.24 ms | under 300 ms |
| The full decision step as the server runs it: read the refunds so far from the record, evaluate, write the sealed entry to SQLite | 9.7 ms | **14.4 ms** | 29 ms | under 300 ms |

For the rules alone every one of the 226 answers matched the table, so the speed is for correct answers. Machine: Windows laptop, Python 3.12.10. The hosted demo runs on a smaller free instance; its behind-the-scenes panel shows the rules step's time for every run. End-to-end time for a test run (PayPal calls and AI checks included) is also shown per run there: about 9 to 25 seconds in our live runs, almost all of it PayPal sandbox and model calls.
