#!/usr/bin/env python3
"""
yeahlink2telegram.py - Yealink Action URL -> Telegram notification bridge.

Listens for the HTTP GET requests a Yealink phone fires via its "Action URL"
feature and forwards a short text message to a Telegram chat.

Standard library only, Python 3.8+.

Every Action URL field can point at this server. The event name is taken from
the "event" query parameter or, if absent, from the last path segment, so the
same query string can be pasted into every field and only the path changes:

  http://<server>:8088/incoming_call?remote=$remote&name=$display_remote&local=$local&id=$call_id&ip=$ip
  http://<server>:8088/missed_call?remote=$remote&name=$display_remote&local=$local&id=$call_id&ip=$ip
  http://<server>:8088/dnd_on?remote=$remote&name=$display_remote&local=$local&id=$call_id&ip=$ip
  ...

Known events get a friendly message; any other event gets a generic one listing
its parameters. Which events are sent is controlled by YN_EVENTS ("*" = all);
the rest are only logged.
"""

import argparse
import json
import logging
import os
import signal
import socket
import socketserver
import sys
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime

# --------------------------------------------------------------------------
# Configuration (all overridable via environment variables)
# --------------------------------------------------------------------------
LISTEN_HOST = os.environ.get("YN_LISTEN_HOST", "0.0.0.0")
LISTEN_PORT = int(os.environ.get("YN_LISTEN_PORT", "8088"))

# Comma-separated source IPs allowed to trigger notifications (the phone).
# Empty = accept from anyone (not recommended).
ALLOWED_IPS = {ip.strip() for ip in os.environ.get("YN_ALLOWED_IPS", "").split(",") if ip.strip()}

# Telegram credentials come from mycredentials.json (relative to the working
# directory, or an absolute path via YN_CREDENTIALS). Env vars are the
# fallback if the file is missing or incomplete.
CREDENTIALS_FILE = os.environ.get("YN_CREDENTIALS", "mycredentials.json")
TG_TOKEN = os.environ.get("YN_TG_TOKEN", "")
TG_CHAT = os.environ.get("YN_TG_CHAT", "")
_cred_error = None
try:
    with open(CREDENTIALS_FILE, "r") as f:
        cred = json.load(f)
        TG_TOKEN = cred["TELEGRAM_TOKEN"]
        TG_CHAT = str(cred["TELEGRAM_CHAT_ID"])
except (OSError, ValueError, KeyError) as exc:
    _cred_error = "%s: %r" % (CREDENTIALS_FILE, exc)

# Yealink's native event names -> the short names used below.
ALIASES = {
    "incoming_call": "incoming",
    "outgoing_call": "outgoing",
    "call_established": "answered",
    "call_terminated": "ended",
    "missed_call": "missed",
    "log_on": "registered",
    "log_off": "unregistered",
}

# Which events produce a notification ("*" = every event). Others are only logged.
NOTIFY_EVENTS = {ALIASES.get(e.strip().lower(), e.strip().lower()) for e in os.environ.get(
    "YN_EVENTS", "incoming,missed,registered,unregistered,register_failed,off_hook,on_hook").split(",") if e.strip()}

# Message templates. Fields: {who} caller, {line} " on <local>", {account}
# user/local line, {ip} " [<ip>]", {duration} ", duration m:ss", {stamp} time.
MESSAGES = {
    "incoming": "Incoming call from {who}{line} at {stamp}",
    "missed": "Missed call from {who}{line} at {stamp}",
    "answered": "Call answered: {who} at {stamp}",
    "ended": "Call ended: {who} at {stamp}{duration}",
    "outgoing": "Outgoing call to {who}{line} at {stamp}",
    "registered": "Line registered: {account}{ip} ({stamp})",
    "unregistered": "Line UNREGISTERED: {account}{ip} ({stamp})",
    "register_failed": "Registration FAILED: {account}{ip} ({stamp})",
    "setup_completed": "Phone started: {account}{ip} ({stamp})",
    "ip_change": "Phone IP changed: {account}{ip} ({stamp})",
    "autop_finish": "Auto-provisioning finished: {account}{ip} ({stamp})",
    "off_hook": "Handset picked up: {account} at {stamp}",
    "on_hook": "Handset hung up: {account} at {stamp}",
}

