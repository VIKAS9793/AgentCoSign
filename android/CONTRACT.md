# AgentCoSign Android: engineering contract

Last verified: 2026-11-05 IST (scaffold aligned). Scope: everything under `android/`.
This contract is tracked in git as `android/CONTRACT.md`. A local, untracked
`android/CLAUDE.md` containing the single line `@CONTRACT.md` loads it into Claude
sessions (the repo's guardrails refuse a tracked file with that name).

The app is a native client of the **same gateway** as the web approver
(`agentcosign/web/approve.js`). It adds no money rules of its own. If the web
app and this app disagree, the gateway is right and this app is wrong.

---

## 0. How to use this file

1. **No memory-based Android.** Every version, API name, and platform behaviour
   used in code must trace to a row in section 2 (verified, with source and date)
   or be checked against an official page *before* use and then added to section 2.
   If a source cannot be reached, mark the item `UNVERIFIED` in code review and
   do not ship it.
2. **Re-verify on a schedule.** Re-check section 2 whenever a dependency
   changes, and at least monthly. Update the date at the top.
3. **Read the backend before touching a screen.** Section 3 is a summary.
   The source of truth is `agentcosign/api/gateway_routes.py`,
   `agentcosign/schemas/gateway.py`, `agentcosign/services/gateway.py`,
   `agentcosign/services/approval_view.py`, and `config.yaml`.
4. When a rule here conflicts with the repo's tracked rules (`tools/guardrails.py`,
   `docs/THREAT_MODEL.md`), the tracked rules win. Fix this file.

---

## 1. Non-negotiables (money safety, carried over from the gateway)

These mirror the gateway's guarantees. Breaking one is a release blocker.

| # | Rule | Why (gateway source) |
|---|---|---|
| N1 | **Fail closed.** Any unknown state, parse failure, timeout or unexpected status means *nothing is sent* and the screen says so. Never show success you did not read back. | Fail-closed design; `FailClosedError` |
| N2 | **The server decides; the phone formats.** Section, `can_send`, `cant_send_reason`, limits and totals come from `/v1/approvals/home`. The app never recomputes eligibility, limits or "fits" locally. | `Gateway.home()` docstring: "decided here, not on the phone" |
| N3 | **"Sent" only on `paypal_status == "COMPLETED"`.** `executed` alone is not success. | guardrail `CONFIRMED_SUCCESS`; `approve.js` |
| N4 | **Money is `BigDecimal` from a string.** Never `Double` or `Float`, never `toFloat()` on an amount. Parse with the server's pattern `^\d{1,9}(\.\d{1,2})?$`. A display-only ratio (progress bar) is computed with `BigDecimal.divide(..., RoundingMode)` and then converted. | `schemas/gateway.py` `MONEY`; guardrail "Money" |
| N5 | **One decision, one signature.** Every approve, reject, stop or resume needs a fresh passkey assertion over a server challenge bound to that request and intent. No cached approvals and no batch approve. | `/v1/approvals/{id}/options`, `/v1/controls/{intent}/options` |
| N6 | **Send what the owner saw.** `POST /v1/approvals/{id}/options` carries `seen.paid` and `seen.already_refunded` exactly as displayed. If the server answers 409 because facts changed, show the new facts. Never auto-retry. | `IntentIn.seen`; threat T6 |
| N7 | **No AI-written text in the decision area.** The approval screen shows PayPal facts first. Anything the assistant read is labelled as such and rendered as plain text, never as styled or linked content. | threat T8 |
| N8 | **The safe choice is never harder.** Decline and Back are always reachable, never hidden, disabled, or made smaller than Approve. Doing nothing sends nothing. | threat T8 |
| N9 | **No secrets on disk.** The session token lives in memory only (lifetime `session_seconds`, max 600 s in code). There is no "remember me". Never log tokens, bodies, or customer text. | `config.yaml approval.session_seconds`; threat T11 |
| N10 | **No double submit.** Decision buttons go inert from tap until the response arrives (state-driven, not a debounce timer). A process death mid-decision reloads `/home`; it never resends. | UAT-FINDINGS problem 2 |

---

## 2. Verified platform baseline (2026-11-05 IST)

