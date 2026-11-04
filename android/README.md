# AgentCoSign for Android (work in progress)

Last updated: 2026-11-04 23:48 IST

A native Android version of the approver app, built from the nine mobile mockups.
**Every screen runs on sample data.** Nothing in this app talks to the gateway yet,
and no passkey is created or used. Each screen shows a "Sample data" badge, and
controls with no gateway endpoint say so on screen.

## Build status

| Part | Verified | How |
|---|---|---|
| `:core:model`, `:core:domain`, `:core:data` (pure Kotlin) | Yes | Compiled, 14 unit tests pass |
| `:core:designsystem`, `:feature:*`, `:app` (Compose, Hilt, Nav3) | **No** | Never compiled. The session that wrote them had no Android SDK |

Run a build on a machine with the Android SDK before trusting any of the Android
modules. Expect compile fixes, most likely around Navigation 3 and the Hilt
assisted ViewModel API.

```bash
cd android
./gradlew :core:domain:test :core:data:test   # pure Kotlin, no SDK needed
./gradlew :app:assembleDebug                  # needs the Android SDK (API 35)
```

Library versions in `gradle/libs.versions.toml` were not checked against Google
Maven, which could not be reached when they were written.

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
| Choose limits, Settings limits | none | Kept on the phone |
| Staff, Add a person | none | Kept on the phone |
| Alerts | none | Kept on the phone |
| Connect PayPal | none | Moves the flow forward only |
| Download refund history | none (`/api/v1/audit/head` is the chain head only) | Not wired |

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

- ViewModels are scoped to the activity, not to each screen. Add the Nav3
  ViewModel entry decorator once the build compiles.
- No ViewModel or UI tests. The feature modules declare test dependencies only.
- No CI job builds this folder. Adding one touches `.github/workflows/`, which
  needs the owner's sign-off under this repo's rules.
- Build logic is repeated in each module rather than shared through convention
  plugins.
