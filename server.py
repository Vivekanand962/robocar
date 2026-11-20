#!/usr/bin/env python3
"""
RoboCar Cloud - runs on Render (or any host). No laptop needed.

  Car ESP32    --wss-->  /ws/car?key=CAR_KEY   (receives commands, sends status)
  ESP32-CAM    --wss-->  /ws/cam?key=CAR_KEY   (sends JPEG frames while someone watches)
  Visitors     --https-> /          ask for access, then drive
  Owner        --https-> /admin     approve / deny / revoke / emergency stop
  Telegram bot (optional) with the same controls

Environment variables (set in Render dashboard):
  ADMIN_PASSWORD      owner password for /admin and Telegram /login
  CAR_KEY             secret shared with both ESP32 sketches
  TELEGRAM_BOT_TOKEN  optional, from @BotFather
  TG_ADMIN_IDS        optional, comma separated Telegram chat ids that are always owner
"""
import asyncio
import hashlib
import hmac
import json
import os
import re
import secrets
import time

from aiohttp import ClientSession, ClientTimeout, WSMsgType, web

BASE = os.path.dirname(os.path.abspath(__file__))
WEB = os.path.join(BASE, "web")
DATA_PATH = os.environ.get("DATA_FILE", os.path.join(BASE, "data.json"))
PORT = int(os.environ.get("PORT", "10000"))
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD") or secrets.token_urlsafe(9)
CAR_KEY = os.environ.get("CAR_KEY") or secrets.token_urlsafe(12)
TG_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
TG_FIXED_ADMINS = {int(x) for x in re.findall(r"-?\d+", os.environ.get("TG_ADMIN_IDS", ""))}
PUBLIC_URL = (os.environ.get("PUBLIC_URL") or os.environ.get("RENDER_EXTERNAL_URL") or "").rstrip("/")
DRIVER_LOCK_S = 5
DRIVER_GONE_S = 2.5
MAX_PENDING = 30
CAM_FPS = int(os.environ.get("CAM_FPS", "8"))


def log(*a):
    print(time.strftime("[%H:%M:%S]"), *a, flush=True)


def now():
    return time.time()


# ============================================================ storage
class Store:
    def __init__(self):
        self.data = {"users": {}, "tg_admins": []}
        try:
            with open(DATA_PATH, encoding="utf-8") as f:
                self.data.update(json.load(f))
        except FileNotFoundError:
            pass
        except Exception as e:
            log("data file unreadable:", e)

    def save(self):
        try:
            tmp = DATA_PATH + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.data, f)
            os.replace(tmp, DATA_PATH)
        except Exception as e:
            log("save failed:", e)

    @staticmethod
    def th(token):
        return hashlib.sha256(token.encode()).hexdigest()

    def users(self):
        return self.data["users"]

    def by_token(self, token):
        if not token:
            return None
        h = self.th(token)
        for u in self.users().values():
            if hmac.compare_digest(u["th"], h):
                return u
        return None

    def get(self, uid):
        return self.users().get(uid)

    def create(self, token, name, msg, ip):
        uid = secrets.token_hex(4)
        self.users()[uid] = {"id": uid, "th": self.th(token), "name": name, "msg": msg, "ip": ip,
                             "status": "pending", "created": now(), "requested": now(),
                             "expires": 0, "last_seen": 0}
        self.save()
        return self.users()[uid]

    @staticmethod
    def has_access(u):
        return bool(u) and u["status"] == "approved" and (not u["expires"] or u["expires"] > now())

    def set_status(self, uid, status, minutes=0):
        u = self.get(uid)
        if u:
            u["status"] = status
            if status == "approved":
                u["expires"] = now() + minutes * 60 if minutes else 0
            self.save()
        return u

    def pending_count(self):
        return sum(1 for u in self.users().values() if u["status"] == "pending")

    def tg_admins(self):
        return TG_FIXED_ADMINS | set(self.data.get("tg_admins", []))


