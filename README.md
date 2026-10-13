# Jev Mail Triage

Find spam and phishing in your Proton Mail inbox — locally, safely, and without
touching your mail until you say so.

Jev Mail Triage runs on your Mac, reads new messages through
[Proton Mail Bridge](https://proton.me/mail/bridge), and asks
[TypeSafe.ai's Jev model](https://docs.typesafe.ai/) two questions about each one:

1. **Is this unsolicited spam?**
2. **Is this a scam or phishing attempt?**

By default it only **records** the answers (dry-run). Messages are moved to a
`Jev Review` folder only if you explicitly turn that on. Nothing is ever deleted.
Apple Mail shows any moves automatically, because it is connected to the same
Bridge account.

> [!IMPORTANT]
> **Privacy:** for each assessed message, the sender, subject, a shortened text
> body, and link addresses are sent to TypeSafe. That data leaves Proton's
> end-to-end encrypted environment. Use this tool only if you are comfortable
> with that.

## What you need

- A Mac with **Proton Mail Bridge** installed, signed in, and running.
- **Python 3.11** or later.
- A **TypeSafe API key** with access to a Jev model.

Your Mac and Bridge must stay running for the service to check mail.

## Quick start

### 1. Install

```sh
git clone https://github.com/soderlind/jev-mail-triage.git
cd jev-mail-triage
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e '.[dev]'
```

### 2. Create your config file

```sh
mkdir -p ~/.config/jev-mail-triage
cp config.example.toml ~/.config/jev-mail-triage/config.toml
```

### 3. Copy your settings from Bridge

Open **Proton Mail Bridge**, select your account, and open
**Mailbox details → IMAP**. You will see a host, port, security mode, username,
and password.

Edit `~/.config/jev-mail-triage/config.toml` and fill in the `[bridge]` section:

| Config setting  | Where to find it in Bridge                      |
| --------------- | ----------------------------------------------- |
| `host`          | IMAP host (usually `127.0.0.1`)                 |
| `port`          | IMAP port (often `1143`)                        |
| `security_mode` | `starttls` or `implicit_tls`, exactly as shown  |
| `username`      | IMAP username (usually your email address)      |

Do **not** put the password in the config file. You will store it in Keychain
in the next step.

### 4. Store your secrets in macOS Keychain

```sh
.venv/bin/jev-mail-triage secret set bridge-password
.venv/bin/jev-mail-triage secret set typesafe-api-key
```

Each command asks you to paste the secret. Nothing is shown on screen.

- **Bridge password:** copy it from **Mailbox details → IMAP → Password** in
  Bridge. This is a Bridge-generated password, **not your Proton account
  password**.
- **TypeSafe API key:** copy it from your TypeSafe account.

Never share these in screenshots or chat. If one leaks, rotate it (see
[Troubleshooting](#troubleshooting)).

> [!TIP]
> The commands use `~/.config/jev-mail-triage/config.toml` by default. To use
> another file, add `--config /path/to/config.toml` before the command, for
> example `jev-mail-triage --config my.toml poll`.

### 5. Check that it connects

```sh
.venv/bin/jev-mail-triage poll
```

The first run records a **baseline**: it notes where your Inbox currently ends
and skips existing messages. From then on, only new arrivals are assessed.
`poll` never moves mail.

### 6. (Optional) Assess some existing messages

```sh
.venv/bin/jev-mail-triage backfill --limit 25 --dry-run
```

This assesses up to 25 of your newest existing messages without moving them.
Each assessed message is sent to TypeSafe and may use API credits.

### 7. Run it continuously

```sh
.venv/bin/jev-mail-triage serve
```

This checks for new mail every 60 seconds. Press `Ctrl+C` to stop. To run it in
the background at login, see [Run at login](#run-at-login-optional).

### 8. Review the results

```sh
.venv/bin/jev-mail-triage status
.venv/bin/jev-mail-triage candidates --headers
```

`status` shows counts. `candidates --headers` lists the messages Jev considers
spam or phishing, with subject, sender, and date, so you can check whether it
got them right.

## Everyday commands

| Command                           | What it does                                                  | Changes mail?               |
| --------------------------------- | ------------------------------------------------------------- | --------------------------- |
| `poll`                            | Checks for new mail once                                      | Never                       |
| `serve`                           | Checks for new mail every 60 seconds                          | Only if moves are enabled   |
| `status`                          | Shows counts per status; no message content                   | No                          |
| `candidates`                      | Lists the 20 newest spam/phishing decisions (scores and IDs)  | No                          |
| `candidates --headers`            | Same, plus subject, sender, and date fetched from Bridge      | No                          |
| `backfill --limit N`              | Assesses up to N existing messages, newest first (max 1,000)  | Only if moves are enabled   |
| `backfill --limit N --dry-run`    | Same, but never moves anything                                | Never                       |
| `retry --limit N`                 | Tries failed assessments again                                | No (they are re-queued)     |
| `evaluate --labels FILE.csv`      | Measures accuracy against labelled test emails                | No                          |
| `secret set NAME`                 | Stores a secret in Keychain                                   | No                          |

Prefix each command with `.venv/bin/jev-mail-triage`, or activate the virtual
environment first (`source .venv/bin/activate`) and run `jev-mail-triage`.

Useful options:

- `status --json` and `candidates --json` print machine-readable output.
- `candidates --limit 100` shows more results (maximum 1,000).

> [!NOTE]
> `--dry-run` prevents mailbox changes, but messages are **still sent to
> TypeSafe** for assessment.

### Understanding the output

- **`assessed`** — the message was checked.
- **`move_decision`** — Jev scored it at or above a threshold. In dry-run mode
  this means "would have moved", **not** that it was moved.
- **`baseline` / `rebaseline`** — the service recorded a new starting point and
  skipped existing mail. A rebaseline happens when the mailbox identity changes,
  for example after removing and re-adding the account in Bridge.

`candidates` reads only the local database and can run while `serve` is running.
Only `--headers` contacts Bridge. It opens the Inbox read-only, so messages stay
unread. Headers are printed on your screen only; they are not stored or sent to
TypeSafe.

## Turning on automatic moves

Start in dry-run mode, review `candidates --headers` for a while, and only enable
moves once you trust the results.

1. Review the thresholds in the `[policy]` section of your config:

   ```toml
   move_enabled = false
   spam_threshold = 0.98
   scam_threshold = 0.95
   ```

   A message is a candidate when either score is **at or above** its threshold.
   Lower values catch more messages but increase the risk of false positives.
   These defaults are **provisional**, not validated guarantees. Calibrate them
   with [labelled test emails](#measuring-accuracy) if you can.

2. Set `move_enabled = true`.
3. Restart `serve`.

When moves are enabled:

- Only high-scoring messages are moved, into a `Jev Review` folder (configurable
  with `review_folder`). Apple Mail shows it under your Proton account.
- Uncertain and clean messages stay where they are.
- Messages whose content was too large to send in full are never moved.
- **Nothing is ever deleted.** Check `Jev Review` and move any mistakes back.

> [!NOTE]
> Changing thresholds affects future assessments only. Messages that were
> already assessed are not rescored.

## Troubleshooting

**"Could not connect to Proton Mail Bridge"**
Make sure Bridge is open and signed in. Check that `host`, `port`, and
`security_mode` match **Mailbox details → IMAP** exactly.

**Login fails, or Bridge reports "no such user"**
The username or password does not match Bridge. Copy both again from
**Mailbox details → IMAP**, then run `secret set bridge-password`.

**My Bridge password was exposed**
In Bridge, remove the account (trash icon) and add it again; this creates a new
password. Run `secret set bridge-password` again. Expect a `rebaseline` on the
next run.

**Certificate or TLS errors**
Certificate checks are always on and cannot be disabled. If macOS does not trust
the Bridge certificate, export it from Bridge as a PEM file and set `tls_ca_file`
in the `[bridge]` section. Do not install a global trust bypass.

**Keychain error**
The program only stores secrets in macOS Keychain and refuses other storage
backends.

**Messages stuck as failed**
Temporary TypeSafe or Bridge errors are retried automatically with increasing
delays. If a message still fails, run `retry --limit 20`. Moves with an unclear
outcome are never retried automatically; check those messages by hand.

**`move_decision` is greater than zero, but nothing moved**
That is expected in dry-run mode. See
[Turning on automatic moves](#turning-on-automatic-moves).

## Run at login (optional)

A sample macOS LaunchAgent is included at
`launchd/com.soderlind.jev-mail-triage.plist`. This project never installs or
activates it for you.

1. Open the sample file and replace the executable and log paths with absolute
   paths for your setup.
2. Install and start it:

   ```sh
   mkdir -p ~/Library/LaunchAgents ~/Library/Logs
   cp launchd/com.soderlind.jev-mail-triage.plist \
     ~/Library/LaunchAgents/com.soderlind.jev-mail-triage.plist
   plutil -lint ~/Library/LaunchAgents/com.soderlind.jev-mail-triage.plist
   launchctl bootstrap "gui/$(id -u)" \
     ~/Library/LaunchAgents/com.soderlind.jev-mail-triage.plist
   ```

3. To stop and unload it:

   ```sh
   launchctl bootout "gui/$(id -u)/com.soderlind.jev-mail-triage"
   ```

Only do this after you have reviewed the dry-run results.

## Measuring accuracy

You can measure how well Jev and your thresholds perform using labelled `.eml`
files. Create a CSV file next to them:

```csv
path,spam,scam_phishing
newsletter.eml,false,false
phishing.eml,false,true
```

Paths are relative to the CSV. Then run:

```sh
.venv/bin/jev-mail-triage evaluate --labels ./fixtures/labels.csv
```

The report shows false positives, precision, recall (for spam, scam/phishing,
and combined), and how many messages would be sent for review. It uses your
configured thresholds and model.

Each file is sent to TypeSafe and may use API credits. Use synthetic or
otherwise approved emails, not real inbox messages.

## How it protects you

**Your mail**

- Dry-run by default; moves require `move_enabled = true`.
- Never deletes mail.
- Reads messages with `BODY.PEEK`, so unread mail stays unread.
- Before moving a message, it checks that the message still exists and is
  unchanged. It prefers IMAP `UID MOVE`. Otherwise, it copies the message,
  verifies the copy, and removes only that one message. It never runs a broad
  `EXPUNGE`; if a move cannot be completed safely, it stops and flags the
  message.
- Only one worker can run at a time.

**Message content**

- Emails are parsed locally. Plain text is preferred; HTML is converted to text,
  with scripts and styles removed.
- Links are listed but never opened. Remote images and other resources are never
  loaded, and message content is never executed.
- Attachments are never sent.
- Input is limited: up to 2 MiB is read from Bridge and up to 64 KiB is sent to
  TypeSafe by default (`max_input_bytes`). Truncated messages are never moved.
- Email content is treated as untrusted data in the Jev prompt. The criteria
  separate spam from subscribed newsletters, receipts, and genuine
  correspondence. A link alone is not considered malicious. Norwegian and
  English are supported.

**Local storage**

- The database (`~/Library/Application Support/jev-mail-triage/state.sqlite3`)
  stores message identifiers, a content fingerprint, Message-ID, scores,
  model/policy versions, retry state, and move history. It **does not** store
  bodies, subjects, or senders.
- Database and lock files are readable only by your user account.
- The Bridge password and TypeSafe API key are stored only in macOS Keychain.
- Logs contain status information and message numbers only, never credentials
  or message content.

## Reference

### Configuration

| Section     | Setting                 | Default                | Meaning                                               |
| ----------- | ----------------------- | ---------------------- | ----------------------------------------------------- |
| *(top)*     | `database_path`         | Application Support    | SQLite state file                                     |
| `[bridge]`  | `account_id`            | `primary`              | Name used to find the Bridge password in Keychain     |
|             | `host`, `port`          | `127.0.0.1`, `1143`    | Bridge IMAP address                                   |
|             | `security_mode`         | `starttls`             | `starttls` or `implicit_tls`, as Bridge shows         |
|             | `username`              | —                      | Bridge IMAP username                                  |
|             | `mailbox`               | `INBOX`                | Folder to check                                       |
|             | `tls_ca_file`           | *(unset)*              | PEM certificate if macOS does not trust Bridge        |
| `[service]` | `poll_interval_seconds` | `60`                   | Time between checks                                   |
|             | `batch_size`            | `20`                   | Messages per check                                    |
|             | `max_attempts`          | `5`                    | Attempts before a message is marked as failed         |
|             | `backoff_max_seconds`   | `300`                  | Longest delay between attempts                        |
| `[policy]`  | `move_enabled`          | `false`                | Allow moves in `serve` and `backfill`                 |
|             | `spam_threshold`        | `0.98` (provisional)   | Minimum spam score for review                         |
|             | `scam_threshold`        | `0.95` (provisional)   | Minimum scam/phishing score for review                |
|             | `max_input_bytes`       | `65536`                | Maximum text sent to TypeSafe                         |
|             | `review_folder`         | `Jev Review`           | Destination folder for moves                          |
|             | `model`                 | `jev-latest`           | TypeSafe model                                        |
|             | `allowlist`             | `[]`                   | Trusted senders; see below                            |
|             | `trusted_authserv_id`   | *(unset)*              | Required for the allowlist                            |

### Allowlist (advanced)

The allowlist prevents a move only when the sender address or domain matches
**and** a trusted `Authentication-Results` header (from `trusted_authserv_id`)
reports DMARC pass. Proton does not document that it adds or cleans these
headers, so configure this only if you have independently verified that the
named receiver is trusted and replaces forged headers. Missing or untrusted
authentication evidence never prevents a move.

### Mailbox resets

If the mailbox identity (IMAP UIDVALIDITY) changes, the service records a new
baseline instead of reprocessing or moving old messages. Use a bounded
`backfill` if you want to assess messages that arrived during the reset.

## Development

Tests use synthetic `.eml` files and fake IMAP/TypeSafe clients only:

```sh
python -m pytest
python -m ruff check src tests
```

The tests do not connect to Proton Mail Bridge, access a real mailbox, call
TypeSafe, move messages, or activate a LaunchAgent.

## License

[MIT](LICENSE)
