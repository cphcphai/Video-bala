# Telegram File Receiver Bot

Production-oriented Telegram file receiver bot built with Python 3 and `python-telegram-bot`, using Telegram polling and SQLite. It also runs a lightweight HTTP health server in the same process for Render Web Service deployments.

## Features

- Telegram polling; no webhook required.
- Render Web Service compatible: binds the health endpoint to `0.0.0.0:$PORT`.
- SQLite database initialized automatically without deleting existing records.
- Owner authorization controlled by `OWNER_ID`.
- `/add USERID` and `/remove USERID` are owner-only.
- Owner is always authorized.
- Upload flow supports Thumbnail and Video, but accepts the file only as a Telegram Document/File.
- Original Telegram file is never downloaded, re-encoded, recompressed, or modified.
- Stores Telegram `file_id` and `file_unique_id`.
- Every upload gets a UUID-based unique upload ID.
- Receive flow never asks for a target Telegram ID; the receiver is always the current Telegram user.
- Independent 12-hour successful-delivery cooldowns for Thumbnail and Video.
- SQLite reservation transaction protects against rapid duplicate receive attempts.
- Stale reservations older than 30 minutes are released.
- Failed Telegram deliveries release their reservations.
- Delivery notifications are sent to `OWNER_ID` only after the delivery is successfully recorded as sent.
- Safe long-message splitting for `/checkall` and `/checkyourupload`.
- Time is stored internally as timezone-aware UTC timestamps.
- Callback queries are acknowledged and callback data is validated.

## Installation

Python 3.10+ is recommended.

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

On Windows, activate the virtual environment with `.venv\Scripts\activate`.

Create environment variables from `.env.example` in your deployment environment. The application does not read a `.env` file automatically, so do not commit secrets.

Run:

```bash
python bot.py
```

## Environment variables

- `BOT_TOKEN` — required Telegram bot token. Never hard-coded.
- `OWNER_ID` — required numeric Telegram user ID of the owner.
- `DB_PATH` — SQLite path; defaults to `bot.db`.
- `PORT` — supplied by Render for the health server. The application defaults to `10000` only for local convenience; Render supplies its own port.

## Owner setup

Set `OWNER_ID` to the owner's numeric Telegram ID. On startup, the owner is inserted/updated as authorized. Existing database records are not deleted.

Owner commands:

- `/add USERID`
- `/remove USERID`

`USERID` must be numeric. The owner cannot remove themselves.

## Commands

- `/start` — welcome screen with Receive and Upload buttons. It never resets an existing authorization.
- `/help` — command and workflow help.
- `/id` — shows the current user's Telegram ID.
- `/checkyourupload` — shows only uploads created by the current user.
- `/checkall` — available to the owner and authorized users; shows all uploads and delivery counts.
- `/add USERID` — owner only.
- `/remove USERID` — owner only.

## Upload process

Press `📤 Upload`, select `🖼 Thumbnail` or `🎬 Video`, then send the file as a Telegram Document/File.

Normal Telegram photo/video messages are rejected. The bot validates the requested type using the Telegram document MIME type and/or filename extension.

The bot does not download the file. It stores the Telegram `file_id` and `file_unique_id`, so later delivery uses Telegram's existing file reference.

## Receive process

Press `📥 Receive`.

There is no target-user-ID input. The receiver is always the current Telegram user, and the callback handler uses `callback.from_user.id` rather than any client-supplied receiver ID.

Unauthorized users receive their own ID and the configured access-contact message. Authorized users immediately receive Thumbnail/Video choices.

For a selected type, the bot reserves one eligible upload in SQLite, sends exactly one Telegram document, and then marks the delivery as sent.

## Authorization

Only the owner can authorize or remove users. Ordinary users cannot authorize themselves.

`/start` updates ordinary users' profile information but does not overwrite their authorization flag.

## 12-hour limits

Thumbnail and Video have separate cooldowns.