STORE = Store()
ADMIN_SESSIONS = set()
STARTED = time.time()


# ============================================================ car link
class Car:
    def __init__(self):
        self.ws = None
        self.status = {}
        self.status_time = 0
        self.health = {}
        self.health_time = 0
        self.connected_at = 0
        self.ctrl_uid = None
        self.ctrl_name = ""
        self.ctrl_last = 0

    @property
    def online(self):
        return self.ws is not None and not self.ws.closed and now() - self.status_time < 3

    def error(self):
        if self.ws is None or self.ws.closed:
            return "car is not connected to the cloud"
        if now() - self.status_time >= 3:
            return "car connected but not sending status"
        return ""

    async def send(self, text):
        if self.ws is None or self.ws.closed:
            raise ConnectionError("car offline")
        await self.ws.send_str(text)

    async def stop(self, why):
        log("STOP car:", why)
        for m in ("mode?m=manual", "cmd?go=S&spd=80"):
            try:
                await self.send(m)
            except Exception:
                pass

    def claim(self, uid, name):
        busy = self.ctrl_uid and self.ctrl_uid != uid and now() - self.ctrl_last < DRIVER_LOCK_S
        if busy and uid != "admin":
            return self.ctrl_name
        if self.ctrl_uid != uid:
            log("Control ->", name)
        self.ctrl_uid, self.ctrl_name, self.ctrl_last = uid, name, now()
        return None

    def touch(self, uid):
        if self.ctrl_uid == uid:
            self.ctrl_last = now()

    async def drop(self, uid, why):
        if self.ctrl_uid == uid:
            self.ctrl_uid, self.ctrl_name = None, ""
            await self.stop(why)

    def driver(self):
        if self.ctrl_uid and (now() - self.ctrl_last < DRIVER_LOCK_S or self.status.get("mode", "MANUAL") != "MANUAL"):
            return self.ctrl_uid, self.ctrl_name
        return None, ""


CAR = Car()


# ============================================================ camera link
class Cam:
    def __init__(self):
        self.ws = None
        self.frame = None
        self.seq = 0
        self.frame_time = 0
        self.cond = asyncio.Condition()
        self.last_want = 0
        self.streaming = False
        self.health = {}
        self.health_time = 0
        self.connected_at = 0
        self.frame_times = []   # recent frame arrival times, for fps

    def fps(self):
        t = now()
        return round(sum(1 for x in self.frame_times if t - x < 2) / 2, 1)

    def connected(self):
        return self.ws is not None and not self.ws.closed

    def error(self):
        if not self.connected():
            return "camera is not connected to the cloud"
        if not self.streaming:
            return "idle"
        if now() - self.frame_time > 3:
            return "waiting for frames"
        return ""

    async def want(self):
        self.last_want = now()
        if self.connected() and not self.streaming:
            self.streaming = True
            try:
                await self.ws.send_str(f"start {CAM_FPS}")
                log("Camera: start")
            except Exception:
                self.streaming = False

    async def push(self, data):
        self.frame_times = [x for x in self.frame_times[-40:] if now() - x < 2] + [now()]
        async with self.cond:
            self.frame, self.frame_time = data, now()
            self.seq += 1
            self.cond.notify_all()

    async def next_frame(self, after, timeout=5):
        async with self.cond:
            try:
                await asyncio.wait_for(self.cond.wait_for(lambda: self.seq != after and self.frame), timeout)
            except asyncio.TimeoutError:
                pass
            return self.seq, (self.frame if self.seq != after else None)


CAM = Cam()


# ============================================================ telegram
HELP = ("Commands:\n/users - active & pending users\n/status - car status + link\n"
        "/stop - emergency stop\n/revokeall - kick everyone\n/link - public link\n/logout")


