# Victorious messaging + shell: handoff (2026-11-21)

Branch `claude/new-session-tkkgkz` in each repo. Status words are used strictly: **Implemented** (code on branch), **Built** (compiled/packaged here), **Tested** (automated test run here), **Device-verified** (ran on a phone/SIM/real provider). **Nothing in this work is device-verified.**

## What exists

| Area | Repo | Implemented | Built | Tested |
|---|---|---|---|---|
| Fleet contract 1.1 (terminal, one-shot, drafts), relay, CLI, Control Center (Arch web + Debian Tk) | VictorLinux | yes | python compile only for UIs | `fleet/tests` 76 tests (simulated phone with real pty), `tests/test_fleet_control_center.py`; **UIs not run in a browser/Tk** |
| Phone worker for 1.1 | DroidCommand-AI `core-fleet-worker` | yes (JVM) | yes | 14 unit + 2 live tests against the real Python coordinator. **Not wired into `:app`** (the worker was never constructed in `:app` before either); Android adapters for the VictorSuite broker and VictorSMS draft bridge are **not written** |
| Shell broker, owner approval, `shell.exec` on-device confirm, Remote shell view | VictorSuite | yes | `:app:assembleDebug` OK (APK built) | 32 broker/dispatcher tests (fake pty/gate) + existing remotecontrol suite; real pty path (`PtyBridge`) **not run** |
| Terminal hand-off + host enforcement | rustdeskV | proto, `terminal_policy.rs`, host check in `connection.rs`, client declaration, Kotlin transport service, Dart hand-off | **no** (no Flutter/Rust full build here) | `terminal_policy.rs` 5 tests via `rustc --test` only |
| Unified inbox, email, outbox, drafts review, MMS download | VictorSMS | yes | `:app:assembleDebug` OK (3 ABI APKs) | `mail-core` 19 tests (GreenMail IMAPS/SMTPS + fake SMTP server), app unit tests (drafts, MMS parser) |

## Update (later the same day)
* DroidCommand: `BrokerOnlyRunner` (tested) refuses every shell/root/termux/shizuku-capable tool for REMOTE-initiated requests; `app/fleet/LiveBridges.kt` (VictorSuite AIDL broker client, VictorSMS Messenger draft client) compiles with `-PincludeAndroid=true` but is **not run on a device** and `FleetWorker` is still **not constructed in `:app`** (it never was).
* Own SIP/VoIP: `voip/` server (6 real-Asterisk TLS tests), VictorSMS SIP provisioning + Telecom service; editions `standard` (Apache-2.0 + Twilio) and `voip` (GPLv3 + Linphone, uncompiled here). See `voip/README.md`, VictorSMS `docs/EDITIONS.md`.

## Not done / blockers (exact)
1. **rustdeskV Android *host* terminal service** (VictorLinux/desktop → phone via rustdesk) is NOT implemented; the host fails closed (`connection.rs` passes `victorsuite_ready=false`). The required VictorLinux→phone flow uses the coordinator + DroidCommand worker + VictorSuite instead (implemented on the JVM side only). Remaining: Rust JNI glue to `IShellBroker`.
2. **DroidCommand `:app` wiring**: construct `FleetWorker` with `LiveFeatures`, an AIDL `ShellBrokerClient` (copy `IShellBroker*.aidl`), a Messenger `DraftHandoffClient` (ops `message.draft.deliver|status`, group `DRAFT`), and the capability probe (`LiveCapabilities`). **Bypass closure is therefore not done**: DroidCommand's own `core-shell`, `core-termux` (RUN_COMMAND, adb), `core-root`, `core-shizuku` paths still exist for local use and a remote-origin routing guard is not implemented.
3. **Thunderbird/K-9**: could not be evaluated at a pinned revision (github.com unreachable from this environment). Choice made: Eclipse Angus Mail (+jsoup). K-9's libraries are, to my knowledge, not published as standalone artifacts [unverified]. Angus Mail on Android is not device-tested.
4. **OAuth**: no client registrations exist; manual setup works. Shortest check: register a Google/Microsoft client, add to `OAuthRegistry`, run XOAUTH2 against the provider.
5. **MMS**: download + raw PDU only; no decoding/display/sending. Needs a SIM with MMS to verify.
6. **VictorSMS repo pin**: `kotlin = 2.4.21` is not published; changed to `2.4.10`. JDK 17 toolchain required (I installed openjdk-17 to build).
7. Attachments of received mail are not cached (only metadata); `ImapClient.fetchOne` exists but no "open attachment" UI. Server-side Drafts sync exists in core but the UI saves drafts locally only. Background sync is foreground-loop only (no JobScheduler).
8. VictorSuite: `shell.exec` approval blocks the single envelope worker thread while the owner decides. VictorSuite's other local shell actions (timers, boot/scheduled commands, zt socket, ADB, libsu, busybox `RunShell`, `ExeCommand`) are the product's own terminal features and were **inventoried by grep, not re-routed**.

