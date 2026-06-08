# Redeem Code Telegram Bot

Modular Telegram bot built with `python-telegram-bot`, async SQLAlchemy, and PostgreSQL.

The bot lets users verify required channel membership, earn points from verified referrals, request Google redeem code withdrawals, and support the developer through Telegram Stars.

## Features

- Channel verification before dashboard access.
- Referral deep links with one-time reward after verification.
- Point ledger with configurable referral reward and withdrawal cost.
- Admin-managed redeem code inventory.
- Automatic redeem code withdrawals with admin retry/reject controls.
- Telegram Stars support invoices and `/paysupport`.
- Polling runtime for straightforward local and server deployment.
- PostgreSQL storage through `DATABASE_URL`.

## Setup

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e .
Copy-Item .env.example .env
```

Edit `.env` with your bot token, admin Telegram IDs, PostgreSQL URL, and required channels. The bot automatically fetches its own username and uses built-in defaults for rewards, withdrawal cost, support amount, performance tuning, and startup/shutdown safety.

`DATABASE_URL` must point to PostgreSQL. These formats are accepted:

```text
postgresql+asyncpg://user:password@host:5432/database
postgresql://user:password@host:5432/database
postgres://user:password@host:5432/database
```

Provider examples that use `psycopg2.connect(...)` usually give the same URL this bot needs. Put that URL directly in `DATABASE_URL`; do not add `psycopg2` connection code to the bot:

```env
DATABASE_URL=postgres://avnadmin:password@your-aiven-host.aivencloud.com:28969/defaultdb?sslmode=require
```

If your password contains special characters such as `@`, `:`, `/`, `#`, or `?`, URL-encode the password before putting it in `DATABASE_URL`.

`sslmode=require` is supported for managed PostgreSQL providers that encrypt traffic but use a self-signed or private certificate chain. Use `sslmode=verify-ca` or `sslmode=verify-full` when your provider gives you a trusted CA bundle and you want certificate validation.

The bot must be able to call `getChatMember` for every channel in `REQUIRED_CHANNEL_IDS`. For private channels, add the bot as an admin and use the numeric channel ID.

Force-join channels:

```env
REQUIRED_CHANNEL_IDS=-1001234555,-1002203742882
```

The bot creates one separate join button for every channel in `REQUIRED_CHANNEL_IDS`. Public `@channel` entries get automatic `https://t.me/...` links. Numeric/private chat IDs get auto-generated invite links on startup, so the bot must be an admin in those chats with invite-user permission.

## Run

```powershell
redeem-bot
```

The bot initializes the PostgreSQL schema on startup and then starts Telegram polling.

Pending Telegram updates are dropped on startup by default, so commands sent while the bot was offline are not processed later. Set `TELEGRAM_DROP_PENDING_UPDATES=false` only if you intentionally want backlog processing.

Bot tables are prefixed with `bot_` to avoid collisions with existing database tables such as `users`.

## Project Structure

```text
src/
  app.py              # Telegram application wiring
  __main__.py         # polling entrypoint
  config.py           # public config import and .env-backed settings
  core/               # settings implementation
  database/           # SQLAlchemy engine and models
  services/           # business logic for points, referrals, withdrawals
  helpers/            # Telegram helper functions and keyboards
  modules/            # bot feature modules: user, admin, payments
  utils/              # shared UI/text utilities
```

## Docker

Build and run locally:

```powershell
docker build -t redeem-code-bot .
docker run --env-file .env redeem-code-bot
```

## Heroku

This project is configured for a Heroku container worker dyno.

[![Deploy](https://www.herokucdn.com/deploy/button.svg)](https://heroku.com/deploy?template=https://github.com/bisug/TGReedemBot)

```powershell
heroku stack:set container -a your-app-name
heroku config:set BOT_TOKEN=... ADMIN_IDS=... DATABASE_URL=... -a your-app-name
git push heroku main
heroku ps:scale worker=1 -a your-app-name
```

Use a managed PostgreSQL database for Heroku. If Heroku provides a `postgres://...` URL, the bot automatically normalizes it for the async PostgreSQL driver.

## Render

This project includes a Render Blueprint for a Docker background worker.

[![Deploy to Render](https://render.com/images/deploy-to-render-button.svg)](https://render.com/deploy?repo=https://github.com/bisug/TGReedemBot.git)

Render will create a worker service from `render.yaml`. During setup, enter:

- `BOT_TOKEN`
- `ADMIN_IDS`
- `DATABASE_URL`
- `REQUIRED_CHANNEL_IDS`

Use a Render PostgreSQL database or any external PostgreSQL provider, then set `DATABASE_URL` on the worker. Because this bot uses Telegram polling, deploy it as a background worker, not a web service.

## Admin Commands

- `/stats` - show bot statistics.
- `/broadcast <message>` - send a message to all registered users.
- Reply with `/broadcast` to broadcast the replied message. `?broadcast <message>` is also supported.
- Broadcasts preserve Telegram formatting and custom emoji from the command text or replied message.
- `/genpoints <points> [max_uses] [custom_code]` - create a points claim code.
- `/admin` - show admin menu.
- `/addcodes` - add codes, one per line after the command.
- `/codes [all|unused|reserved|used]` - list redeem codes.
- `/updatecode <old_code> <new_code>` - replace an unused redeem code.
- `/removecode <code>` - remove an unused redeem code.
- `/stock` - show inventory counts.
- `/withdrawals` - list open withdrawal records.
- `/approve <withdrawal_id>` - retry or manually approve a withdrawal.
- `/reject <withdrawal_id> [reason]` - reject without deducting points.

Example code upload:

```text
/addcodes FCGXJ43S9VYK1PZJ
9S4TKZNCH209SPXC
L3TSW7X2T6F8VLTA
6DR1PCYTX0ZU5LN5
DVWBZVLXXJLM7TCF
```

## User Commands

- `/start` - open the dashboard.
- `/help` - show available commands.
- `/claim <code>` - redeem a points code generated by an admin.
- `/withdraw` - check withdrawal status and request a redeem code automatically.
- `/paysupport` - get payment support instructions.
