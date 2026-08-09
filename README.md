# Benelux Telegram Aggregator MVP

MVP pipeline for turning listings from a Telegram source group into structured cards and publishing them to a target Telegram channel.

## What it does

- Reads new or recent messages from a Telegram group via Telethon.
- Downloads attached images and sends them, plus the message text, to Gemini.
- Uses Gemini structured output to classify and normalize listing data.
- Validates and deduplicates listings in SQLite.
- Formats a compact Telegram post.
- In `TEST` mode, prints cards instead of posting.
- In `LIVE` mode, posts to the target channel.

## Setup

1. Copy `.env.example` to `.env` and fill in the values.
2. Install dependencies into your Python 3.12 virtual environment.
3. Run the worker.

## Run

```bash
source venv/bin/activate
benelux-aggregator
```

For a one-off backfill, you can also run:

```bash
python -m telegram_aggregator.cli --once
```

## Environment

- `TELEGRAM_API_ID` and `TELEGRAM_API_HASH` come from `my.telegram.org`.
- `TELEGRAM_SESSION` is the Telethon session name or path.
- `TELEGRAM_BOT_TOKEN` enables inline buttons, Safe Deal callbacks, and admin replies in channel/post workflows.
- `SOURCE_CHAT` is the source group username, invite link, or ID.
- `TARGET_CHANNEL` is the destination channel username or ID.
- `ADMIN_TELEGRAM_ID` enables `/stats`, `/export`, and Safe Deal moderation actions.
- `GEMINI_API_KEY` is your Gemini API key.
- `GEMINI_MODEL` can be `auto` to discover available Gemini models through the API, or a specific model name if you want to pin one.
- `MODE=TEST` prints output without posting.
- `MAX_AGE_DAYS=3` keeps the backfill to the last three days of source messages.
- The worker does not count Telegram channel views as unique users; analytics are based on button clicks, applications, and deal state changes only.
- `MAX_FLOOD_WAIT_SECONDS=60` makes the worker stop instead of sleeping for a very long Telegram flood wait.
- `SEND_THROTTLE_SECONDS=8` adds a small pause between successful posts to reduce flood waits.