A receiver may successfully receive one Thumbnail and one Video within the same 12-hour period. Each cooldown is calculated from the actual stored `delivered_at` timestamp.

Cooldown state is based on successful deliveries, not attempts.

## SQLite

Tables:

- `users`
- `uploads`
- `deliveries`

`deliveries` uses `(upload_id, receiver_id)` as its primary key. Useful indexes are created automatically.

SQL parameters are bound with SQLite placeholders; user input is never interpolated into SQL.

Startup uses `CREATE TABLE IF NOT EXISTS` and does not destroy existing data.

WAL mode and a database lock/transaction strategy are used to reduce contention for concurrent operations.

## Duplicate protection

Receive attempts use `BEGIN IMMEDIATE` and insert a `reserved` delivery row before calling Telegram.

The algorithm is:

1. Release reservations older than 30 minutes.
2. Check the receiver's successful 12-hour cooldown for the requested type.
3. Find an upload not already reserved or successfully delivered to that receiver.
4. Insert the reservation inside the same SQLite write transaction.
5. Commit the reservation.
6. Send the Telegram document using its stored `file_id`.
7. On success, change the row from `reserved` to `sent` with `delivered_at`.
8. On Telegram failure, delete the `reserved` row so the upload can be retried.

The unique `(upload_id, receiver_id)` key prevents duplicate delivery records.

SQLite and Telegram are separate systems, so they cannot form one mathematically atomic transaction across a process crash. If a process dies after Telegram accepts the document but before SQLite records `sent`, the external send cannot be rolled back. The reservation timeout/recovery mechanism limits stuck reservations, but this cross-system crash window cannot be eliminated without an external idempotency mechanism that Telegram's send API does not provide.

## Reservation recovery

Any `reserved` delivery older than 30 minutes is deleted before a new reservation attempt and at startup.

This prevents an interrupted operation from permanently blocking an upload.

## Render Web Service deployment

Create a Render **Web Service**, not a Background Worker.

The included `render.yaml` uses:

- Build: `pip install -r requirements.txt`
- Start: `python bot.py`

The bot starts a health server on `0.0.0.0:$PORT`, while Telegram polling runs in the same process.

Set `BOT_TOKEN` and `OWNER_ID` as Render environment variables. `DB_PATH` defaults to `bot.db`.

### Render storage limitation

Render Web Services use ephemeral local filesystem storage unless a persistent disk is explicitly configured on a plan/service that supports it. Therefore, the SQLite database in `bot.db` can be lost when the service is replaced/redeployed if no persistent disk is attached.

For a production deployment where authorization, uploads, and delivery history must survive service replacement, use persistent storage appropriate to your Render plan or move the database to a durable external database. This project itself does not delete the database during startup.

The Telegram files themselves are not stored on the Render filesystem because the bot stores Telegram `file_id` values instead of downloading the files.

## Telegram file_id behavior

Telegram provides a `file_id` that can be used by bots to send the same Telegram-hosted file again. This project stores that ID and does not download or transform the file.

`file_unique_id` is also stored as a stable identifier supplied by Telegram, but it is not used as the send handle.

## Security

- `BOT_TOKEN` comes only from the environment.
- Tokens are never printed.
- Owner authorization is server-side.
- Callback receiver identity is taken from `callback.from_user.id`.
- No target receiver ID is accepted by receive callbacks.
- SQL is parameterized.
- Numeric owner-command IDs are validated.
- Telegram/API exceptions are handled so one user's operation does not terminate the bot.

## Health endpoint

`GET /` returns:

```text
OK
```

The health server is intentionally minimal and runs in a daemon thread so it does not block Telegram polling.

## Validation

The project is tested with Python bytecode compilation plus an isolated SQLite functional test suite covering authorization, `/start` persistence, receive identity, upload storage, independent cooldowns, reservation uniqueness, failure release, reporting scopes, long-message splitting, owner persistence, security scanning, and health-server binding.

