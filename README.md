# Telegram Course Update Bot

A simple Telegram bot where an admin can send course update notices and students can query them by date, course, or section.

## Features

- Admin-only `/update` flow
  - Ask for course name
  - Ask for module name
  - Ask for URL
  - Ask for optional date
  - Save with date/time
- Admin-only `/delete ID`
  - Delete a saved update by its id
- Admin-only `/broadcast`
  - Send any plain-text message to all users who have already started the bot
- User commands
  - `Latest`
  - `Last 7 Days`
  - `Search by Date`
  - Students can tap buttons instead of typing commands

## Setup

1. Create a bot with [@BotFather](https://t.me/BotFather)
2. Copy `.env.example` to `.env`
3. Put your bot token in `BOT_TOKEN`
4. Put your Telegram numeric user id in `ADMIN_USER_IDS`
5. Install dependencies:

```powershell
pip install -r requirements.txt
```

6. Run:

```powershell
python bot.py
```

## Notes

- `/latest` now shows each update id, so admins can use that id with `/delete`.
- Broadcast works only for chats that have already opened the bot at least once with `/start`.
- `Search by Date` asks the student to send a date in `YYYY-MM-DD` format.
- Student updates are shown in a short sentence with date, course name, module name, and a clickable `here` link.
- During `/update`, admin can type a date like `2026-04-15` or type `skip` to use today's date.
- Multiple admin ids can be added with commas:

```env
ADMIN_USER_IDS=123456789,987654321
```