class Telegram:
    def __init__(self, token):
        self.api = f"https://api.telegram.org/bot{token}/"
        self.ok = False
        self.error = ""
        self.session = None

    async def call(self, method, http_timeout=15, **params):
        async with self.session.post(self.api + method, json=params,
                                     timeout=ClientTimeout(total=http_timeout)) as r:
            return await r.json()

    async def safe(self, method, **params):
        try:
            return await self.call(method, **params)
        except Exception as e:
            log("Telegram", method, "failed:", e)

    async def broadcast(self, text, keyboard=None):
        for chat in STORE.tg_admins():
            p = {"chat_id": chat, "text": text, "disable_web_page_preview": True}
            if keyboard:
                p["reply_markup"] = {"inline_keyboard": keyboard}
            await self.safe("sendMessage", **p)

    @staticmethod
    def request_keyboard(uid):
        return [[{"text": "Allow 15 min", "callback_data": f"ap:{uid}:15"},
                 {"text": "Allow 1 hour", "callback_data": f"ap:{uid}:60"}],
                [{"text": "Allow always", "callback_data": f"ap:{uid}:0"},
                 {"text": "Deny", "callback_data": f"dn:{uid}"}]]

    async def notify_request(self, u):
        await self.broadcast(f"\U0001F697 Access request\nName: {u['name']}\nMessage: {u['msg'] or '-'}\nIP: {u['ip']}",
                             self.request_keyboard(u["id"]))

    async def handle_message(self, m):
        chat = m["chat"]["id"]
        text = (m.get("text") or "").strip()
        is_admin = chat in STORE.tg_admins()
        if text.startswith("/login"):
            await self.safe("deleteMessage", chat_id=chat, message_id=m["message_id"])
            if hmac.compare_digest(text[6:].strip().encode(), ADMIN_PASSWORD.encode()):
                if chat not in STORE.data.setdefault("tg_admins", []):
                    STORE.data["tg_admins"].append(chat)
                    STORE.save()
                await self.safe("sendMessage", chat_id=chat, text=(
                    "Logged in as car owner.\n\nTo stay logged in after the server restarts, add this in "
                    f"Render > Environment:\nTG_ADMIN_IDS = {chat}\n\n" + HELP))
            else:
                await asyncio.sleep(1)
                await self.safe("sendMessage", chat_id=chat, text="Wrong password.")
            return
        if not is_admin:
            await self.safe("sendMessage", chat_id=chat,
                            text="This bot controls a private robot car.\nOwner: send /login <admin password>")
            return
        cmd = text.split()[0].split("@")[0].lower() if text else ""
        if cmd in ("/users", "/pending"):
            rows, kb = [], []
            for u in STORE.users().values():
                if u["status"] == "approved" and STORE.has_access(u):
                    rows.append(f"- {u['name']} [approved, {describe_expiry(u)}]")
                    kb.append([{"text": f"Revoke {u['name'][:20]}", "callback_data": f"rv:{u['id']}"}])
                elif u["status"] == "pending":
                    rows.append(f"- {u['name']} [pending]")
                    kb += self.request_keyboard(u["id"])
            await self.safe("sendMessage", chat_id=chat, text="\n".join(rows) or "No active or pending users.",
                            reply_markup={"inline_keyboard": kb})
        elif cmd == "/revokeall":
            n = await revoke_all()
            await self.safe("sendMessage", chat_id=chat, text=f"Revoked {n} user(s). Car stopped.")
        elif cmd == "/stop":
            await CAR.stop("telegram /stop")
            await self.safe("sendMessage", chat_id=chat, text="\U0001F6D1 Stop sent to car.")
        elif cmd == "/link":
            await self.safe("sendMessage", chat_id=chat, text=link_text())
        elif cmd == "/status":
            s = CAR.status
            _, dn = CAR.driver()
            await self.safe("sendMessage", chat_id=chat, text=(
                f"Car: {'ONLINE' if CAR.online else 'OFFLINE - ' + CAR.error()}\n"
                f"Camera: {'connected' if CAM.connected() else 'not connected'}\n"
                f"Mode: {s.get('mode', '-')}  Battery: {s.get('bat', '-')} V\n"
                f"Driver: {dn or 'nobody'}\n{link_text()}"))
        elif cmd == "/logout":
            STORE.data["tg_admins"] = [c for c in STORE.data.get("tg_admins", []) if c != chat]
            STORE.save()
            await self.safe("sendMessage", chat_id=chat, text="Logged out (ids in TG_ADMIN_IDS stay owner).")
        else:
            await self.safe("sendMessage", chat_id=chat, text=HELP)

    async def handle_callback(self, q):
        chat = q["message"]["chat"]["id"]
        if chat not in STORE.tg_admins():
            await self.safe("answerCallbackQuery", callback_query_id=q["id"], text="Not authorized")
            return
        parts = q.get("data", "").split(":")
        result = await admin_action({"ap": "approve", "dn": "deny", "rv": "revoke"}.get(parts[0], ""),
                                    parts[1] if len(parts) > 1 else "",
                                    int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 0)
        await self.safe("answerCallbackQuery", callback_query_id=q["id"], text=result[:190])
        await self.safe("editMessageText", chat_id=chat, message_id=q["message"]["message_id"],
                        text=q["message"].get("text", "") + f"\n\n=> {result}")

    async def loop(self):
        self.session = ClientSession()
        offset = 0
        while True:
            try:
                r = await self.call("getUpdates", http_timeout=40, offset=offset, timeout=30)
                self.ok, self.error = bool(r.get("ok")), r.get("description", "")
                if not self.ok:
                    await asyncio.sleep(10)
                for up in r.get("result", []):
                    offset = up["update_id"] + 1
                    try:
                        if "message" in up:
                            await self.handle_message(up["message"])
                        elif "callback_query" in up:
                            await self.handle_callback(up["callback_query"])
                    except Exception as e:
                        log("Telegram update error:", e)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.ok, self.error = False, str(e)
                await asyncio.sleep(5)


