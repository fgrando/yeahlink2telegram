# yeahlink2telegram
Forward Yealink VoIP phone Action URL events to a Telegram chat. Python 3.8+, standard library only.

1. Create `mycredentials.json`: `{"TELEGRAM_TOKEN": "...", "TELEGRAM_CHAT_ID": "..."}`
   Token: create a bot with [@BotFather](https://t.me/BotFather). Chat ID: send your bot any message, open
   `https://api.telegram.org/bot<TOKEN>/getUpdates` and copy `"chat":{"id":...}` (group IDs are negative).
2. Run `python yeahlink2telegram.py` (`--test` sends one message and exits, `-v` for debug logs).
3. On the phone (Features -> Action URL), paste each URL into the field its path names (replace `<server>`):
```
http://<server>:8088/incoming_call?remote=$remote&name=$display_remote&local=$local&id=$call_id&ip=$ip
http://<server>:8088/missed_call?remote=$remote&name=$display_remote&local=$local&id=$call_id&ip=$ip
http://<server>:8088/outgoing_call?remote=$remote&name=$display_remote&local=$local&id=$call_id&ip=$ip
http://<server>:8088/call_established?remote=$remote&name=$display_remote&local=$local&id=$call_id&ip=$ip
http://<server>:8088/call_terminated?remote=$remote&name=$display_remote&local=$local&id=$call_id&ip=$ip
http://<server>:8088/off_hook?remote=$remote&name=$display_remote&local=$local&id=$call_id&ip=$ip
http://<server>:8088/on_hook?remote=$remote&name=$display_remote&local=$local&id=$call_id&ip=$ip
http://<server>:8088/log_on?remote=$remote&name=$display_remote&local=$local&id=$call_id&ip=$ip
http://<server>:8088/log_off?remote=$remote&name=$display_remote&local=$local&id=$call_id&ip=$ip
http://<server>:8088/register_failed?remote=$remote&name=$display_remote&local=$local&id=$call_id&ip=$ip
http://<server>:8088/setup_completed?remote=$remote&name=$display_remote&local=$local&id=$call_id&ip=$ip
http://<server>:8088/ip_change?remote=$remote&name=$display_remote&local=$local&id=$call_id&ip=$ip
http://<server>:8088/autop_finish?remote=$remote&name=$display_remote&local=$local&id=$call_id&ip=$ip
```
Any other field works too with any path name (e.g. `/dnd_on?...`) and gets a generic message.
Sent by default: incoming, missed, off/on hook, log on/off, register failed. Others are only logged unless
listed in `YN_EVENTS` (or `YN_EVENTS=*`).

Environment variables: `YN_LISTEN_HOST`/`YN_LISTEN_PORT` (default `0.0.0.0:8088`), `YN_ALLOWED_IPS` (phone IPs),
`YN_EVENTS` (events to send, `*` = all), `YN_CREDENTIALS` (credentials file path), `YN_STATUS_THROTTLE` (seconds).
