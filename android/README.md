# AgentCoSign for Android (work in progress)

Last updated: 2026-11-05 IST

A native Android version of the approver app, built from the nine mobile mockups.
**Every screen runs on sample data.** Nothing in this app talks to the gateway yet,
and no passkey is created or used. Each screen shows a "Sample data" badge, and
controls with no gateway endpoint say so on screen.

## Build status

| Part | Verified | How |
|---|---|---|
| `:core:model`, `:core:domain`, `:core:data` (pure Kotlin) | Yes | Compiled with Kotlin 2.4.21, 8 unit tests pass |
| `:core:designsystem`, `:feature:*`, `:app` (Compose, Hilt, Nav3) | **No** | Never compiled. The session that wrote them had no Android SDK |

Toolchain, each version checked against its official release page on 2026-11-05:
AGP 9.5.0 with built-in Kotlin (no `kotlin-android` plugin), Gradle 9.6.0, JDK 17,
Kotlin 2.4.21, KSP 2.3.13, Hilt 2.60.1, Compose BOM 2026.09.00, Navigation 3 1.2.0,
Lifecycle 2.11.0, androidx.hilt 1.4.0, Activity 1.13.0, Core 1.19.1, Credentials 1.6.0.

- **compileSdk 37, targetSdk 36.** Compose 1.12 (in this BOM) requires compileSdk 37
  and AGP 9. Google Play requires targetSdk 36 since 2026-08-31.
- Expect compile fixes on the first real build. The likeliest spots: the package of
  `rememberViewModelStoreNavEntryDecorator`, assisted `hiltViewModel` in the new
  `androidx.hilt.lifecycle.viewmodel.compose` package, and `ColorScheme.copy`.

```bash
cd android
./gradlew :core:data:test        # pure Kotlin, no SDK needed
./gradlew :app:assembleDebug     # needs the Android SDK (API 37 platform)
```

## Modules

```
:app                 Hilt application, Nav3 back stack, sample-data DI bindings
:core:model          Money (BigDecimal, never floating point), requests, ledger, settings
:core:domain         Repository contracts, LimitsRule (auto <= approval <= daily)
:core:data           FakeApprovalsRepository, FakeSettingsRepository
:core:designsystem   Theme tokens from the mockups, shared rows, tiles, pills, buttons
:feature:onboarding  Welcome, Connect PayPal, Choose limits, Set up this phone
:feature:home        Dashboard, Requests and Activity tabs, Stop all refunds
:feature:review      One request, Approve or Decline
:feature:settings    Limits, suggesters, phones, alerts, PayPal, record, always-on rules
```

## Screens and the gateway

| Screen | Gateway endpoint | State in this app |
|---|---|---|
| Welcome | none needed | Done |
| Dashboard, tabs | `GET /v1/approvals/home`, `/stream`, `/done` | Sample data |
| Review, approve, decline | `POST /v1/approvals/{id}/options`, `/approve`, `/reject` | Sample data, unsigned |
| Set up this phone | `POST /v1/approver/enrolment/options`, `/enrolment` | Screen only |
| Stop all refunds | `POST /v1/controls/{intent}/options`, `/{intent}` | Sample data |
| Limits (onboarding, Settings) | none (limits live in the gateway's `config.yaml`) | Shown read-only; no editing on the phone |
| Staff, Add a person | none | Kept on the phone |
| Alerts | none | Kept on the phone |
| Connect PayPal | none | Moves the flow forward only |
| Download refund history | none (`/api/v1/audit/head` is the chain head only) | Not wired |

## Where the data lives

The hosted gateway (the demo website) sits between this app and PayPal, and the
project owner runs it. The app talks **only** to the gateway's HTTPS API. It never
talks to PayPal, and never to the database.

The plan is to store the gateway's data in a free Supabase or Postgres database.
Today the gateway stores everything in SQLite files on Render's free disk, which is
wiped on every restart (see the UAT findings). Moving to Postgres is a gateway change,
covering the audit ledger with its hash chain, passkeys, messages and workspaces, and
it gets its own pull request with its own tests. Database credentials live only in
the gateway's environment, never in the app.

## Blocker: native passkeys are refused by the gateway

Every `/v1/approvals/*` and `/v1/controls/*` call needs an approver session, and
a session needs a passkey assertion. The gateway checks each assertion against
**one** origin, the web `public_origin` (`agentcosign/services/passkeys.py`,
`expected_origin=self._origin`; wired in `agentcosign/wiring.py`).

A passkey used through Android Credential Manager reports its origin as
`android:apk-key-hash:<hash of the signing certificate>`, not the web origin.
So a native assertion fails verification and the app cannot open a session.
This means no gateway-backed screen can work natively until this is fixed.

What the fix needs, all outside this folder:

1. The gateway accepts a list of origins: the web origin plus the app's
   `android:apk-key-hash:` origin. The `webauthn` library already accepts a list
   for `expected_origin`.
2. The gateway serves `/.well-known/assetlinks.json` for the app's package and
   signing certificate, so Android lets the app use passkeys for that domain.
3. A real device test confirms a passkey made in the browser can also be used
   from the app (same Google Password Manager account). Not yet verified.

Until that lands, the app stays on sample data.

## Known gaps

- Screen text is still written in Kotlin, not in `strings.xml` resources. Moving
  it into per-module resources is the next step after the first successful build.
- `FLAG_SECURE` on the review and sign-in screens is not added yet.
- No ViewModel or UI tests. The feature modules declare test dependencies only.
- No CI job builds this folder. Adding one touches `.github/workflows/`, which
  needs the owner's sign-off under this repo's rules.
- Build logic is repeated in each module rather than shared through convention
  plugins.