TG = Telegram(TG_TOKEN) if TG_TOKEN else None


def tg_notify(coro_fn, *args):
    if TG and TG.session:
        asyncio.get_running_loop().create_task(coro_fn(*args))


# ============================================================ shared actions
def link_text():
    return f"Public link: {PUBLIC_URL}\nAdmin: {PUBLIC_URL}/admin" if PUBLIC_URL else "Public URL not set"


def describe_expiry(u):
    if not u.get("expires"):
        return "no limit"
    left = int(u["expires"] - now())
    return f"{left // 60} min left" if left > 0 else "expired"


async def admin_action(action, uid, minutes=0):
    u = STORE.get(uid) if uid else None
    if action in ("approve", "deny", "revoke", "delete") and not u:
        return "User not found"
    if action == "approve":
        STORE.set_status(uid, "approved", minutes)
        log(f"Approved {u['name']} ({describe_expiry(u)})")
        return f"Approved {u['name']} ({describe_expiry(u)})"
    if action == "deny":
        STORE.set_status(uid, "denied")
        await CAR.drop(uid, "user denied")
        return f"Denied {u['name']}"
    if action == "revoke":
        STORE.set_status(uid, "revoked")
        await CAR.drop(uid, "access revoked")
        log(f"Revoked {u['name']}")
        return f"Revoked {u['name']}"
    if action == "delete":
        STORE.users().pop(uid, None)
        STORE.save()
        await CAR.drop(uid, "user deleted")
        return f"Deleted {u['name']}"
    if action == "revoke_all":
        return f"Revoked {await revoke_all()} user(s)"
    if action == "stop":
        await CAR.stop("admin emergency stop")
        return "Stop sent"
    if action == "test":
        if uid not in TESTS:
            return "Unknown test"
        CAR.claim("admin", "Owner")
        try:
            await CAR.send(f"test?t={uid}")
        except Exception:
            return "Car is offline"
        return f"{TESTS[uid]} test started - result shows in Health in a few seconds"
    return "Unknown action"


