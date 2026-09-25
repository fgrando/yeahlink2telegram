# yeahlink2telegram
Forward Yealink VoIP phone Action URL events to a Telegram chat. Python 3.8+, standard library only.

1. Create `mycredentials.json`: `{"TELEGRAM_TOKEN": "...", "TELEGRAM_CHAT_ID": "..."}`
   Token: create a bot with [@BotFather](https://t.me/BotFather). Chat ID: send your bot any message, open
   `https://api.telegram.org/bot<TOKEN>/getUpdates` and copy `"chat":{"id":...}` (group IDs are negative).
2. Run `python yeahlink2telegram.py` (`--test` sends one message and exits, `-v` for debug logs).
3. On the phone (Features -> Action URL), point any event at the server; the path is the event name:
   `http://<server>:8088/incoming_call?remote=$remote&name=$display_remote&local=$local&id=$call_id&ip=$ip`

Known events (incoming, missed, answered, ended, outgoing, registered, register_failed, off_hook, on_hook, ...)
get a friendly message; any other event gets a generic one with its parameters.

Environment variables: `YN_LISTEN_HOST`/`YN_LISTEN_PORT` (default `0.0.0.0:8088`), `YN_ALLOWED_IPS` (phone IPs),
`YN_EVENTS` (events to send, `*` = all), `YN_CREDENTIALS` (credentials file path), `YN_STATUS_THROTTLE` (seconds).
