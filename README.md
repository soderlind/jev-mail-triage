# Jev Mail Triage

A local, dry-run-first service that assesses new Proton Mail messages through
Proton Mail Bridge and TypeSafe.ai's Jev model. It does not access Proton Mail
directly or change mailbox state unless movement is explicitly enabled.

## Requirements

- macOS with Proton Mail Bridge running and an account configured for IMAP.
- Python 3.11 or later.
- A TypeSafe API key and a Jev model enabled for the account.

The Mac and Bridge must remain running for polling to work. In move mode, Apple
Mail reflects the folder changes made by Bridge over IMAP.

## Install and configure

From this project directory:

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e '.[dev]'
mkdir -p ~/.config/jev-mail-triage
cp config.example.toml ~/.config/jev-mail-triage/config.toml
```

Edit `~/.config/jev-mail-triage/config.toml` with the host, port, security mode,
username, and account identifier shown in Proton Mail Bridge. Choose either
`starttls` or `implicit_tls` exactly as Bridge specifies; the service does not
probe or downgrade TLS. Certificate and hostname validation are always enabled.
If macOS does not trust the Bridge certificate, configure `tls_ca_file` to a
PEM CA/certificate file supplied by your Bridge setup. Do not disable
verification or install a global trust bypass.

Store secrets interactively in macOS Keychain:

```sh
.venv/bin/jev-mail-triage --config ~/.config/jev-mail-triage/config.toml \
  secret set bridge-password
.venv/bin/jev-mail-triage secret set typesafe-api-key
```

The selected Python `keyring` backend must be macOS Keychain; the program
refuses to use another backend. Bridge credentials are keyed by `account_id`.
`secret set bridge-password` reads that identifier from the selected config.

## Commands

The first `poll` or `serve` run records the current Inbox UIDVALIDITY/UIDNEXT
baseline and intentionally leaves existing messages alone. Only later arrivals
are processed. Use an explicit bounded backfill to assess existing messages;
it processes newest first and accepts at most 1,000 per invocation.

```sh
# Assess new arrivals once; always dry-run, regardless of config.
.venv/bin/jev-mail-triage poll

# Show counts and operational statuses only; no message content is stored/output.
.venv/bin/jev-mail-triage status
.venv/bin/jev-mail-triage status --json

# List up to 20 stored review candidates using local SQLite only.
.venv/bin/jev-mail-triage candidates
.venv/bin/jev-mail-triage candidates --limit 100 --json

# Explicitly fetch subject, sender, date, and Message-ID locally from Bridge.
.venv/bin/jev-mail-triage candidates --headers

# Re-queue up to 20 non-movement failures for another attempt.
.venv/bin/jev-mail-triage retry --limit 20

# Explicitly assess up to 25 existing messages. Dry-run unless config enables moves.
.venv/bin/jev-mail-triage backfill --limit 25
.venv/bin/jev-mail-triage backfill --limit 25 --dry-run