TESTS = {"sonar": "Ultrasonic", "servo": "Servo", "m0": "Front-left motor", "m1": "Front-right motor",
         "m2": "Rear-left motor", "m3": "Rear-right motor"}


async def revoke_all():
    n = 0
    for u in list(STORE.users().values()):
        if u["status"] in ("approved", "pending"):
            u["status"] = "revoked"
            n += 1
    STORE.save()
    CAR.ctrl_uid = None
    await CAR.stop("revoke all")
    return n


async def background():
    """Expiry, driver-gone watchdog, keepalive to car, camera stop when nobody watches."""
    tick = 0
    while True:
        await asyncio.sleep(0.5)
        tick += 1
        t = now()
        try:
            # tell the car its driver is still here (car stops by itself after 2 s without this)
            if CAR.ctrl_uid and t - CAR.ctrl_last < DRIVER_GONE_S:
                await CAR.send("alive")
            elif CAR.ctrl_uid and CAR.status.get("mode", "MANUAL") != "MANUAL":
                await CAR.drop(CAR.ctrl_uid, "driver page lost")
        except Exception:
            pass
        if CAM.streaming and t - CAM.last_want > 10:
            CAM.streaming = False
            try:
                await CAM.ws.send_str("stop")
                log("Camera: stop (no viewers)")
            except Exception:
                pass
        if tick % 2 == 0:
            for u in list(STORE.users().values()):
                if u["status"] == "approved" and u["expires"] and u["expires"] < t:
                    STORE.set_status(u["id"], "expired")
                    await CAR.drop(u["id"], f"time limit over for {u['name']}")
                    log("Access expired:", u["name"])


# ============================================================ request helpers
NAME_RE = re.compile(r"[^\w .,'@()\-]", re.UNICODE)
ROBOT_ENDPOINTS = {
    "cmd": {"go": ("F", "B", "L", "R", "S"), "spd": None},
    "mode": {"m": ("manual", "auto", "follow"), "spd": None},
    "route": {"a": ("rec", "play", "ret", "clear")},
    "follow": {"seen": None, "x": None, "h": None},
}


def client_ip(req):
    fwd = req.headers.get("X-Forwarded-For", "")
    return fwd.split(",")[0].strip() if fwd else (req.remote or "?")


def is_https(req):
    return req.secure or req.headers.get("X-Forwarded-Proto", "") == "https"


def set_cookie(req, resp, name, value, max_age, samesite="Lax"):
    resp.set_cookie(name, value, max_age=max_age, path="/", httponly=True,
                    samesite=samesite, secure=is_https(req))


def is_admin(req):
    return req.cookies.get("rc_admin") in ADMIN_SESSIONS


def visitor(req):
    return STORE.by_token(req.cookies.get("rc_uid"))


def access(req):
    if is_admin(req):
        return {"id": "admin", "name": "Owner"}
    u = visitor(req)
    if STORE.has_access(u):
        u["last_seen"] = now()
        return u
    return None


def J(obj, status=200):
    return web.json_response(obj, status=status, headers={"Cache-Control": "no-store"})


def page(name):
    return web.FileResponse(os.path.join(WEB, name), headers={
        "Cache-Control": "no-store", "X-Frame-Options": "DENY", "Referrer-Policy": "no-referrer",
        "Content-Type": "text/html; charset=utf-8"})


async def body_json(req):
    try:
        if req.content_length and req.content_length > 10000:
            return {}
        return await req.json()
    except Exception:
        return {}


# ============================================================ visitor routes
async def h_root(req):
    return page("drive.html" if access(req) else "request.html")


async def h_admin_page(req):
    return page("admin.html")


async def h_health(req):
    return web.Response(text="ok")