| Item | Value | Source (fetched 2026-11-04/05) |
|---|---|---|
| Play target API | **targetSdk 36** required for new apps and updates since **2026-08-31** (extension to 2026-11-01 possible) | developer.android.com/google/play/requirements/target-sdk |
| compileSdk | **37**. Required: Compose 1.12 (BOM 2026.09.00) "requires compileSdk 37 and AGP 9", and androidx.hilt 1.4.0 compiles against 37 (needs AGP ≥ 9.2). **targetSdk stays 36** (Play minimum) until the Android 17 behaviour changes are read and tested. `compileSdk = 37` assignment form | develop/ui/compose/compiler; releases/hilt; build/releases/gradle-plugin |
| AGP | **9.5.0** (October 2026); needs **Gradle 9.6.0**, SDK Build Tools 36.0.0, JDK 17; NDK not required | same |
| Backup/restore tests | AGP 9.5 adds **experimental** automated backup and restore tests (`android.experimental.testSuiteSupport=true`, `android.experimental.enableBackupTest=true`, `testOptions.backupTests`, JUnit 5, connected device). Experimental, so not a release gate | same |
| Kotlin on Android | **Built-in Kotlin** (AGP 9): do **not** apply `org.jetbrains.kotlin.android`; `kotlin-kapt` is incompatible; use `kotlin { compilerOptions { } }`, not `android.kotlinOptions` | developer.android.com/build/migrate-to-built-in-kotlin |
| Kotlin | **2.4.21** (2026-10-08) | kotlinlang.org/docs/releases.html |
| KSP | **2.3.13** (KSP2 versioning is independent of Kotlin) | Maven Central metadata |
| Hilt | **2.60.1** (2026-07-06). AGP 9 support for the Hilt Gradle plugin arrived in **2.59**; **2.59.1 requires AGP ≥ 9.0.0**; 2.59.2 fixed slow incremental builds from `HiltSyncTask` on AGP 9; 2.60 built against Kotlin 2.3.21. AGP 9 + Hilt is reported to break with `android.enableJetifier=true`: keep Jetifier off. Plugin `com.google.dagger.hilt.android`, runtime `com.google.dagger:hilt-android`, processor through **KSP** (`ksp(...)`), never kapt | github.com/google/dagger/releases; developer.android.com/training/dependency-injection/hilt-android |
| Hilt processor artifact | Both `com.google.dagger:hilt-compiler` and `com.google.dagger:hilt-android-compiler` are published at 2.60.1, and Google's Hilt page shows each in a different snippet. Use `hilt-compiler`; if the first AGP 9 build fails on the processor, switch and record the result here | Maven Central POMs; hilt-android page |
| Compose BOM | **2026.09.00**; compose-ui **1.12.1**, foundation/animation **1.12.1** | developer.android.com/jetpack/compose/bom/bom-mapping |
| Material 3 | **1.4.0** stable; 1.5.0-beta02 (2026-10-21) | releases/compose-material3 |
| M3 Expressive | In the **1.5** line: `MotionScheme`, `MaterialExpressiveTheme`, expressive buttons, `ButtonGroup`, `FloatingToolbar` graduated to stable. `LoadingIndicator`/`MaterialShapes` promotion was reverted. Their status **in 1.4.0 is UNVERIFIED** | releases/compose-material3 |
| Navigation 3 | **1.2.0** stable (`navigation3-runtime`, `navigation3-ui`) | releases/navigation3 |
| Nav3 API names | `NavDisplay`, `entryProvider { entry<K> {} }` (scope class `EntryProviderScope`), `rememberNavBackStack(vararg NavKey)`, `rememberSaveableStateHolderNavEntryDecorator()`, `NavEntryDecorator` (class), `NavigationBackHandler`, metadata DSL `NavMetadataKey`; `NavDisplay` takes `List<SceneStrategy>` (the `then` chaining is deprecated) | releases/navigation3 |
| androidx.hilt | **1.4.0** (2026-07-01). `hiltViewModel()` lives in **`androidx.hilt:hilt-lifecycle-viewmodel-compose`**, package `androidx.hilt.lifecycle.viewmodel.compose` (moved out of `hilt-navigation-compose` in 1.3.0). Assisted `hiltViewModel(creationCallback)` since 1.2.0; that it survived the move is **UNVERIFIED** | releases/hilt |
| Activity / Core | activity-compose **1.13.0**; core **1.19.1** (`core-ktx` is now an empty compatibility artifact; depend on `androidx.core:core`). `onBackPressed()` deprecated | releases/activity; releases/core |
| Coroutines / serialization | kotlinx-coroutines **1.11.0**; kotlinx-serialization **1.12.0** | Maven Central metadata |
| Gradle | **9.6.0** (AGP 9.5 minimum) | build/releases/gradle-plugin |
| Lifecycle | **2.11.0** (2026-06-17), includes `lifecycle-viewmodel-navigation3` with `rememberViewModelStoreNavEntryDecorator()` | releases/lifecycle |
| Credential Manager | **androidx.credentials 1.6.0** (+ `credentials-play-services-auth`); passkeys need **API 28+** | releases/credentials; identity/passkeys/create-passkeys |
| Passkey origin from an app | `android:apk-key-hash:<base64url(SHA-256 of signing cert), no padding>`; the server must allow-list it (one per signing cert: debug, upload, Play app signing) | identity/passkeys/create-passkeys |
| Legacy auth | Credential Manager replaces **FIDO2 API, Smart Lock for Passwords, One Tap, legacy Google Sign-In** | identity/sign-in/credential-manager |
| Android 16 targeting | Edge-to-edge opt-out (`windowOptOutEdgeToEdgeEnforcement`) **disabled**; predictive back on by default, **`onBackPressed` not called, `KEYCODE_BACK` not dispatched**; orientation/resizability/aspect locks **ignored on sw600dp+**; `elegantTextHeight` ignored | about/versions/16/behavior-changes-16 |
| Compose compiler | Strong skipping **on by default** since Kotlin 2.0.20; `@Stable`/`@Immutable` only where `.equals()` is cheap; avoid them on large-collection models in lazy lists | compose/performance/stability/strongskipping |
| Fonts | `ui-text-google-fonts:1.12.1` for downloadable fonts; **variable fonts are not supported through downloadable fonts**, so bundle them in `res/font`; `FontVariation.Settings` | develop/ui/compose/text/fonts |
| Baseline Profiles | Plugin + Macrobenchmark `benchmark-macro-junit4` ≥ 1.5.0, `profileinstaller` ≥ 1.4.1; generate on non-minified variant, ship minified; also Startup Profiles | topic/performance/baselineprofiles/overview |
| Compose UI tests | `ui-test-junit4`, `createComposeRule()` / `createAndroidComposeRule<A>()`, `ui-test-manifest` (debug) | develop/ui/compose/testing |
| Screenshot tests | **Compose Preview Screenshot Testing through AGP test suites** (AGP ≥ 9.5.0-alpha03, engine `0.0.1-alpha16`, **experimental**). The standalone `com.android.compose.screenshot` plugin is **no longer maintained**: do not use it. Flags `android.experimental.enableScreenshotTest=true`, `android.experimental.testSuiteSupport=true`; DSL `android { testOptions { screenshotTests.create("screenshotTest") { engineVersion = "0.0.1-alpha16"; targetVariants.add("debug"); dependencies { implementation(compose ui-tooling); implementation("com.android.tools.screenshot:screenshot-validation-api:0.0.1-alpha16") } } } }`; tests in `src/screenshotTest/kotlin`, `@PreviewTest` + `@Preview`; tasks `update{Suite}{Target}{Variant}TestSuite` / `test{Suite}{Target}{Variant}TestSuite`; references in `src/{suite}{Target}{Variant}/reference/`; report in `build/reports/tests/{task}/`. Host-side (no device); raise `android.compose.screenshot.maxHeapSize` if needed | studio/preview/compose-screenshot-testing; .../compose-screenshot-testing-with-testsuites |
| Play Financial features declaration | **Required for every app on the account, even with no financial features**, on any testing or production track (system and private apps exempt). Since the 2025-10-30 policy announcement, **no app update can be published until it is completed**. The form's feature categories were **not** readable from this session (support.google.com blocked) | search results quoting support.google.com answers 13849271, 16550159, 9876821 |
| Play Data safety | "Financial info" covers user payment info, purchase history, credit score and other financial info. Data processed **ephemerally** (in memory, only for the real-time request) must still be entered in the form but is not shown on the listing. Data that never leaves the device is not "collected" | search results quoting support.google.com answer 10787469 and Google's Data safety FAQ (third-party copies; official page not readable here) |