# Poll continuously at service.poll_interval_seconds (default 60).
.venv/bin/jev-mail-triage serve
```

`serve` and `backfill` honor `policy.move_enabled`; it defaults to `false`.
`poll` always forces dry-run. To permit automatic moves, deliberately set
`move_enabled = true` in the config and run `serve` or a backfill. Only messages
at or above the configured spam or scam/phishing threshold are candidates.
Defaults (`spam_threshold = 0.98`, `scam_threshold = 0.95`) are provisional,
not validated guarantees. Calibrate using labeled fixtures before enabling
movement. Truncated inputs are never moved. Uncertain or clean messages remain
where they are; this application never deletes mail.

`retry` only re-queues failed assessments without a pending/ambiguous movement
journal. Ambiguous movement states require manual inspection and are never
automatically repeated. The status output includes stored move-decision counts,
which are classification verdicts rather than confirmation that mail moved.
`candidates` lists those recorded move decisions, including historical decisions,
with UIDVALIDITY, UID, Message-ID, scores, reason, status, and truncation flag.
It reads SQLite without updating assessments and can run alongside `serve`.
The default limit is 20 (maximum 1,000); newest assessments appear first.
Only `--headers` connects to Bridge. It selects the source mailbox read-only and
uses bounded `BODY.PEEK` header fetches, preserving read status. Headers are
printed locally, never stored or sent to TypeSafe. JSON escaping prevents email
header control characters from affecting the terminal. A changed UIDVALIDITY,
missing source message, or non-pending status is reported instead of looking up
an unrelated UID or searching the review folder. Connection failures are explicit.
Bridge operations retry up to `max_attempts` with exponential backoff capped by
`backoff_max_seconds`. TypeSafe failures are retried per message and persisted;
exhausted messages require the explicit `retry` command.

## Classification and privacy

The service sends the sender, subject, bounded text body, and extracted link
destinations to TypeSafe in one request with separate spam and scam/phishing
Noul questions. The criteria distinguish unsolicited bulk mail from subscribed
newsletters, receipts, and genuine correspondence, and do not treat link
presence alone as malicious. Norwegian and English messages are supported.
TypeSafe receives these fields outside Proton's end-to-end encrypted
environment; use only if that data flow is acceptable to you.

MIME parsing is local. Plain text is preferred; HTML is reduced to text, script
and style contents are discarded, and links are listed without fetching them.
Attachments are excluded. The service does not load remote resources, follow
links, execute message content, or send attachments. Raw IMAP reads use
`BODY.PEEK` so unread mail remains unread. Input is bounded (2 MiB fetched from
Bridge and 64 KiB sent by default); truncation is recorded and prevents a move.

The SQLite database stores mailbox/UID identity, a content fingerprint,
Message-ID if available, scores, model/policy versions, retry state, and
movement journal. It does not store message bodies, subjects, or senders.
Database and lock files are restricted to the current user. Bridge password and
TypeSafe API key are stored in macOS Keychain. Logs contain operational status
and UIDs only; they do not include credentials or message content.

The optional allowlist only suppresses a move when an exact address/domain
matches and a configured trusted `Authentication-Results` authserv-id reports
DMARC pass. Proton does not document a guarantee that such inbound headers are
added or sanitized. Do not configure this option unless you have independently
verified that the named receiver is trusted and strips/replaces forged headers.
Missing or untrusted authentication evidence never suppresses a move.

## Safe movement

When enabled, the service creates/uses a root-level `Jev Review` folder and
rechecks the source UID, UIDVALIDITY, and content fingerprint immediately before
acting. It prefers UID MOVE. If unavailable, it requires UIDPLUS, copies the
message, verifies the copied content, then deletes only the source UID with
UID EXPUNGE. It never issues a broad EXPUNGE. If the server cannot safely
complete or reconcile an action, the item is marked for attention rather than
blindly retried. UIDVALIDITY changes establish a new baseline; use bounded
backfill if you need to review messages that arrived during a reset window.

## Fixture evaluation

Evaluation sends each listed fixture to TypeSafe and may incur API usage. Use
synthetic or otherwise approved fixtures only; never use real inbox messages for
automated testing. The CSV is local, with a header and one `.eml` path and
two boolean labels per row:

```csv
path,spam,scam_phishing
newsletter.eml,false,false
phishing.eml,false,true
```

Paths are resolved relative to the CSV. Run:

```sh
.venv/bin/jev-mail-triage evaluate --labels ./fixtures/labels.csv
```

Output includes false positives, precision, recall for spam, scam/phishing,
and combined review decisions, plus review volume. The evaluation uses the
configured thresholds and model.

## LaunchAgent (optional)

`launchd/com.soderlind.jev-mail-triage.plist` is a sample only. It is not
installed or activated by this project. Copy it to
`~/Library/LaunchAgents/`, replace the executable/log paths with absolute paths
for your installation, create the log directory, and inspect it before manually
bootstrapping it. Example user-run steps after editing the sample:

```sh
mkdir -p ~/Library/LaunchAgents ~/Library/Logs
cp launchd/com.soderlind.jev-mail-triage.plist \
  ~/Library/LaunchAgents/com.soderlind.jev-mail-triage.plist
plutil -lint ~/Library/LaunchAgents/com.soderlind.jev-mail-triage.plist
launchctl bootstrap "gui/$(id -u)" \
  ~/Library/LaunchAgents/com.soderlind.jev-mail-triage.plist
# To stop and unload:
launchctl bootout "gui/$(id -u)/com.soderlind.jev-mail-triage"
```

Only bootstrap it after verifying dry-run results and consciously enabling
movement if desired. These commands are documentation only; the project does
not copy, load, or activate the LaunchAgent.

## Development checks

Tests use synthetic `.eml` fixtures and fake IMAP/TypeSafe clients only:

```sh
python -m pytest
python -m ruff check src tests
```

The automated suite does not connect to Proton Mail Bridge, access a real
mailbox, call TypeSafe, move messages, or activate a LaunchAgent.
