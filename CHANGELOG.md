# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project uses
[Semantic Versioning](https://semver.org/).

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

[1.1.0]: https://github.com/soderlind/jev-mail-triage/compare/1.0.0...1.1.0
[1.0.0]: https://github.com/soderlind/jev-mail-triage/releases/tag/1.0.0