**UNVERIFIED (check before use, then move up):**
Hilt behaviour specifically under AGP 9 *built-in* Kotlin (the release notes say "AGP 9 support", which implies it because built-in Kotlin is AGP 9's default, but never name it) ·
the Compose compiler plugin id/version pairing under built-in Kotlin ·
`Modifier.animateItem` replacing `animateItemPlacement` ·
the full Financial features option list and the new Financial Services policy preview (read them in Play Console; support.google.com is blocked here) ·
the 16 KB page-size requirement (only if a native library is added) ·
gradle-pitest-plugin's current stable version (search showed `1.19.0-rc.2`) ·
SSE client choice (OkHttp vs Ktor).

---

## 3. Backend contract (same gateway as web)

### Auth headers
| Header | Used for | Source |
|---|---|---|
| `x-approver-session: <token>` | every `/v1/approvals/*`, `/v1/controls/*` call | `require_approver_session` |
| `x-enrolment-token: <link token>` | enrolment, from a one-time operator link (max 600 s) | `require_operator` |
| `x-workspace: <id>` | hosted-demo visitor workspace only | `WORKSPACE_HEADER` |
The app never holds the agent key or the operator key.

### Endpoints the app uses
| Flow | Calls |
|---|---|
| Enrol this phone | `POST /v1/approver/enrolment/options` → Credential Manager create → `POST /v1/approver/enrolment` |
| Sign in | `POST /v1/approver/session/options` → Credential Manager get → `POST /v1/approver/session` → `{session, approver, account, session_minutes}` |
| Home | `GET /v1/approvals/home` |
| Live hints | `GET /v1/approvals/stream` (SSE) |
| Ended today | `GET /v1/approvals/done` → `{items, total, caught}` |
| One record | `GET /v1/approvals/{id}/history` |
| Decide | `POST /v1/approvals/{id}/options {intent, seen}` → get → `POST /v1/approvals/{id}/approve|reject {credential}` → `OutcomeOut` |
| Stop / resume all | `POST /v1/controls/{intent}/options` → get → `POST /v1/controls/{intent}` |
| Record head | `GET /api/v1/audit/head` (public, no amounts) |

### Status codes, which the UI must map exactly as `approve.js` does
| Code | Meaning | UI |
|---|---|---|
| 401 | session missing or expired | drop session, stop stream, show sign-in: "Signed out after N minutes, for your safety." |
| 403 | fingerprint signature not accepted | "That check didn't go through. Try again." Request unchanged. |
| 409 | request or control can't be decided now (facts changed, time up, stopped) | show server `detail`, re-read `/home` |
| 410 | demo workspace closed | "This test setup has closed." Offer set-up again. |
| 413 / 429 | too large / rate limited (`Retry-After`) | honour `Retry-After`; never tight-loop |
| 5xx / network | unknown | N1: nothing sent, offer retry of the *read*, never of the decision |

### Home payload (`Gateway.home`)
`server_now`, `refunds_stopped` ("yes"/"no"), `paypal_checked`, `checked_at`,
`approver_display`, `account_ending`, `counts`, `soonest_decide_by`,
`sections{needs_decision, cant_send, paused, sending, failed}` (each item has
`section`, `can_send`, `cant_send_reason ∈ {stopped, paypal_unavailable, not_enough_left, time_up}`),
`done{recent,total,caught}`, `today`, `settings{auto_limit, max_limit, daily_cap, repeat_limit, decide_minutes, window_hours}`.
All numbers arrive as **strings**. Times use **`server_now`** as the clock for
countdowns, never the device clock alone.

### Live updates (SSE)
The first event is the current version. After that the stream sends a `changed`
hint when something moves (checked every 2 s), a keep-alive comment about every
14 s, and `event: signed_out` when the session expires. **Events carry no money
data.** On a hint, re-read `/home`. Collect only while the UI is at least
STARTED. Reconnect with capped backoff. Coalesce hints that arrive during a tap
(the web app holds updates for a settle window so a list never moves under a finger).

### Limits (from `config.yaml`, shown, never enforced, by the app)
auto `25.00`, approval max `250.00`, daily cap `1000.00`, 3 refunds per customer per 24 h,
decide window 14400 s (cap 86400), fingerprint prompt 120 s (cap 300), session 600 s (cap 600).
**There is no endpoint to change limits.** Limit editing in the mockups is
display-only until the gateway adds one with a signed intent.

### Who runs the gateway, and where data lives (owner decision, 2026-11-05)
- The **project owner runs the gateway** (the hosted demo website). Every merchant's
  requests, decisions and audit record are stored on the owner's infrastructure.
- Planned storage: a **free Supabase or Postgres** database. Today the gateway uses
  SQLite files on Render's free disk, which is lost on restart. The Postgres move is a
  **gateway** change (ledger hash chain, passkeys, messages, workspaces) with its own
  PR and tests. It is not an Android change.
- **The app never talks to the database.** No Supabase client SDK, no database URL, no
  anon or service key in the APK. Everything goes through the gateway's HTTPS API, so
  N2 (the server decides) still holds. Putting a database key in an app makes it public.
- Free-tier limits (pausing on inactivity, size, backups) are **UNVERIFIED**:
  supabase.com was unreachable from this session. Check them before relying on the
  demo for anything a judge or user will touch, and plan for a paused database
  being an error the app must show (N1).
- Consequence for Play: the developer operates the gateway, so the Data safety
  answers in section 12 apply as written (Purchase history declared, not ephemeral).

### Known blocker
`agentcosign/services/passkeys.py` verifies `expected_origin` against the **web
origin only**. Native assertions (`android:apk-key-hash:…`) will fail. Fix on the
gateway side: accept an origin list (web + each app signing-cert hash), and serve
`/.well-known/assetlinks.json`. Until then the app runs on sample data only.

---

## 4. Architecture

- **Modules:** `:app`, `:core:model`, `:core:domain`, `:core:data`, `:core:network`,
  `:core:designsystem`, `:core:testing`, `:feature:{onboarding,home,review,settings}`.
  `:core:model` and `:core:domain` are pure Kotlin (no `android.*`).
- **Layers:** UI → ViewModel → Repository (single source of truth) → network.
  DTOs never leave `:core:network`/`:core:data`; map to domain models at the edge.
- **State:** one `StateFlow<UiState>` per screen, sealed `Loading/Success/Error/Empty`
  (plus domain states like `Deciding`). Backing `MutableStateFlow` is private.
  One-shot effects (navigate, snackbar) go through a `Channel` or `SharedFlow`
  consumed in a lifecycle-aware collector, never stored as state flags.
- **ViewModel scoping:** Nav3 with `rememberSaveableStateHolderNavEntryDecorator()`
  **and** `rememberViewModelStoreNavEntryDecorator()`, so each entry owns its ViewModels.
- **DI:** Hilt with KSP. Constructor injection only. Test bindings via Hilt test modules.
- **Concurrency:** structured only (`viewModelScope`, injected dispatchers). No
  `GlobalScope`, no `runBlocking` outside tests. All IO off main.
- **Navigation:** back stack is app state; no business logic in `entryProvider`.
  Back is handled by `NavDisplay`/`NavigationBackHandler`, never `onBackPressed`.
- **Persistence:** DataStore for non-secret preferences only. The session token is
  never persisted (N9).

---

## 5. Kotlin standards and KDoc

- Follow the Kotlin coding conventions and the Android Kotlin style guide; ktlint or
  detekt in CI with zero warnings on changed files.
- Explicit API mode (`kotlin { explicitApi() }`) in `:core:*` modules.
- Prefer `val`, immutable data, `sealed interface` for closed hierarchies,
  `value class` for ids (`RequestId`), exhaustive `when` without `else` on sealed types.
- No `!!` in production code. No `lateinit` in ViewModels.
- Errors are values (`sealed interface Failure`) at module boundaries; exceptions
  are caught at the data edge and mapped. Never catch `CancellationException` without rethrowing.
- **KDoc** on every public and `internal` declaration in `:core:*` and every
  ViewModel/public composable: a one-line summary, then `@param`, `@return`, `@throws`
  where they add information. Document **why** and **invariants** (for example
  "amount is never above paid minus already refunded"), not restating the signature.
- Comment hygiene matches the repo guardrail: no ticket numbers, rule ids, document
  paths, version numbers, or project history in comments.

---

## 6. UX and UI rules

- **Copy:** plain words, short sentences, no em dashes. Banned in user-facing copy
  (guardrail `MERCHANT_JARGON`): agent, reject*, passkey, ledger, escalat*, webauthn,
  policy, rule, error, exception, payload, hash, undefined, null, and rule ids like `R7`.
  Say "Decline", "fingerprint", "your record". A refused action leads with
  "No money moved." Copy lives in `strings.xml` (localisable, plurals via `pluralStringResource`).
- **Hierarchy on the decision screen:** amount → customer → what PayPal says
  (paid, already refunded) → reason → who suggested it → actions. Facts before opinions.
- **Touch targets** ≥ 48 dp; decision buttons full-width on compact screens, Approve
  and Decline equal size (N8).
- **Edge-to-edge** always (forced on API 36): use `WindowInsets`/`safeDrawingPadding`;
  nothing under the gesture bar or cutout.
- **Predictive back** everywhere; mid-decision back cancels the prompt, never submits.
- **Adaptive:** no orientation lock (ignored on sw600dp+ anyway). List-detail on
  expanded widths; rotation keeps state (`rememberSaveable`, SavedState).
- **One light theme, no dark mode** (owner decision 2026-11-05). The system's
  auto-darkening is switched off (`android:forceDarkAllowed=false`) so the measured
  contrast below always holds.
- **Accessibility (WCAG 2.x AA, measured):** see section 6a. TalkBack labels on every
  icon-only control; one TalkBack stop per control (toggle rows use `toggleable` with
  `Role.Switch`, tabs use `selectable` with `Role.Tab`); amounts read in full; status
  never by colour alone (icon + words); layouts hold at 200% font scale; respect
  reduced motion (section 8).
- **Empty, loading, error, offline, stopped, signed-out** states are designed for every
  screen. Skeletons over spinners for lists; never a blank screen.
- **Sample data** must always be labelled on screen until real wiring lands.

## 6a. Colour tokens and measured contrast

Taken from the approved mockups. Ratios computed with the WCAG relative-luminance
formula; re-run the calculation for any new colour before using it.

| Token | Hex | Use | Contrast |
|---|---|---|---|
| ground | `#F3F0E9` | page background | n/a |
| surface | `#FBFAF6` | cards, sheets | n/a |
| ink | `#1F2A24` | primary text, primary button fill | 14.2 on surface, 13.0 on ground |
| muted | `#5C625D` | secondary text | 6.0 / 5.5 (AA) |
| accent | `#C4673F` | brand mark, icons only | white on it 3.9 (icons OK, **never text**) |
| good / goodSoft | `#1E6B3A` / `#E3F0E5` | Refunded, Connected, Active | 5.6 on goodSoft, 6.3 on surface |
| waiting / waitingSoft | `#8A5A00` / `#F6E8C3` | Waiting, sample data, not-built notes | 4.9 on waitingSoft, 5.2 on ground |
| danger / dangerSoft | `#A8241B` / `#F7E4E1` | Declined, Stopped, Disconnect | 5.8 on dangerSoft, 6.3 on ground |
| controlBorder | `#858A84` | outlines of buttons, switches, inputs | 3.4 on surface, 3.1 on ground (≥ 3:1 non-text) |
| border | `#E4E0D6` | decorative card edges and dividers only | 1.3: **never** the only cue that something is a control |

Other rules: touch targets ≥ 48dp; focus indicators use `ink` (14.2:1); disabled text
keeps `muted` (6.0:1) and adds a non-colour cue; no text on `accent`.

---

## 7. Typography

- Bundle the brand faces in `res/font` (variable fonts can't be downloaded). Use a
  variable sans with `FontVariation.Settings` for weight and optical tuning on
  display/headline; static fallback for the body. Verify the licence before adding.
- One `Typography` in `:core:designsystem`; screens never set `fontSize` ad hoc.
- **Money uses tabular figures** (`fontFeatureSettings = "tnum"`) so amounts align
  and don't jitter as they update.
- Amount: `displaySmall`/`headlineLarge` bold. Labels: `labelMedium` with tracking.
  Body ≥ 14 sp. Line height ≥ 1.3× size for body.
- Weight carries emphasis (M3 guidance); don't use colour alone for emphasis.
- Test every screen at 85%, 100%, 130%, and 200% font scale.

---

## 8. Motion and real-time state

Motion explains a **state change**. It never decorates, and never delays a decision.

| State change | Motion |
|---|---|
| New request arrives (SSE → re-read) | lazy list item insert with item animation; a single soft haptic if the app is foreground |
| waiting → sending → sent / failed | `AnimatedContent` on the status pill, keyed by state; "sent" appears only on `COMPLETED` (N3) |
| decide → in progress | button morphs to progress in place; the other button disables but stays visible (N8) |
| refunds stopped / resumed | banner `AnimatedVisibility` from the top; colour change on the tiles |
| countdown to `decide_by` | text updates once a minute; colour shifts in the last 15 minutes; no ticking seconds |
| screen to screen | Nav3 default transitions + predictive back; shared element only for the request card → review |

Rules:
- Use `MotionScheme` tokens if Material 3 1.5 is adopted; otherwise define spring/tween
  tokens once in `:core:designsystem`. No magic durations in features.
- **Never animate an amount's digits** (no count-up). The final value must be readable
  immediately; animate the container, not the number.
- Respect the system animation scale and reduced motion: when animations are off,
  state changes still happen instantly and nothing waits on an animation to finish.
- Read animated values in deferred phases (`graphicsLayer {}`, lambda modifiers) to
  avoid recomposing every frame.
- A list never reorders under an active touch; hold updates until the gesture ends.

---

## 9. Performance budgets

| Metric | Budget | How |
|---|---|---|
| Cold start to first frame (mid-range device) | < 1.0 s | Baseline + Startup Profiles, lazy Hilt graph |
| Home to interactive after SSE hint | < 300 ms p95 on Wi-Fi | one `/home` read, diff into state |
| Jank | < 1% slow frames on home scroll | Macrobenchmark `FrameTimingMetric` |
| Recomposition | no recomposition of the list on a single-item change | stable keys, immutable UI models, Layout Inspector counts |

- R8 full mode on release; `isMinifyEnabled = true`; keep rules minimal and reviewed.
- No work in composition: format money and times in the ViewModel or a remembered
  formatter, not on every recomposition.
- Lazy lists always have `key` and `contentType`.
- Network: one OkHttp client, timeouts on every call, HTTP/2, gzip; no polling when
  SSE is connected.

---

## 10. Security

- Passkeys only through **Credential Manager** (FIDO2 API and Smart Lock are replaced).
  `userVerification` is set by the server (`required`).
- Digital Asset Links for the gateway domain; one allow-listed origin per signing cert.
- Network security config: no cleartext, no user-added CAs in release.
- `FLAG_SECURE` on the review and sign-in screens in release builds (blocks screenshots
  and the recents thumbnail). UAT capture uses debug builds.
- `android:allowBackup="false"` or `dataExtractionRules` that exclude everything sensitive.
- No analytics or crash payloads containing amounts, names, order ids, or customer text.
- Intents: everything not exported unless it has to be; consider
  `android:intentMatchingFlags="enforceIntentFilter"` (Android 16 opt-in).

---

## 11. Testing: mutation and UAT

**Pyramid**
1. **JVM unit** (`:core:*`, ViewModels with `kotlinx-coroutines-test`): every
   state transition, every status-code mapping in section 3, money parsing edge cases.
2. **Contract tests:** DTO parsing against JSON captured from the Python test suite's
   real responses (`/home`, `OutcomeOut`, SSE frames). A schema drift fails CI.
3. **Compose UI tests** (`createComposeRule`): each UiState renders; N3, N8 and N10 hold
   (Approve disabled while deciding, Decline always present, no "sent" without COMPLETED).
4. **Screenshot tests** (AGP test suites, section 2) per state × 100%/200% font × compact/expanded width, using multi-preview annotations. Experimental tool: a screenshot diff is a review signal, and the Compose UI tests stay the gate for behaviour.
5. **Macrobenchmark** for startup and home scroll; Baseline Profile generation.

**Mutation testing**
- PIT via the gradle-pitest-plugin on **pure-Kotlin modules** (`:core:model`,
  `:core:domain`, `:core:data` mappers). Android modules are out of scope for PIT.
- Threshold: mutation score ≥ 80% on `:core:domain` and money parsing; surviving mutants
  in money, eligibility mapping or status-code mapping block merge.
- Plugin version and Kotlin coroutine support are UNVERIFIED; confirm before adding.

**UAT (on a real phone, against the sandbox, run and captured over USB like the web app)**
Each case follows the repo's UAT method: write the test, watch it fail, fix, watch it pass.
- Double-tap Approve → exactly one decision recorded.
- Kill the network after the fingerprint, before the response → app shows "checking",
  re-reads `/home`, never resends; PayPal shows one refund.
- Kill the app during the fingerprint prompt → nothing sent; reopening shows the request waiting.
- Session expires on the review screen → 401 → signed-out message; no decision sent.
- Order changed between viewing and approving → 409 → new facts shown.
- Refunds stopped from another device while reviewing → live hint → Approve becomes unavailable.
- Workspace closed (demo) → 410 message.
- Rate limited → `Retry-After` honoured.
- Rotation, split-screen, foldable posture, 200% font, TalkBack, system dark mode on (app must stay light), reduced motion,
  predictive back mid-prompt.
- Assistant-written text with markup or links renders as inert plain text (N7).
- Back up and restore the app (local, cloud and device-to-device): after a restore the
  app opens signed out, holds no session, no cached requests and no passkey state, and
  asks for a fingerprint before showing anything. Use AGP's experimental backup tests if
  they work; otherwise run it by hand with `adb shell bmgr`.

---

## 12. Play compliance checklist

- [ ] targetSdk 36 (Play requirement, verified); compileSdk 37 (Compose 1.12 requirement)
- [ ] Edge-to-edge without opt-out; predictive back without `onBackPressed`
- [ ] No orientation or resizability locks
- [ ] **Financial features declaration completed** before the first upload to any track (required for every app; updates are blocked without it).
  Recommended answer: **"My app doesn't provide any financial features"**. The form's options (as far as visible from search snippets of Google's help: *Payments and transfers → Mobile payments and digital wallets*, *Money transfer and wire services*, *Banking and loans* incl. earned wage advances, tokenised digital assets) describe apps that **provide** a financial service. This app provides none: it holds no funds, lends nothing, and is not the payment provider; the merchant's own gateway sends refunds through the merchant's own PayPal account, and the app is an approval control on top. Third-party guidance says a shop that merely receives payments need not tick the payment boxes.
  Conditions: (1) re-read the form and the **new Financial Services policy preview** (support answer 16597822) in Play Console before submitting; (2) the store listing must describe the app as an approval control ("approve or decline refunds your own PayPal business account sends"), not as payments or money transfer, or the answer will look inconsistent to a reviewer; (3) if unsure, ask Play policy support before the first release, not after a rejection.
- [ ] **Data safety form.** Principle: **declare what the developer's systems receive and store, and over-declare rather than under-declare.** An inaccurate form is a policy violation; an extra line only costs listing optics. "Collected" means sent off the device; ephemeral means in memory only for the real-time request. The gateway **stores** decisions and amounts in its ledger, so nothing it receives is ephemeral.
  Recommended answers (assuming the developer operates the gateway, as with the hosted demo; if each merchant self-hosts it and the developer never receives the data, revisit every line):
  - *Personal info → Name*: approver name at enrolment. Collected, not shared, required. Purposes: app functionality, account management.
  - *Personal info → User IDs*: the **passkey credential id** and the approver account it is bound to. It identifies a person's account, it is **not** a device identifier (passkeys sync across devices through the credential provider), so it does **not** go under *Device or other IDs*.
  - *Financial info → Purchase history*: order amounts, refund amounts and decisions recorded by the gateway. Google defines it as "purchases or transactions a user has made"; these are the merchant's customers' transactions, so the fit is imperfect, but transaction records sent back (`seen` figures, decisions) and stored server-side are the closest category. Collected, not shared, required. Purposes: app functionality; fraud prevention, security and compliance.
  - *App activity → Other actions*: approve, decline, stop and resume. Collected, not shared, required. Purposes: app functionality; fraud prevention, security and compliance.
  - *Device or other IDs*: **none today**. Adding push alerts (FCM registration token, Firebase installation id) makes this **yes**; update the form in the same release.
  - Encrypted in transit: yes. Deletion request path: describe how an approver passkey and account are removed on the gateway.
  - No analytics or ads SDKs, so nothing is shared. Any SDK reopens this list.
- [ ] Privacy policy URL; no PII in logs or crash reports
- [ ] Release signed with Play App Signing; its cert hash added to the gateway's allowed origins and `assetlinks.json`
- [ ] No deprecated APIs listed in section 13

## 13. Banned or deprecated (each verified unless marked)

`onBackPressed` / `KEYCODE_BACK` handling (not delivered on API 36) ·
`windowOptOutEdgeToEdgeEnforcement` · `elegantTextHeight` · `screenOrientation` locks ·
FIDO2 API, Smart Lock for Passwords, One Tap, legacy Google Sign-In ·
`org.jetbrains.kotlin.android` plugin and `kotlin-kapt` under AGP 9 · `android.kotlinOptions` ·
Nav3 `SceneStrategy.then` chaining · `lifecycle-compiler` processor, `launchWhenX`,
`AbstractSavedStateViewModelFactory` · `removeViewModelStoreOnPop` ·
LiveData in new code (not deprecated; banned by this contract) · `SharedPreferences` for
anything new · `Modifier.animateItemPlacement` (UNVERIFIED) · `Double`/`Float` for money.

## 14. Anti-patterns that block merge

God ViewModels (> 300 lines) · business logic in composables or `entryProvider` ·
exposing `MutableStateFlow` · `LaunchedEffect` used to handle user events ·
state flags for one-shot events · collecting flows without lifecycle awareness ·
unkeyed lazy items · formatting in composition · `remember` without keys over changing
input · reading animated state in composition where a deferred read works ·
recomputing gateway decisions on the phone (N2) · optimistic "sent" UI (N3) ·
retrying a decision call (N10) · hard-coded colours, sizes, or durations outside the
design system · copy outside `strings.xml`.

## 15. Definition of done (per screen)

1. Behaviour traces to a gateway endpoint (section 3) or is labelled sample data.
2. Every UiState is designed, previewed, and covered by a UI test.
3. Status-code mapping unit-tested; money paths mutation-tested.
4. Contrast (section 6a), 200% font, TalkBack, reduced motion and rotation checked.
5. No new UNVERIFIED items without an entry in section 2.
6. UAT cases for the screen pass on a real device against the sandbox.

## 16. Scaffold status against this contract

Aligned on 2026-11-05: AGP 9.5.0 + built-in Kotlin (no `kotlin-android`), Gradle 9.6.0,
compileSdk 37 / targetSdk 36, Kotlin 2.4.21, KSP 2.3.13, Hilt 2.60.1, BOM 2026.09.00,
Nav3 1.2.0 with `rememberViewModelStoreNavEntryDecorator()`, Lifecycle 2.11.0,
`hilt-lifecycle-viewmodel-compose` 1.4.0, credentials 1.6.0 in the catalog, one light theme with measured WCAG tokens (dark mode removed, system auto-dark off), single-stop toggle rows and tab semantics,
tabular figures for money (`MoneyText`, stat tiles), limits read-only (phone-side
editor and its local rule removed), decisions carry the `seen` figures and a changed
order is refused (unit-tested).

Still open: copy in `strings.xml`; `FLAG_SECURE`; real Credential Manager wiring
(blocked on the gateway origin fix); gateway HTTP + SSE client; ViewModel, UI and
screenshot tests; Baseline Profiles; mutation testing; first real compile.
