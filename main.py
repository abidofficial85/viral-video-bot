#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Viral Video — Telegram bot + Mini App (stdlib only, no pip packages needed).

What it does
------------
* Bot (long polling):
    /start  -> video list + button to open the Mini App
    /admin  -> admin panel (ADMIN_ID only): stats, video list, delete
    /export -> admin backup of videos.json as a document
    Admin uploads a video by simply sending it to the bot with the title
    in the caption. Videos are stored as Telegram file_ids (not on disk),
    so they survive server restarts. Only the small videos.json index
    lives on disk — back it up with /export before redeploys.
* Mini App (this same process serves webapp/):
    User picks a video -> watches a Monetag Rewarded Interstitial ad
    (show_<ZONE_ID>() from the Monetag SDK tag) -> server verifies the
    Telegram identity (initData signature) and that the ad result was
    'valued' -> issues a one-time, 10-minute stream token -> the video
    plays from /api/stream, proxied from Telegram's file servers with
    Range support (seeking works).
* Monetag postbacks: POST /api/monetag-postback is logged to
  postbacks.log so earnings/events can be cross-checked. The immediate
  unlock trusts the SDK's 'valued' result + verified Telegram identity;
  the postback log is the server-side audit trail.

Environment variables (see .env.example):
    BOT_TOKEN        Telegram bot token (from @BotFather, fresh token!)
    ADMIN_ID         Your Telegram user id (from @userinfobot)
    WEBAPP_URL       Public https URL of this service (Render URL)
    MONETAG_ZONE_ID  Main zone id from the Monetag dashboard (optional:
                     without it the app runs in TEST MODE with a fake ad)
    MONETAG_SDK_URL  SDK script URL from the Monetag dashboard (optional)
    PORT             Port to listen on (Render sets this automatically)
    DATA_DIR         Where videos.json / postbacks.log live (default ".")
    TELEGRAM_API_BASE  For testing only (default https://api.telegram.org)

NEVER put BOT_TOKEN in chat, in git, or in this file. Render's
Environment Variables page is the only place it should live.
"""

import hashlib
import hmac
import json
import mimetypes
import os
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# ---------------------------------------------------------------- config

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
ADMIN_ID = os.environ.get("ADMIN_ID", "")
try:
    ADMIN_ID_INT = int(ADMIN_ID) if ADMIN_ID else 0
except ValueError:
    ADMIN_ID_INT = 0
WEBAPP_URL = os.environ.get("WEBAPP_URL", "").rstrip("/")
MONETAG_ZONE_ID = os.environ.get("MONETAG_ZONE_ID", "")
MONETAG_SDK_URL = os.environ.get("MONETAG_SDK_URL", "")
PORT = int(os.environ.get("PORT", "8080"))
DATA_DIR = os.environ.get("DATA_DIR", ".")
API_BASE = os.environ.get("TELEGRAM_API_BASE", "https://api.telegram.org").rstrip("/")
FILE_BASE = API_BASE + "/file"

TEST_MODE = not MONETAG_ZONE_ID  # no Monetag configured -> fake ad, full test flow
VIDEOS_PATH = os.path.join(DATA_DIR, "videos.json")
POSTBACKS_PATH = os.path.join(DATA_DIR, "postbacks.log")
WEBAPP_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "webapp")

STORE_LOCK = threading.Lock()
TOKENS = {}  # token -> {user_id, video_id, expires, used}


def log(msg):
    print("[viral-video] %s" % msg, flush=True)


# ---------------------------------------------------------------- storage

def load_videos():
    try:
        with open(VIDEOS_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict) and isinstance(data.get("videos"), list):
            return data["videos"]
    except (OSError, ValueError):
        pass
    return []


def save_videos(videos):
    tmp = VIDEOS_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"videos": videos}, f, ensure_ascii=False, indent=2)
    os.replace(tmp, VIDEOS_PATH)


def find_video(video_id):
    for v in load_videos():
        if v.get("id") == video_id:
            return v
    return None


def public_video(v):
    return {
        "id": v.get("id"),
        "title": v.get("title"),
        "duration": v.get("duration", 0),
        "views": v.get("views", 0),
    }


def fmt_duration(seconds):
    try:
        seconds = int(seconds)
    except (TypeError, ValueError):
        return ""
    if seconds <= 0:
        return ""
    return "%d:%02d" % (seconds // 60, seconds % 60)


# ---------------------------------------------------------------- Telegram API

def tg_api(method, payload=None, timeout=40):
    """Call the Bot API. Returns the `result` field or raises RuntimeError."""
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN is not set")
    url = "%s/bot%s/%s" % (API_BASE, BOT_TOKEN, method)
    data = json.dumps(payload or {}).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.load(resp)
    except urllib.error.HTTPError as e:
        try:
            body = json.load(e)
        except ValueError:
            body = {}
        raise RuntimeError("Telegram API %s failed: %s" %
                           (method, body.get("description", "HTTP %s" % e.code)))
    if not body.get("ok"):
        raise RuntimeError("Telegram API %s failed: %s" %
                           (method, body.get("description", "unknown")))
    return body.get("result")


def tg_send_message(chat_id, text, reply_markup=None):
    payload = {"chat_id": chat_id, "text": text}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    return tg_api("sendMessage", payload)


def tg_answer_callback(callback_id, text=""):
    return tg_api("answerCallbackQuery",
                  {"callback_query_id": callback_id, "text": text})


def tg_send_document(chat_id, filename, content_bytes, caption=""):
    """Multipart upload (stdlib only)."""
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN is not set")
    boundary = "----viralvideo" + secrets.token_hex(8)
    body = []
    body.append(("--%s\r\n" % boundary).encode())
    body.append(('Content-Disposition: form-data; name="chat_id"\r\n\r\n').encode())
    body.append(("%s\r\n" % chat_id).encode())
    if caption:
        body.append(("--%s\r\n" % boundary).encode())
        body.append(('Content-Disposition: form-data; name="caption"\r\n\r\n').encode())
        body.append((caption + "\r\n").encode())
    body.append(("--%s\r\n" % boundary).encode())
    body.append(('Content-Disposition: form-data; name="document"; '
                 'filename="%s"\r\n' % filename).encode())
    body.append(("Content-Type: application/json\r\n\r\n").encode())
    body.append(content_bytes)
    body.append(("\r\n--%s--\r\n" % boundary).encode())
    url = "%s/bot%s/sendDocument" % (API_BASE, BOT_TOKEN)
    req = urllib.request.Request(
        url, data=b"".join(body),
        headers={"Content-Type": "multipart/form-data; boundary=%s" % boundary})
    with urllib.request.urlopen(req, timeout=60) as resp:
        result = json.load(resp)
    if not result.get("ok"):
        raise RuntimeError("sendDocument failed: %s" % result.get("description"))
    return result.get("result")


# ---------------------------------------------------------------- bot logic

def video_list_keyboard():
    videos = load_videos()
    rows = []
    for v in videos:
        label = v.get("title") or "Untitled"
        dur = fmt_duration(v.get("duration", 0))
        if dur:
            label = "%s (%s)" % (label, dur)
        rows.append([{"text": "🎬 " + label,
                      "callback_data": "watch:" + v["id"]}])
    if WEBAPP_URL:
        rows.append([{"text": "📱 Mini App খুলুন",
                      "web_app": {"url": WEBAPP_URL}}])
    return {"inline_keyboard": rows} if rows else None


def handle_start(chat_id, user_id):
    videos = load_videos()
    if not videos:
        text = ("👋 স্বাগতম Viral Video-তে!\n\n"
                "এখনো কোনো ভিডিও যোগ হয়নি। একটু পরে আবার দেখুন।")
        tg_send_message(chat_id, text)
        return
    text = ("🎬 Viral Video\n\n"
            "একটা ভিডিও বাছুন — Mini App-এ এড দেখে তারপর ভিডিওটি চলবে।")
    tg_send_message(chat_id, text, video_list_keyboard())


def handle_admin(chat_id):
    videos = load_videos()
    total_views = sum(int(v.get("views", 0)) for v in videos)
    lines = ["⚙️ অ্যাডমিন প্যানেল — Viral Video",
             "",
             "মোট ভিডিও: %d" % len(videos),
             "মোট ভিউ: %d" % total_views,
             "",
             "📤 ভিডিও যোগ করতে: সরাসরি ভিডিও পাঠান, caption-এ টাইটেল লিখুন।",
             "💾 ব্যাকআপ নিতে: /export",
             "♻️ ব্যাকআপ ফেরাতে: videos.json ফাইলটি এখানে পাঠান।",
             "",
             "ভিডিও ডিলিট করতে নিচের বাটন চাপুন:"]
    rows = []
    for v in videos:
        rows.append([{"text": "🗑 " + (v.get("title") or "Untitled"),
                      "callback_data": "del:" + v["id"]}])
    markup = {"inline_keyboard": rows} if rows else None
    tg_send_message(chat_id, "\n".join(lines), markup)


def handle_export(chat_id):
    with STORE_LOCK:
        videos = load_videos()
    blob = json.dumps({"videos": videos}, ensure_ascii=False, indent=2)
    tg_send_document(chat_id, "videos.json", blob.encode("utf-8"),
                     caption="Viral Video ব্যাকআপ — %dটি ভিডিও" % len(videos))


def handle_video_upload(message):
    video = message.get("video") or {}
    file_id = video.get("file_id")
    if not file_id:
        return
    title = (message.get("caption") or "").strip() or "Untitled"
    with STORE_LOCK:
        videos = load_videos()
        videos.append({
            "id": secrets.token_hex(6),
            "file_id": file_id,
            "title": title,
            "duration": int(video.get("duration") or 0),
            "views": 0,
            "added_at": int(time.time()),
        })
        save_videos(videos)
    tg_send_message(message["chat"]["id"],
                    "✅ ভিডিও যোগ হয়েছে!\n🎬 %s" % title)


def handle_restore_document(message):
    doc = message.get("document") or {}
    if not doc.get("file_name", "").endswith(".json"):
        tg_send_message(message["chat"]["id"],
                        "এটা JSON ফাইল নয়। videos.json ব্যাকআপ ফাইলটি পাঠান।")
        return
    try:
        info = tg_api("getFile", {"file_id": doc["file_id"]})
        path = info.get("file_path", "")
        url = "%s/bot%s/%s" % (FILE_BASE, BOT_TOKEN, path)
        with urllib.request.urlopen(url, timeout=60) as resp:
            data = json.load(resp)
        videos = data.get("videos")
        if not isinstance(videos, list):
            raise ValueError("bad backup format")
        with STORE_LOCK:
            save_videos(videos)
        tg_send_message(message["chat"]["id"],
                        "✅ ব্যাকআপ ফেরানো হয়েছে — %dটি ভিডিও।" % len(videos))
    except Exception as e:  # noqa: BLE001 - report to admin, keep polling
        tg_send_message(message["chat"]["id"],
                        "❌ ব্যাকআপ ফেরানো যায়নি: %s" % e)


def handle_watch_callback(callback_id, chat_id, video_id):
    v = find_video(video_id)
    if not v:
        tg_answer_callback(callback_id, "ভিডিওটি পাওয়া যায়নি।")
        return
    tg_answer_callback(callback_id)
    if WEBAPP_URL:
        url = "%s?startapp=video_%s" % (WEBAPP_URL, video_id)
        markup = {"inline_keyboard": [[
            {"text": "▶️ এড দেখে ভিডিও দেখুন", "web_app": {"url": url}}]]}
        tg_send_message(chat_id,
                        "🎬 %s\n\nনিচের বাটনে চাপ দিলে Mini App খুলবে — "
                        "সেখানে এড দেখলেই ভিডিও চলবে।" % (v.get("title") or ""),
                        markup)
    else:
        tg_send_message(chat_id,
                        "⚠️ Mini App-এর লিংক (WEBAPP_URL) এখনো বসানো হয়নি। "
                        "Render-এ WEBAPP_URL সেট করে আবার Deploy দিন।")


def handle_delete_callback(callback_id, chat_id, video_id):
    with STORE_LOCK:
        videos = load_videos()
        kept = [v for v in videos if v.get("id") != video_id]
        if len(kept) == len(videos):
            tg_answer_callback(callback_id, "ভিডিওটি পাওয়া যায়নি।")
            return
        save_videos(kept)
    tg_answer_callback(callback_id, "ডিলিট হয়েছে।")
    tg_send_message(chat_id, "🗑 ভিডিও ডিলিট হয়েছে।")


def handle_message(message):
    chat_id = message["chat"]["id"]
    user_id = message.get("from", {}).get("id", 0)
    text = (message.get("text") or "").strip()
    if "video" in message:
        if user_id == ADMIN_ID_INT:
            handle_video_upload(message)
        else:
            tg_send_message(chat_id, "ভিডিও আপলোড শুধু অ্যাডমিন করতে পারেন।")
        return
    if "document" in message:
        if user_id == ADMIN_ID_INT:
            handle_restore_document(message)
        return
    if text.startswith("/start"):
        handle_start(chat_id, user_id)
    elif text.startswith("/admin"):
        if user_id == ADMIN_ID_INT:
            handle_admin(chat_id)
        else:
            tg_send_message(chat_id, "এই কমান্ড শুধু অ্যাডমিনের জন্য।")
    elif text.startswith("/export"):
        if user_id == ADMIN_ID_INT:
            handle_export(chat_id)
        else:
            tg_send_message(chat_id, "এই কমান্ড শুধু অ্যাডমিনের জন্য।")
    elif text:
        tg_send_message(chat_id,
                        "👋 /start চাপ দিয়ে ভিডিও লিস্ট দেখুন।")


def handle_update(update):
    if "callback_query" in update:
        cb = update["callback_query"]
        data = cb.get("data", "")
        chat_id = cb["message"]["chat"]["id"]
        user_id = cb.get("from", {}).get("id", 0)
        if data.startswith("watch:"):
            handle_watch_callback(cb["id"], chat_id, data[6:])
        elif data.startswith("del:"):
            if user_id == ADMIN_ID_INT:
                handle_delete_callback(cb["id"], chat_id, data[4:])
            else:
                tg_answer_callback(cb["id"], "শুধু অ্যাডমিন ডিলিট করতে পারেন।")
    elif "message" in update:
        handle_message(update["message"])


def bot_polling_loop():
    if not BOT_TOKEN:
        log("BOT_TOKEN not set — bot polling disabled, web server only.")
        return
    if not ADMIN_ID_INT:
        log("WARNING: ADMIN_ID not set — admin commands will not work.")
    offset = 0
    log("Bot polling started.")
    while True:
        try:
            updates = tg_api("getUpdates", {
                "offset": offset, "timeout": 30,
                "allowed_updates": ["message", "callback_query"]}, timeout=40)
            for upd in updates:
                offset = max(offset, int(upd.get("update_id", 0)) + 1)
                try:
                    handle_update(upd)
                except Exception as e:  # noqa: BLE001 - one bad update must not kill polling
                    log("update handler error: %s" % e)
        except Exception as e:  # noqa: BLE001 - polling must survive network errors
            log("polling error (retrying in 5s): %s" % e)
            time.sleep(5)


# ---------------------------------------------------------------- initData check

def verify_init_data(init_data):
    """Verify Telegram WebApp initData. Returns user dict or None."""
    if not init_data or not BOT_TOKEN:
        return None
    try:
        pairs = urllib.parse.parse_qsl(init_data, keep_blank_values=True)
    except ValueError:
        return None
    data = dict(pairs)
    received_hash = data.pop("hash", None)
    if not received_hash:
        return None
    check_string = "\n".join("%s=%s" % (k, data[k]) for k in sorted(data))
    secret = hmac.new(b"WebAppData", BOT_TOKEN.encode("utf-8"),
                      hashlib.sha256).digest()
    calc = hmac.new(secret, check_string.encode("utf-8"),
                    hashlib.sha256).hexdigest()
    if not hmac.compare_digest(calc, received_hash):
        return None
    try:
        auth_date = int(data.get("auth_date", "0"))
    except ValueError:
        return None
    if abs(time.time() - auth_date) > 86400:  # older than 24h
        return None
    try:
        return json.loads(data.get("user", "{}"))
    except ValueError:
        return None


# ---------------------------------------------------------------- HTTP server

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # keep stdout clean
        pass

    # -- helpers ------------------------------------------------------
    def send_json(self, status, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def read_json_body(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > 65536:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except ValueError:
            return {}

    # -- routes -------------------------------------------------------
    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        query = urllib.parse.parse_qs(parsed.query)
        if path == "/api/config":
            self.send_json(200, {
                "appName": "Viral Video",
                "testMode": TEST_MODE,
                "zoneId": MONETAG_ZONE_ID or None,
                "sdkUrl": MONETAG_SDK_URL or None,
            })
        elif path == "/api/videos":
            self.send_json(200, {"videos": [public_video(v)
                                            for v in load_videos()]})
        elif path == "/api/stream":
            self.handle_stream(query.get("token", [""])[0])
        elif path in ("/", "/index.html"):
            self.serve_static("index.html")
        elif path.startswith("/"):
            self.serve_static(path.lstrip("/"))
        else:
            self.send_json(404, {"error": "not found"})

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/api/unlock":
            self.handle_unlock()
        elif parsed.path == "/api/monetag-postback":
            self.handle_postback()
        else:
            self.send_json(404, {"error": "not found"})

    # -- static -------------------------------------------------------
    def serve_static(self, name):
        if not name or ".." in name or name.startswith("/"):
            self.send_json(404, {"error": "not found"})
            return
        full = os.path.normpath(os.path.join(WEBAPP_DIR, name))
        if not full.startswith(os.path.normpath(WEBAPP_DIR)):
            self.send_json(404, {"error": "not found"})
            return
        if not os.path.isfile(full):
            self.send_json(404, {"error": "not found"})
            return
        mime, _ = mimetypes.guess_type(full)
        with open(full, "rb") as f:
            body = f.read()
        self.send_response(200)
        self.send_header("Content-Type", mime or "application/octet-stream")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # -- unlock -------------------------------------------------------
    def handle_unlock(self):
        body = self.read_json_body()
        video_id = body.get("videoId", "")
        reward = body.get("rewardEventType", "")
        ymid = body.get("ymid", "")
        user = verify_init_data(body.get("initData", ""))
        if not user:
            self.send_json(403, {"error": "Telegram পরিচয় যাচাই হয়নি। "
                                            "Mini App-টি Telegram-এর ভেতর "
                                            "থেকে খুলুন।"})
            return
        if not TEST_MODE and reward != "valued":
            # Monetag: only a 'valued' ad result earns the reward.
            self.send_json(403, {"error": "এডটি পুরো দেখা হয়নি (valued হয়নি)। "
                                            "আবার এড দেখুন।"})
            return
        video = find_video(video_id)
        if not video:
            self.send_json(404, {"error": "ভিডিওটি পাওয়া যায়নি।"})
            return
        token = secrets.token_urlsafe(24)
        TOKENS[token] = {
            "user_id": user.get("id"),
            "video_id": video_id,
            "expires": time.time() + 600,  # 10 minutes, single use
            "used": False,
        }
        log("unlock: user=%s video=%s ymid=%s test=%s" %
            (user.get("id"), video_id, ymid, TEST_MODE))
        self.send_json(200, {"token": token})

    # -- postback audit log -------------------------------------------
    def handle_postback(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length > 0 else b""
        try:
            with open(POSTBACKS_PATH, "a", encoding="utf-8") as f:
                f.write(json.dumps({
                    "ts": int(time.time()),
                    "raw": raw.decode("utf-8", "replace"),
                }, ensure_ascii=False) + "\n")
        except OSError as e:
            log("postback log failed: %s" % e)
        self.send_json(200, {"ok": True})

    # -- stream proxy ---------------------------------------------------
    def handle_stream(self, token):
        entry = TOKENS.get(token)
        if not entry or entry.get("used") or time.time() > entry["expires"]:
            self.send_json(403, {"error": "লিংকটির মেয়াদ শেষ বা ব্যবহার হয়ে "
                                            "গেছে। আবার এড দেখুন।"})
            return
        entry["used"] = True
        video = find_video(entry["video_id"])
        if not video:
            self.send_json(404, {"error": "ভিডিওটি পাওয়া যায়নি।"})
            return
        try:
            info = tg_api("getFile", {"file_id": video["file_id"]}, timeout=30)
            file_url = "%s/bot%s/%s" % (FILE_BASE, BOT_TOKEN,
                                        info.get("file_path", ""))
        except Exception as e:  # noqa: BLE001 - report cleanly
            self.send_json(502, {"error": "ভিডিও আনতে সমস্যা: %s" % e})
            return
        # Count the view on first successful stream start.
        try:
            with STORE_LOCK:
                videos = load_videos()
                for v in videos:
                    if v.get("id") == video["id"]:
                        v["views"] = int(v.get("views", 0)) + 1
                save_videos(videos)
        except OSError:
            pass
        # Proxy with Range support (seeking). Stream in chunks, never
        # load the whole file into memory.
        headers = {}
        if self.headers.get("Range"):
            headers["Range"] = self.headers["Range"]
        req = urllib.request.Request(file_url, headers=headers)
        try:
            upstream = urllib.request.urlopen(req, timeout=120)
        except urllib.error.HTTPError as e:
            self.send_response(e.code)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        except Exception as e:  # noqa: BLE001
            self.send_json(502, {"error": "ভিডিও স্ট্রিমে সমস্যা: %s" % e})
            return
        status = upstream.status
        self.send_response(status)
        ctype = upstream.headers.get("Content-Type") or "video/mp4"
        self.send_header("Content-Type", ctype)
        if upstream.headers.get("Content-Range"):
            self.send_header("Content-Range",
                             upstream.headers["Content-Range"])
        if upstream.headers.get("Content-Length"):
            self.send_header("Content-Length",
                             upstream.headers["Content-Length"])
        self.send_header("Accept-Ranges", "bytes")
        self.end_headers()
        try:
            while True:
                chunk = upstream.read(65536)
                if not chunk:
                    break
                self.wfile.write(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            upstream.close()


def main():
    if not BOT_TOKEN:
        log("WARNING: BOT_TOKEN not set — bot disabled until it is set.")
    if TEST_MODE:
        log("TEST MODE: no MONETAG_ZONE_ID — Mini App will use a fake ad "
            "so the full flow can be tested before Monetag is configured.")
    threading.Thread(target=bot_polling_loop, daemon=True).start()
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    log("Serving on port %d (webapp dir: %s)" % (PORT, WEBAPP_DIR))
    server.serve_forever()


if __name__ == "__main__":
    main()
