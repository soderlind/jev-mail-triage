# macOS app

`JevMailTriage.app` is a menu bar app for the same triage engine as the CLI. It
bundles its own Python runtime, so users need no Python, pip, virtual
environment or source checkout.

- **Requires** macOS 27 or later on Apple Silicon. See [Intel](#intel) for why
  Intel isn't supported yet.
- **Status:** preview. It is built from source; no signed release has been
  published.

## How it works

| Part | What it does |
| --- | --- |
| `JevMailTriage.app` (SwiftUI) | Provides the menu bar item, dashboard, setup and Settings. It holds no mail logic. |
| `Contents/Helpers/JevMailTriageHelper.app` | The Python engine, frozen with PyInstaller. The app runs it as `rpc` for one JSON request at a time. |
| `Contents/Library/LaunchAgents/com.soderlind.JevMailTriage.worker.plist` | The background worker (the same helper running `worker`), registered with `SMAppService`. |

The app and the CLI share one config, database and set of Keychain items:

- Config: `~/.config/jev-mail-triage/config.toml`
- Status, control file and database: `~/Library/Application Support/jev-mail-triage/`
- Logs: `~/Library/Logs/JevMailTriage/worker.log`

The protocol and the Keychain ownership decision are described in
[ADR 0003](adr/0003-macos-app-with-bundled-helper.md).

### Lifecycle

- **Start** clears the pause flag and registers the background worker. The
  first time, macOS may ask you to allow it under **System Settings → General →
  Login Items & Extensions**. The app links there and shows **Needs approval in System
  Settings** until you do.
- **Closing windows** keeps triage running. The menu bar item stays.
- **Pause** writes the pause flag. The worker stops before its next cycle,
  releases the single-worker lock, and idles without contacting Bridge or
  TypeSafe. The worker process stays registered, so resuming is immediate.
- **Turn Off Background Worker…** (dashboard) unregisters the worker entirely.
- **Quit** asks when triage is active. **Quit** leaves the worker running in the
  background; **Pause and Quit** stops triage first. Background triage does not
  need the menu bar app.
- **Open menu bar app at login** (Settings → General) only controls the menu bar app, not the
  worker.

### Safety

- **Single worker.** Only one worker can run at a time. Each worker holds a lock
  on the database, and the legacy agent uses the same lock, so neither can run
  alongside the other.
- **Restarts.** launchd restarts a crashed worker after a 30-second throttle.
  Retries keep the existing backoff and attempt limits.
- **Bridge.** If Bridge is down, the status shows **Bridge unavailable** and the
  worker retries on its normal interval.
- **Sleep/wake.** After the Mac wakes, the worker checks mail promptly.
- **Settings changes.** When the config changes, the worker reloads it between
  cycles.

## Build

Prerequisites:

- Apple Silicon Mac on macOS 27 with Xcode 27 (Swift 6) command line tools.
- [uv](https://docs.astral.sh/uv/). The helper is built with uv's standalone
  CPython 3.12 because its `minos` is low enough for the deployment target.
  Homebrew Python 3.12 on macOS 27 is built with `minos 27.0`.
- Network access for the first `pip install` of the pinned helper requirements.

```sh
packaging/build-helper.sh      # build/helper/dist/JevMailTriageHelper.app (≈330 MB)
macos/scripts/build-app.sh     # build/app/JevMailTriage.app, ad-hoc signed
```

`build-app.sh` builds the helper if it's missing, then:

1. Builds the Swift app in release mode for arm64.
2. Assembles the bundle and signs it inside out.
3. Verifies it with `codesign --verify --strict`.
4. Checks that every Mach-O file is arm64 with `minos` ≤ 27.0.
5. Smoke-tests the embedded helper with an empty environment and a throwaway
   HOME.

Set `REBUILD_HELPER=1` to force a fresh helper build. Set `MIN_MACOS` to change
the deployment target.

Ad-hoc builds run without the hardened runtime. The hardened runtime enforces
library validation, which needs every loaded image to have the same Team ID, and
ad-hoc signatures have no Team ID. All signing with a real identity uses the
hardened runtime with empty entitlements.

### Development

```sh
cd macos && swift test                                  # protocol, client and lifecycle tests
JEV_HELPER=$PWD/../build/helper/dist/JevMailTriageHelper.app/Contents/MacOS/jev-mail-triage-helper \
  swift run JevMailTriage                               # UI against a built helper
```

`SMAppService` only works from an assembled bundle. Under `swift run` the
background worker always shows as not registered. Use `build-app.sh` to test the
lifecycle.

### Distribution (Developer ID)

Credentials stay in your login Keychain. Nothing is read from the repository.

```sh
SIGN_IDENTITY="Developer ID Application: Your Name (TEAMID)" macos/scripts/sign.sh
SIGN_IDENTITY="Developer ID Application: Your Name (TEAMID)" macos/scripts/make-dmg.sh
xcrun notarytool store-credentials jev-notary --apple-id YOU@example.com --team-id TEAMID   # once
NOTARY_PROFILE=jev-notary macos/scripts/notarize.sh build/app/JevMailTriage-<version>-arm64.dmg
```

`sign.sh` does not use `--deep`. It signs, in order:

1. Each nested dylib and Python extension module.
2. The helper bundle.
3. The app.

`sign.sh` then verifies the result and re-runs the helper smoke test. An `Apple
Development` identity works for local hardened-runtime testing, but only Developer
ID builds can be notarized.

### Intel

Intel builds are not supported yet. A universal or x86_64 build would need
three things:

- An x86_64 or universal2 Python.
- x86_64 wheels for `pydantic_core` and `lingua`.
- `check-macho.sh` run against `x86_64`.

None of these has been validated.

## Install and set up

1. Open the DMG and drag **Jev Mail Triage** to Applications. Then open it.
2. Setup explains what is sent to TypeSafe: the sender name and address, the
   subject, the plain-text body and link destinations, capped at
   `max_input_bytes`. Message bodies, subjects and senders are never stored.
3. Enter the Bridge details from **Bridge → Settings → IMAP**:
   1. Fill in the connection fields and the Bridge password.
   2. If Bridge uses a certificate your Mac doesn't trust, choose the exported
      certificate.
   3. Click **Test Connection**. It is read-only and must pass.
4. Paste the TypeSafe API key and click **Check Key**.
5. Save. The worker starts in **dry run**, which logs decisions and moves
   nothing.
6. To enable automatic moves, use **Enable Automatic Moves…** on the dashboard
   and confirm. Moves only go to the review folder; mail is never deleted.

## Migrating from the CLI LaunchAgent

If `~/Library/LaunchAgents/com.soderlind.jev-mail-triage.plist` is loaded, the
app shows **Legacy LaunchAgent found**. It will not start its own worker until you
migrate. Click **Migrate…** and confirm. The app then:

1. Runs `launchctl bootout` on the legacy agent and waits until it has stopped.
2. Renames its plist to `com.soderlind.jev-mail-triage.plist.migrated-<timestamp>`.
   The plist is not deleted.
3. Has the helper re-save the Bridge password and TypeSafe key (`secrets.adopt`).
   macOS asks once per item whether **jev-mail-triage-helper** may use it; choose
   **Always Allow**.
4. Registers and starts the bundled worker.

Your config, database, review state and `move_enabled` setting carry over
unchanged.

The re-saved Keychain items now belong to the app's helper. If you later run the
CLI from the repo, macOS may ask to allow Python as well.

To undo the migration:

1. Turn off the background worker in the app.
2. Rename the plist back.
3. Run `launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.soderlind.jev-mail-triage.plist`.

## Manual test checklist

Run these on a signed build (Developer ID, or Apple Development for local
testing) with a test Proton account where possible.

- [ ] **Clean install.** Use a new user account with no config. Setup appears;
      Test Connection fails clearly with Bridge stopped and passes with it
      running. A wrong key fails Check Key. Save starts the worker in dry run.
- [ ] **Keychain prompts.** First save creates both items with no prompt.
      Restarting the worker and app causes no prompts. A rebuilt ad-hoc copy
      prompts; that is expected.
- [ ] **Background approval.** Start shows **Needs approval in System Settings** and opens Login
      Items. After you allow it there, the status becomes **Running** within a
      few seconds. Disabling it in System Settings makes the app show approval
      needed again.
- [ ] **Legacy migration.** With the legacy agent loaded, the app refuses to
      start and offers **Migrate…**.
      - The plist is renamed and `launchctl print gui/$UID/com.soderlind.jev-mail-triage`
        fails.
      - You get one Keychain prompt per item.
      - The worker runs and only one `jev-mail-triage-helper worker` process
        exists.
- [ ] **Pause and resume.** Pause shows **Paused** and `worker.log` stops
      showing cycles. Resume continues within one poll interval. Quit with
      **Pause and Quit** leaves the worker paused; plain **Quit** leaves it
      running.
- [ ] **Sleep and wake.** Sleep for more than 5 minutes, then wake. The log
      shows "Wake from sleep detected", and there is no duplicate worker or
      duplicate move.
- [ ] **Bridge downtime.** Quit Bridge: the status shows **Bridge unavailable**.
      Restart Bridge: the status returns to Running with no manual action.
- [ ] **Helper crash.** `kill -9` the worker. launchd restarts it after about
      30 seconds, and the dashboard shows the restart rather than an error.
- [ ] **Settings change.** Change the poll interval in Settings. The worker
      reloads it, `config.toml.bak` is written, and `move_enabled` is unchanged.
- [ ] **Upgrade.** Quit the app, then replace it with a newer build. The
      SMAppService registration persists. Check that the worker runs the new
      helper (`ps -o command` shows the path inside the new bundle; if it
      doesn't, use Turn Off Background Worker… and then Start). The config,
      database and Keychain items are kept, and no extra prompts appear for
      the same Developer ID.
- [ ] **Uninstall.** Turn off the background worker and delete the app. No
      worker remains (`launchctl print gui/$UID/com.soderlind.JevMailTriage.worker`
      fails).