## Shell-path inventory (grep-level; caller/target/privilege/approval/executor)
* VictorSuite broker (new): caller = trusted app (Binder uid + cert pin); target = this phone; privilege user (root separate scope + switch); approval = owner dialog per request; executor = `AndroidShellProcessFactory` (Termux pty / process).
* VictorSuite `RUN_COMMAND` (unchanged public contract): Termux permission + allow-external-apps; no per-request approval (Termux semantics).
* VictorSuite `remote.command shell.exec`: paired caller (cert pin) + external-control switch; **now** owner approval via the broker (previously always refused).
* DroidCommand: `ShellTool`/`ProcessBuilderShellExecutor`, `AdbTermuxExecutor`/`AndroidPendingIntentRunCommandClient`, `core-root`, `core-shizuku`, `AutomationTools`, `EmbeddedShizukuStarter`: local policy (`SecureToolExecutor`) + approvals; **not yet routed via VictorSuite**.
* rustdeskV: desktop `terminal_service.rs` (PTY under the authorised user); Android client terminal now hands off; Android host: none.
* VictorSMS: no shell action; SMS trigger only forwards.

## Owner manual
**Setup/pairing.** PC: `victor-fleet start; victor-fleet pair` → enter the secret on the phone's DroidCommand after confirming the fingerprint. VictorSuite: menu → *Remote shell access* → switch on → *Trust an installed app* (DroidCommand) after checking the certificate; root is a separate switch and scope. VictorSMS: Settings → Connected apps → pair DroidCommand with *Drafts* (separate from *Send*); Settings → Email accounts.
**Mail.** See VictorSMS manual (Email section).
**Terminal on a phone (from the PC).** `victor-fleet devices` shows Terminal/Messaging state with next steps; `victor-fleet terminal DEVICE` (or Control Center → Open terminal); approve on the phone; `Enter ~.` detaches. One command: `victor-fleet exec DEVICE -- ls`.
**Terminal on a PC (from Android).** VictorSuite → *Remote shell (PC)* or rustdeskV → Terminal; trust each other's certificate once (rustdeskV shows VictorSuite's fingerprint; VictorSuite → *Remote shell access* → tick *Terminal transport* for rustdesk).
**Drafts.** `victor-fleet message draft-email DEVICE --to a@b --subject S --body B`; review and tap Send on the phone; `victor-fleet message status ID`.
**Revoke.** `victor-fleet revoke DEVICE` (ends live shells), VictorSuite → *Remote shell access* → Revoke/Disconnect, VictorSMS → Connected apps → revoke.

## Commands run
`cd VictorLinux/fleet && python3 -m unittest discover -s tests -p 'test_*.py'` (76 OK); `cd VictorLinux/tests && python3 -m pytest test_fleet_control_center.py`; `cd droidcommand-AI && VICTOR_FLEET_LIB=…/VictorLinux/fleet ./gradlew :core-fleet-worker:test`; `cd VictorSuite && ANDROID_HOME=/opt/android-sdk ./gradlew :app:testDebugUnitTest --tests 'com.termux.victor.*' :app:assembleDebug`; `cd victorsms && ./gradlew :mail-core:test :app:testDebugUnitTest :app:assembleDebug -Porg.gradle.java.installations.paths=/usr/lib/jvm/java-17-openjdk-amd64`; `rustc --edition 2021 --test rustdeskV/src/server/terminal_policy.rs`.
APKs: `VictorSuite/app/build/outputs/apk/debug/*.apk`, `victorsms/app/build/outputs/apk/debug/*.apk`.