# A failed registration retries every 30 s; do not send more than one
# register_failed message within this many seconds.
STATUS_THROTTLE = int(os.environ.get("YN_STATUS_THROTTLE", "900"))

DEDUP_SECONDS = 10          # ignore identical event+call repeats within this window
CALL_STATE_TTL = 6 * 3600   # forget call state after this many seconds
NOTIFY_TIMEOUT = 15         # seconds for the Telegram HTTP call

log = logging.getLogger("yeahlink2telegram")

# --------------------------------------------------------------------------
# Telegram delivery
# --------------------------------------------------------------------------
def deliver(message):
    if not (TG_TOKEN and TG_CHAT):
        log.info("Telegram not configured, message: %s", message.replace("\n", " | "))
        return
    try:
        data = urllib.parse.urlencode({"chat_id": TG_CHAT, "text": message}).encode()
        url = "https://api.telegram.org/bot%s/sendMessage" % TG_TOKEN
        with urllib.request.urlopen(url, data=data, timeout=NOTIFY_TIMEOUT) as r:
            r.read()
        log.info("sent: %s", message.replace("\n", " | "))
    except Exception as exc:  # never let delivery kill the server
        log.error("delivery failed: %s", exc)


def notify_async(message):
    threading.Thread(target=deliver, args=(message,), daemon=True).start()


# --------------------------------------------------------------------------
# Event processing
# --------------------------------------------------------------------------
_lock = threading.Lock()
_calls = {}         # call_id -> {"start": ts, "answered": ts|None}
_recent = {}        # (event, key) -> ts
_throttled = {}     # event -> ts of last register_failed notification


def _purge(now):
    for k in [k for k, v in _calls.items() if now - v["start"] > CALL_STATE_TTL]:
        del _calls[k]
    for k in [k for k, v in _recent.items() if now - v > DEDUP_SECONDS]:
        del _recent[k]


def describe_party(number, display_name):
    """Human-readable caller description."""
    if not number or number.lower() in ("anonymous", "unknown", "restricted"):
        return "withheld number"
    if display_name and display_name != number:
        return "%s (%s)" % (display_name, number)
    return number