async def h_me(req):
    if is_admin(req):
        return J({"state": "approved", "name": "Owner", "admin": True, "expires": 0})
    u = visitor(req)
    if not u:
        return J({"state": "none"})
    state = u["status"]
    if state == "approved" and not STORE.has_access(u):
        state = "expired"
    return J({"state": state, "name": u["name"], "expires": u["expires"]})


async def h_request(req):
    b = await body_json(req)
    name = NAME_RE.sub("", str(b.get("name", ""))).strip()[:40]
    msg = str(b.get("msg", "")).replace("\n", " ").strip()[:200]
    if len(name) < 2:
        return J({"error": "Please enter your name"}, 400)
    u = visitor(req)
    if u:
        if u["status"] == "pending":
            return J({"state": "pending"})
        if STORE.has_access(u):
            return J({"state": "approved"})
        if now() - u.get("requested", 0) < 120:
            return J({"error": "Please wait 2 minutes before asking again"}, 429)
    if STORE.pending_count() >= MAX_PENDING:
        return J({"error": "Too many requests waiting. Try later."}, 429)
    resp = J({"state": "pending"})
    if u:
        u.update(name=name, msg=msg, ip=client_ip(req), status="pending", requested=now())
        STORE.save()
    else:
        token = secrets.token_urlsafe(24)
        u = STORE.create(token, name, msg, client_ip(req))
        set_cookie(req, resp, "rc_uid", token, 31536000)
    log(f"Access request from {name} ({client_ip(req)})")
    if TG:
        tg_notify(TG.notify_request, dict(u))
    return resp


async def h_status(req):
    who = access(req)
    if not who:
        return J({"error": "no access"}, 403)
    CAR.touch(who["id"])
    duid, dname = CAR.driver()
    left = int(who["expires"] - now()) if who.get("expires") else 0
    return J({"online": CAR.online, "robot": CAR.status, "error": CAR.error(),
              "driver": dname, "you_drive": duid == who["id"], "you": who["name"],
              "left": max(0, left), "cam_error": CAM.error()})


async def h_robot(req):
    endpoint = req.match_info["ep"]
    if endpoint not in ROBOT_ENDPOINTS:
        return J({"error": "not found"}, 404)
    who = access(req)
    if not who:
        return J({"error": "no access"}, 403)
    holder = CAR.claim(who["id"], who["name"])
    if holder:
        return J({"error": f"{holder} is driving right now"}, 409)
    parts = []
    for k, allowed in ROBOT_ENDPOINTS[endpoint].items():
        v = req.query.get(k, "")
        if allowed is None:
            v = str(int(v)) if re.fullmatch(r"-?\d{1,4}", v) else "0"
        elif v not in allowed:
            return J({"error": f"bad {k}"}, 400)
        parts.append(f"{k}={v}")
    try:
        await CAR.send(f"{endpoint}?" + "&".join(parts))
        return J({"ok": True})
    except Exception:
        return J({"error": "car is offline"}, 502)


async def h_cam_stream(req):
    if not access(req):
        return J({"error": "no access"}, 403)
    await CAM.want()
    resp = web.StreamResponse(headers={"Content-Type": "multipart/x-mixed-replace; boundary=frame",
                                       "Cache-Control": "no-store", "X-Accel-Buffering": "no"})
    await resp.prepare(req)
    seq, last_check = 0, now()
    try:
        while True:
            await CAM.want()
            seq, frame = await CAM.next_frame(seq)
            if now() - last_check > 2:
                last_check = now()
                if not access(req):
                    break
            if frame:
                await resp.write(b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: "
                                 + str(len(frame)).encode() + b"\r\n\r\n" + frame + b"\r\n")
    except (ConnectionResetError, asyncio.CancelledError):
        pass
    return resp


