# Tamper detection of the signed record (run Oct 28, 2026, commit cc575fa)

Every decision, approval and PayPal answer is written to a record where each entry carries the SHA-256 hash of the one before and an HMAC sealed with a key kept outside the database. These tests change a real record on disk and check that the change is caught. Run them: `pytest tests/unit/test_ledger.py -k "chain or row or head"`.

| Change made to a saved record | Caught | Test |
|---|---|---|
| None (control) | the record verifies | `test_untouched_chain_verifies` |
| One field of one row edited | yes | `test_editing_one_row_is_detected` |
| One row deleted | yes | `test_deleting_a_row_is_detected` |
| The whole chain rewritten with fresh hashes, without the key | yes | `test_rewriting_the_whole_chain_without_the_key_is_detected` |
| Checked with a different key | refused | `test_a_different_key_cannot_verify_the_chain` |
| The whole chain rewritten **with** the key | yes, against a record head published earlier | `test_a_full_rewrite_with_the_key_is_caught_by_a_published_head` |
| Chain cut shorter than a published head | yes | `test_a_chain_shorter_than_the_published_head_is_caught` |

All 7 passed. The hosted demo publishes its current head at `/api/v1/audit/head`; anyone who saves it can later check that no earlier entry was changed (`python -m agentcosign.cli verify-head`).
