# Android session tracker

Last updated: 2026-11-05 IST. Branch: `ccr-a3992ffa-nnxay1`. PR: #1 (draft).
Rules for all work here: `android/CONTRACT.md`. Update this file at the end of every session.

## Decisions made (owner)

| Date | Decision |
|---|---|
| 2026-11-04 | Build a native Android app from the nine Figma Make mockups, on sample data first |
| 2026-11-05 | Align the scaffold with current platform rules now, compile later on a local machine |
| 2026-11-05 | The owner runs the gateway (the demo website) as the middle layer between app and PayPal |
| 2026-11-05 | Gateway data moves to a free Supabase or Postgres database (not done yet, see below) |
| 2026-11-05 | One light theme only, no dark mode; colour tokens from the mockups, measured against WCAG AA |

## Built

| Area | State | Verified how |
|---|---|---|
| Project setup | AGP 9.5.0 built-in Kotlin, Gradle 9.6.0, compileSdk 37 / targetSdk 36, Kotlin 2.4.21, KSP 2.3.13, Hilt 2.60.1, BOM 2026.09.00, Nav3 1.2.0, Lifecycle 2.11.0 | Versions read from official release pages; **not compiled** |
| `:core:model` | Money (BigDecimal), requests, ledger, settings, `SeenFigures` | Compiles; tested through `:core:data` |
| `:core:domain` | Repository contracts; limits are read-only | Compiles |
| `:core:data` | Sample-data repositories with the gateway's refusals (stopped, time up, over paid, daily cap, decided twice, order changed) | **8 unit tests pass** |
| `:core:designsystem` | Light theme, measured WCAG tokens, tabular money figures, accessible toggle rows | Not compiled |
| `:feature:onboarding` | Welcome, Connect PayPal (stub), limits (read-only), Set up this phone (stub) | Not compiled |
| `:feature:home` | Dashboard tiles, Requests / Activity tabs, Stop all refunds with confirm | Not compiled |
| `:feature:review` | Request facts, Approve / Decline, sends `seen` figures | Not compiled |
| `:feature:settings` | Limits (read-only), suggesters, phones, alerts, PayPal, record, always-on rules | Not compiled |
| `:app` | Hilt app, Nav3 back stack with per-screen ViewModels, system auto-dark off | Not compiled |

## Remaining (in suggested order)

1. **First real build** on a machine with Android Studio. Follow the handoff steps below.
2. **Gateway: accept the app's passkey origin.** `agentcosign/services/passkeys.py` checks one web origin; it must accept a list including `android:apk-key-hash:<debug, upload and Play signing cert hashes>`, and the gateway must serve `/.well-known/assetlinks.json`. Without this the app can never leave sample data.
3. **App: gateway client.** HTTPS client with `x-approver-session`, status-code mapping (401/403/409/410/429), SSE `changed` / `signed_out` handling. Replace the sample repositories behind the same interfaces.
4. **App: Credential Manager** for enrolment, sign-in, decisions and stop/resume.
5. **Gateway: move storage to Supabase/Postgres** (see next section).
6. Move screen text into `strings.xml`; add `FLAG_SECURE` on review and sign-in.
7. Tests: ViewModel, Compose UI, screenshot (AGP test suites), mutation (PIT on pure Kotlin), UAT on a real phone (contract section 11).
8. Baseline Profiles; Play listing, Financial features declaration and Data safety form (contract section 12).
9. CI job for `android/` (touches `.github/workflows/`, needs the owner's sign-off tag).

## Database configuration (to do from a local session)

The gateway, not the app, owns the database. The app never receives a database URL or key.

Current state: SQLite files on Render's free disk (`settings.sqlite_db_path`, one file per
demo workspace). Render's free disk is wiped on restart, so data does not survive.

Steps when ready:
1. Create a free Supabase project (or any Postgres). Copy the **connection string**
   (use the pooled connection if offered). Check the free tier's current limits first,
   especially pausing after inactivity: they were not verified in the cloud session.
2. Put it in the gateway's environment only, for example `AGENTCOSIGN_DATABASE_URL`
   in Render's environment settings and in a local `.env`. Never commit it. Never put
   it in the Android app.
3. Gateway code change (its own PR, with tests): add a Postgres path next to SQLite in
   `agentcosign/services/ledger.py`, `passkeys.py`, `messages.py`, `gateway.py` and
   `agentcosign/wiring.py`; keep the audit hash chain and HMAC behaviour identical;
   keep SQLite for the hermetic test suite; decide how demo workspaces map to Postgres
   (schema per workspace, or a workspace column).
4. Commit rules for that PR: it touches `agentcosign/`, so the commit message needs
   `Constraints:`, `Evidence:` and `README:` lines (see `tools/hooks/commit-msg`);
   a change to `render.yaml` or `.env.example` needs the human sign-off tag.
5. Verify: restart the gateway and confirm requests, decisions and the audit record
   are still there; run the tamper-detection evidence again.

## Open questions

- Is the gateway also meant to serve real merchants later, or only the demo? This decides multi-tenant design.
- Store listing wording (must describe an approval control, not payments). See contract section 12.

## Local setup reminder

- Open `android/` (not the repo root) in Android Studio. Install SDK Platform 37 and Build Tools 36.
- Create an untracked `android/CLAUDE.md` containing one line: `@CONTRACT.md`.
- Before committing: `python tools/guardrails.py` from the repo root must print `guardrails: OK`.
  Commit messages are Conventional Commits with no AI attribution lines.