async def h_cam_frame(req):
    if not access(req):
        return J({"error": "no access"}, 403)
    await CAM.want()
    if CAM.frame and now() - CAM.frame_time < 1:
        frame = CAM.frame
    else:
        _, frame = await CAM.next_frame(CAM.seq, timeout=4)
    if not frame:
        return J({"error": CAM.error() or "no frame"}, 503)
    return web.Response(body=frame, content_type="image/jpeg", headers={"Cache-Control": "no-store"})


# ============================================================ admin routes
async def h_admin_login(req):
    b = await body_json(req)
    if hmac.compare_digest(str(b.get("password", "")).encode(), ADMIN_PASSWORD.encode()):
        sid = secrets.token_urlsafe(24)
        ADMIN_SESSIONS.add(sid)
        log("Admin logged in from", client_ip(req))
        resp = J({"ok": True})
        set_cookie(req, resp, "rc_admin", sid, 2592000, "Strict")
        return resp
    await asyncio.sleep(1.5)
    log("Failed admin login from", client_ip(req))
    return J({"error": "Wrong password"}, 401)


async def h_admin_logout(req):
    ADMIN_SESSIONS.discard(req.cookies.get("rc_admin"))
    resp = J({"ok": True})
    set_cookie(req, resp, "rc_admin", "", 0, "Strict")
    return resp


async def h_admin_action(req):
    if not is_admin(req):
        return J({"error": "login required"}, 401)
    if "application/json" not in req.headers.get("Content-Type", ""):
        return J({"error": "json required"}, 400)
    b = await body_json(req)
    try:
        minutes = int(b.get("minutes") or 0)
    except (TypeError, ValueError):
        minutes = 0
    return J({"ok": True, "msg": await admin_action(str(b.get("action", "")), str(b.get("id", "")), minutes)})


async def h_admin_state(req):
    if not is_admin(req):
        return J({"error": "login required"}, 401)
    duid, dname = CAR.driver()
    users = []
    for u in sorted(STORE.users().values(), key=lambda x: -x.get("requested", 0)):
        st = u["status"]
        if st == "approved" and not STORE.has_access(u):
            st = "expired"
        users.append({"id": u["id"], "name": u["name"], "msg": u["msg"], "ip": u["ip"], "status": st,
                      "requested": u.get("requested", 0), "expires": u["expires"],
                      "last_seen": u.get("last_seen", 0), "driving": u["id"] == duid})
    t = now()
    return J({"now": t, "online": CAR.online, "robot": CAR.status, "robot_ip": CAR.status.get("ip"),
              "error": CAR.error(), "driver": dname, "cam_error": CAM.error(), "public_url": PUBLIC_URL,
              "car_connected": CAR.ws is not None and not CAR.ws.closed,
              "car_link_s": int(t - CAR.connected_at) if CAR.connected_at else 0,
              "car_health": CAR.health, "car_health_age": round(t - CAR.health_time, 1) if CAR.health_time else None,
              "cam_connected": CAM.connected(), "cam_streaming": CAM.streaming,
              "cam_link_s": int(t - CAM.connected_at) if CAM.connected_at else 0,
              "cam_fps": CAM.fps(), "cam_frame_kb": round(len(CAM.frame) / 1024, 1) if CAM.frame else 0,
              "cam_frame_age": round(t - CAM.frame_time, 1) if CAM.frame_time else None,
              "cam_health": CAM.health, "cam_health_age": round(t - CAM.health_time, 1) if CAM.health_time else None,
              "server_uptime": int(t - STARTED),
              "users": users, "tg_admins": len(STORE.tg_admins()),
              "telegram": "off" if not TG else ("connected" if TG.ok else "error: " + TG.error)})


# ============================================================ device websockets
def device_key_ok(req):
    return hmac.compare_digest(req.query.get("key", "").encode(), CAR_KEY.encode())


