# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project uses
[Semantic Versioning](https://semver.org/).

## [Unreleased]

## [2.0.1] - 2026-11-25

### Fixed

- Statistics: counts and percentages in chart legends no longer wrap one
  character per line when the Outcome card is narrow; the donut shrinks instead.

## [2.0.0] - 2026-11-25

### Added

- macOS menu bar app (`macos/`, preview) with a native SwiftUI interface:
  - Setup with read-only Bridge and TypeSafe checks, and an explanation of what
    is sent to TypeSafe.
  - Dashboard with running, paused, Bridge unavailable and error states.
  - Statistics, and a review list with headers fetched on demand.
  - Settings for policy, local gate and service options, with Mailbox and
    Review folder pickers filled from Bridge (`bridge.folders`, read-only).
  - An explicit confirmation before turning on automatic moves.
  - A "Needs attention" card that explains each failure and re-queues failed
    assessments (`attention`, `attention.retry`). Interrupted moves are never
    retried automatically.
  - Launch at login.
- The app bundles the engine as a PyInstaller helper, so no Python install is
  needed. It requires macOS 27 or later on Apple Silicon.
- Background worker as an `SMAppService` LaunchAgent. It uses the existing
  single-worker database lock, honours pause, resume and reload through a
  control file, and publishes a status heartbeat.
- Explicit migration from the CLI LaunchAgent. It stops and renames the old
  agent, and has the helper re-save the Keychain items so it owns them.
- Versioned JSON protocol (`jev-mail-triage rpc`) with structured errors and
  distinct exit codes, plus the `worker` command.
- Build, inside-out signing, DMG and notarization scripts (`packaging/`,
  `macos/scripts/`).

The CLI and its `serve` LaunchAgent keep working as before.

### Fixed

- Long messages that exceed the model's token limit (`max_tokens_exceeded`) are
  now assessed with progressively shortened input instead of failing five times
  and needing attention. Shortened assessments are never moved; messages that
  still do not fit are reported as `input_too_large`.
- Check Key in setup works before any configuration is saved.
- The worker releases its lock while backing off after a failure, so CLI
  commands are not blocked, and pausing interrupts move recovery and gate
  training promptly; interrupted training resumes on the next run.
- Settings and worker control files are updated under a file lock, so the app
  and worker cannot overwrite each other's changes.
- The setup wizard stays open if saving a secret to the Keychain fails.
- Pause and Quit only quits once triage is paused.
- Pressing Return in list fields (allowlist, languages, training folders) starts a new line.

## [1.2.0] - 2026-11-25

### Added

- `stats` command with totals: mail found and assessed, spam and scam/phishing
  decisions, messages moved, mail left in place, how much Jev and the local gate
  decided, and local gate verdicts by reason. Supports `--days N` and `--json`.
- `apply --limit N` moves stored move decisions to the review folder without
  new TypeSafe calls. Requires `move_enabled = true`.
- `skip-backlog` marks Inbox mail that has not been assessed yet as seen
  (including mail already queued), so only new arrivals are sent to TypeSafe.

### Fixed

- Moves never happened with Proton Mail Bridge: IMAP capabilities were read
  before login, when Bridge does not yet advertise `MOVE` or `UIDPLUS`, so every
  move decision ended as `safe_move_capability_unavailable`. Capabilities are
  now read after login.

### Changed

- `config.example.toml` now sets `review_folder = "Folders/Jev Review"`, because
  Proton Mail Bridge only allows new folders under `Folders/`. The README
  explains this and how to rebuild `.venv` after a Python upgrade.
- `stats`, `status`, `candidates`, `train`, and `evaluate` now print tables
  using [Rich](https://github.com/Textualize/rich), with colour in a terminal.
  Scores at or above a threshold are highlighted. `candidates` without `--json`
  shows a table instead of one JSON object per line. Mail headers are shown as
  plain text, so they can't inject terminal formatting. `--json` output is
  unchanged.

## [1.1.0] - 2026-10-14

### Added

- **Local gate**: an optional filter that runs on your Mac before Jev. It
  decides confident cases locally and sends only uncertain mail to TypeSafe.
  Checks run in order:
  1. Authenticated allowlist and known correspondents (people you have written
     to, learned from your Sent folder).
  2. Allowed languages.
  3. Near-duplicates of mail in your Spam folder.
  4. A Bayes filter trained on your own folders, your corrections, and
     confident Jev results.
- `[gate]` config section with three modes: `off`, `shadow` (record local
  verdicts while Jev still decides), and `enforce` (confident local verdicts
  skip TypeSafe).
- `allowed_languages` setting: mail written mostly in other languages is
  treated as spam. Language detection runs offline with Lingua, sentence by
  sentence, so a foreign signature does not count against a message. `no`
  expands to `nb` and `nn`.
- `train` command that reads your Spam, Archive, and Sent folders read-only.
  `serve` retrains automatically every `train_interval_seconds`.
- Moving a message to Spam that was left in the Inbox, or to Archive after it
  was sent to review, counts as a correction and is weighted more heavily.
- `status` shows local gate verdicts and `local_disagree`. `candidates` shows
  the local verdict and reason. `evaluate` reports local gate results.
- ADR 0002 on the local gate, and new glossary terms.

### Fixed

- A message you moved from `Jev Review` back to the Inbox was assessed again
  and moved straight back. It is now recognised and left alone, even with the
  gate off.

### Changed

- New dependency: `lingua-language-detector` (about 170 MB, fully offline).
- The database schema is upgraded to version 3 automatically. When the gate is
  on, it also stores hashed word and link-domain counts, similarity signatures,
  and hashed correspondent addresses. No readable message text is stored.

## [1.0.0] - 2026-10-13

### Added

- Initial release: dry-run-first triage of Proton Mail Bridge mail with
  TypeSafe's Jev model, scoring each new message for spam and scam/phishing.
- Optional moves to a `Jev Review` folder using UID MOVE or verified
  UIDPLUS copy-and-expunge. Mail is never deleted.
- `serve`, `poll`, `backfill`, `status`, `candidates`, `retry`, `evaluate`,
  and `secret` commands; secrets stored in macOS Keychain.
- Baseline handling, UIDVALIDITY recovery, retries with backoff, and
  crash-safe move recovery.

[Unreleased]: https://github.com/soderlind/jev-mail-triage/compare/2.0.1...HEAD
[2.0.1]: https://github.com/soderlind/jev-mail-triage/compare/2.0.0...2.0.1
[2.0.0]: https://github.com/soderlind/jev-mail-triage/compare/1.2.0...2.0.0
[1.2.0]: https://github.com/soderlind/jev-mail-triage/compare/1.1.0...1.2.0
[1.1.0]: https://github.com/soderlind/jev-mail-triage/compare/1.0.0...1.1.0
[1.0.0]: https://github.com/soderlind/jev-mail-triage/releases/tag/1.0.0