def fmt_duration(sec):
    sec = int(sec)
    return "%d:%02d" % (sec // 60, sec % 60) if sec < 3600 else \
        "%d:%02d:%02d" % (sec // 3600, (sec % 3600) // 60, sec % 60)


def process_event(event, params):
    """Update call state and return a message, or None if nothing to send."""
    remote = params.get("remote", "")
    local = params.get("local", "")
    call_id = params.get("id", "") or remote
    now = time.time()

    with _lock:
        _purge(now)
        key = (event, call_id)
        if key in _recent:
            return None  # duplicate burst
        _recent[key] = now

        if event == "register_failed":
            if now - _throttled.get(event, 0) < STATUS_THROTTLE:
                return None  # retries every 30 s - do not spam
            _throttled[event] = now

        duration = None
        if event in ("incoming", "outgoing"):
            _calls[call_id] = {"start": now, "answered": None}
        elif event == "answered":
            _calls.setdefault(call_id, {"start": now, "answered": None})["answered"] = now
        elif event == "ended":
            st = _calls.pop(call_id, None)
            if st and st["answered"]:
                duration = now - st["answered"]
        elif event == "missed":
            _calls.pop(call_id, None)

    if "*" not in NOTIFY_EVENTS and event not in NOTIFY_EVENTS:
        return None

    stamp = datetime.now().strftime("%H:%M:%S")
    template = MESSAGES.get(event)
    if template is None:
        details = ", ".join("%s=%s" % (k, v) for k, v in params.items() if v and k != "event")
        return "Phone event '%s' at %s%s" % (event, stamp, (": " + details) if details else "")
    return template.format(
        who=describe_party(remote, params.get("name", "")),
        line=(" on " + local) if local else "",
        account=params.get("user") or local or "line",
        ip=(" [%s]" % params["ip"]) if params.get("ip") else "",
        duration=(", duration " + fmt_duration(duration)) if duration is not None else "",
        stamp=stamp,
    )


# --------------------------------------------------------------------------
# Minimal, tolerant HTTP handling
# --------------------------------------------------------------------------
def parse_request_line(line):
    """
    Tolerant request-line parser. Yealink does not always URL-encode
    substituted values (a display name may contain raw spaces), which breaks
    strict HTTP parsers. We strip the method and the trailing HTTP version
    and keep everything in between as the target.
    """
    parts = line.split(" ", 1)
    if len(parts) != 2:
        return None, None
    method, rest = parts
    if rest.rsplit(" ", 1)[-1].upper().startswith("HTTP/"):
        rest = rest.rsplit(" ", 1)[0]
    return method.upper(), rest


def parse_target(target):
    """Return (event, params). Event comes from ?event= or the last path segment."""
    parts = urllib.parse.urlsplit(target)
    # Yealink sends E.164 numbers with a raw '+'. Treat it as a literal plus,
    # not as form-encoded space (raw spaces are preserved as-is anyway).
    query = parts.query.replace("+", "%2B")
    params = {}
    for k, v in urllib.parse.parse_qsl(query, keep_blank_values=True):
        v = v.strip()
        if v.startswith("$"):  # placeholder the phone did not substitute
            v = ""
        params[k.lower()] = v
    event = params.get("event") or urllib.parse.unquote(parts.path).rstrip("/").rsplit("/", 1)[-1]
    event = event.strip().lower()
    return ALIASES.get(event, event), params


class Handler(socketserver.StreamRequestHandler):
    timeout = 5

    def _reply(self, code, text):
        reason = {200: "OK", 400: "Bad Request", 403: "Forbidden", 405: "Method Not Allowed"}[code]
        body = (text + "\n").encode()
        head = ("HTTP/1.1 %d %s\r\nContent-Type: text/plain\r\n"
                "Content-Length: %d\r\nConnection: close\r\n\r\n") % (code, reason, len(body))
        try:
            self.wfile.write(head.encode() + body)
        except OSError:
            pass

    def handle(self):
        try:
            line = self.rfile.readline(8192).decode("utf-8", errors="replace").rstrip("\r\n")
            while True:  # drain headers
                h = self.rfile.readline(8192)
                if not h or h in (b"\r\n", b"\n"):
                    break
        except OSError:
            return

        client = self.client_address[0]
        if ALLOWED_IPS and client not in ALLOWED_IPS:
            log.warning("rejected request from %s", client)
            return self._reply(403, "forbidden")

        method, target = parse_request_line(line)
        if method != "GET":
            return self._reply(405 if method else 400, "GET only")

        event, params = parse_target(target)
        log.debug("%s %s -> %s %s", client, target, event, params)
        if not event:
            return self._reply(400, "missing event")

        msg = process_event(event, params)
        if msg:
            notify_async(msg)
        else:
            log.info("event '%s' (not notified)", event)
        self._reply(200, "ok")


class Server(socketserver.ThreadingMixIn, socketserver.TCPServer):
    allow_reuse_address = True
    daemon_threads = True


# --------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description="Yealink Action URL -> Telegram bridge")
    ap.add_argument("--test", action="store_true", help="send one test notification and exit")
    ap.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    args = ap.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    if not (TG_TOKEN and TG_CHAT):
        log.warning("Telegram credentials not configured (%s)", _cred_error or "empty values")

    if args.test:
        deliver("Test notification from yeahlink2telegram at %s" % datetime.now().strftime("%H:%M:%S"))
        return

    if not ALLOWED_IPS:
        log.warning("YN_ALLOWED_IPS is empty - accepting requests from any address")
    # systemd stops services with SIGTERM; turn it into a clean exit path.
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))

    host = socket.gethostname()
    with Server((LISTEN_HOST, LISTEN_PORT), Handler) as srv:
        log.info("listening on %s:%d, events=%s", LISTEN_HOST, LISTEN_PORT, ",".join(sorted(NOTIFY_EVENTS)))
        # Sent only after the port is bound, so it really means "ready".
        notify_async("Call notifier online on %s:%d (%s)"
                     % (host, LISTEN_PORT, datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
        try:
            srv.serve_forever()
        except (KeyboardInterrupt, SystemExit):
            pass
        finally:
            # Synchronous: the process is about to exit.
            deliver("Call notifier stopping on %s (%s)"
                    % (host, datetime.now().strftime("%Y-%m-%d %H:%M:%S")))


if __name__ == "__main__":
    main()