async def h_ws_car(req):
    if not device_key_ok(req):
        log("Car rejected: wrong CAR_KEY from", client_ip(req))
        return web.Response(status=403, text="bad key")
    ws = web.WebSocketResponse(heartbeat=15)
    await ws.prepare(req)
    if CAR.ws is not None and not CAR.ws.closed:
        await CAR.ws.close()
    was_offline = now() - CAR.status_time > 60
    CAR.ws, CAR.connected_at = ws, now()
    log("Car connected from", client_ip(req))
    if TG and was_offline:
        tg_notify(TG.broadcast, "\U0001F7E2 Car is online.\n" + link_text())
    try:
        async for msg in ws:
            if msg.type == WSMsgType.TEXT:
                try:
                    obj = json.loads(msg.data)
                except ValueError:
                    continue
                if obj.get("type") == "health":
                    CAR.health, CAR.health_time = obj, now()
                else:
                    CAR.status, CAR.status_time = obj, now()
    finally:
        if CAR.ws is ws:
            CAR.ws = None
            log("Car disconnected")
    return ws


async def h_ws_cam(req):
    if not device_key_ok(req):
        log("Camera rejected: wrong CAR_KEY from", client_ip(req))
        return web.Response(status=403, text="bad key")
    ws = web.WebSocketResponse(heartbeat=15, max_msg_size=2_000_000)
    await ws.prepare(req)
    if CAM.ws is not None and not CAM.ws.closed:
        await CAM.ws.close()
    CAM.ws, CAM.streaming, CAM.connected_at = ws, False, now()
    log("Camera connected from", client_ip(req))
    if now() - CAM.last_want < 10:
        await CAM.want()
    try:
        async for msg in ws:
            if msg.type == WSMsgType.BINARY and msg.data[:2] == b"\xff\xd8":
                await CAM.push(msg.data)
            elif msg.type == WSMsgType.TEXT:
                try:
                    obj = json.loads(msg.data)
                    if obj.get("type") == "health":
                        CAM.health, CAM.health_time = obj, now()
                except ValueError:
                    pass
    finally:
        if CAM.ws is ws:
            CAM.ws, CAM.streaming = None, False
            log("Camera disconnected")
    return ws


# ============================================================ app
async def on_startup(app):
    app["bg"] = asyncio.get_running_loop().create_task(background())
    if TG:
        app["tg"] = asyncio.get_running_loop().create_task(TG.loop())


async def on_cleanup(app):
    for k in ("bg", "tg"):
        if k in app:
            app[k].cancel()
    if TG and TG.session:
        await TG.session.close()


def make_app():
    app = web.Application(client_max_size=64 * 1024)
    app.router.add_get("/", h_root)
    app.router.add_get("/admin", h_admin_page)
    app.router.add_get("/healthz", h_health)
    app.router.add_get("/api/me", h_me)
    app.router.add_post("/api/request", h_request)
    app.router.add_get("/api/status", h_status)
    app.router.add_get("/api/{ep}", h_robot)
    app.router.add_get("/cam/stream", h_cam_stream)
    app.router.add_get("/cam/frame.jpg", h_cam_frame)
    app.router.add_post("/admin/login", h_admin_login)
    app.router.add_post("/admin/logout", h_admin_logout)
    app.router.add_post("/admin/api/action", h_admin_action)
    app.router.add_get("/admin/api/state", h_admin_state)
    app.router.add_get("/ws/car", h_ws_car)
    app.router.add_get("/ws/cam", h_ws_cam)
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    return app


if __name__ == "__main__":
    log(f"RoboCar Cloud on port {PORT}  public={PUBLIC_URL or '-'}")
    if not os.environ.get("ADMIN_PASSWORD"):
        log("ADMIN_PASSWORD not set, temporary password:", ADMIN_PASSWORD)
    if not os.environ.get("CAR_KEY"):
        log("CAR_KEY not set, temporary key:", CAR_KEY)
    log("Telegram:", "on" if TG else "off")
    web.run_app(make_app(), port=PORT, print=None, access_log=None)
