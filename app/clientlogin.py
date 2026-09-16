"""The game client's login: `POST /api/login`.

The client (OTClient 4.1, `httpLogin = true`) posts
`{"type": "login", "email": ..., "password": ...}` and expects back, at HTTP 200,
either a session and character list or `{"errorCode": <int>, "errorMessage": ...}`.
What it reads, and why each rule below exists:

* `errorCode` must be a JSON integer: the client reads it with `get<int>()`
  (httplogin.cpp), and a string throws. Code 6 opens the client's authenticator
  prompt (entergame.lua), so it is never sent.
* `session.sessionkey` is 64 lowercase hex characters. It rides inside the
  client's 128-byte RSA block beside the character name, and the game server
  reads a credential with a newline in it as email and password rather than as
  a session key (protocolgame.cpp).
* every `characters[].worldid` names an entry of `worlds[]`, or the client's
  Lua fails on `world.name`.
* `worlds[].name` is exactly the world list's name: the client announces it to
  the game server, which refuses a name that is not its own.

The game server finds the session by the SHA-256 of the key in
`account_sessions`; only the hash is stored here.

Nothing in this module touches the database directly or reads the environment:
the caller hands in a Store, the world Registry and the limiter, which keeps it
testable without a database and without importing the web app.
"""
import hashlib
import hmac
import json
import logging
import re
import secrets
import time

from app import ratelimit

log = logging.getLogger("uvicorn.error.arkot.login")

VOCATIONS = {0: "None", 1: "Sorcerer", 2: "Druid", 3: "Paladin", 4: "Knight",
             5: "Master Sorcerer", 6: "Elder Druid", 7: "Royal Paladin", 8: "Elite Knight"}

MAX_BODY = 4096
MAX_FIELD = 255
SESSION_LIFETIME = 86400
PURGE_EVERY = 600
SESSION_KEY_RE = re.compile(r"[0-9a-f]{64}")

# errorCode values. 6 is reserved by the client for "authenticator token
# required" and must never be sent.
CODE_BAD_REQUEST = 1
CODE_INTERNAL = 2
CODE_INVALID_CREDENTIALS = 3
CODE_BANNED = 4
CODE_RATE_LIMITED = 5
CODE_AUTHENTICATOR_UNSUPPORTED = 7
CODE_WORLDS_UNAVAILABLE = 8
AUTHENTICATOR_CODE = 6

MSG_INVALID = "Account name or password is not correct."
MSG_INTERNAL = "Internal error. Please try again later."
MSG_BAD_REQUEST = "The login request could not be read."
MSG_AUTHENTICATOR = ("This account is protected by an authenticator, which this login "
                     "does not support yet. Please contact the staff.")
MSG_WORLDS = "The world list is unavailable right now. Please try again later."

# Compared against when no account matches, so a miss costs a real comparison.
_DUMMY_HASH = hashlib.sha1(b"arkenfall-no-such-account").hexdigest()


def sha1(text: str) -> str:
    return hashlib.sha1(text.encode()).hexdigest()


def error(code: int, message: str) -> dict:
    code = int(code)
    if code in (0, AUTHENTICATOR_CODE):
        # 0 would read as success, 6 as an authenticator prompt.
        code = CODE_INTERNAL
    return {"errorCode": code, "errorMessage": str(message)}


def new_session_key() -> str:
    key = secrets.token_hex(32)
    # token_hex(32) is always this shape; the check guards against a future
    # edit that would break the RSA budget or the server's key detection.
    if not SESSION_KEY_RE.fullmatch(key):
        raise RuntimeError("session key has the wrong shape")
    return key


def session_hash(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def format_ban_date(ts: int) -> str:
    # The game server's formatDateShort: "{:%d %b %Y}" in local time.
    return time.strftime("%d %b %Y", time.localtime(int(ts)))


def ban_message(reason, expires_at, banned_by_name) -> str:
    """The game server's own refusal text (protocolgame.cpp), word for word."""
    reason = reason or "(none)"
    by = banned_by_name or ""
    if int(expires_at or 0) > 0:
        return (f"Your account has been banned until {format_ban_date(expires_at)} by {by}."
                f"\n\nReason specified:\n{reason}")
    return f"Your account has been permanently banned by {by}.\n\nReason specified:\n{reason}"


def ban_is_active(ban, now: int) -> bool:
    """As the game server decides: an expired ban no longer counts. It is left
    in place; the game server retires expired bans into their history."""
    if not ban:
        return False
    expires = int(ban.get("expires_at") or 0)
    return expires == 0 or now <= expires


def world_entry(world) -> dict:
    address = world.dial_address
    return {"id": world.id, "name": world.name,
            "externaladdressprotected": address, "externaladdressunprotected": address,
            "externalportprotected": world.port, "externalportunprotected": world.port,
            "location": "", "anticheatprotection": False, "previewstate": 0,
            "pvptype": world.pvptype}


def character_entry(row: dict, world_id: int) -> dict:
    return {"worldid": int(world_id), "name": str(row["name"]), "level": int(row["level"]),
            "vocation": VOCATIONS.get(int(row["vocation"]), "None"),
            "ismale": int(row["sex"]) == 1, "tutorial": False, "ismaincharacter": False,
            "ishidden": False, "dailyrewardstate": 0,
            "outfitid": int(row["looktype"]), "headcolor": int(row["lookhead"]),
            "torsocolor": int(row["lookbody"]), "legscolor": int(row["looklegs"]),
            "detailcolor": int(row["lookfeet"]), "addonsflags": int(row["lookaddons"])}


def success(session_key: str, account: dict, worlds, characters, login_email: str, now: int) -> dict:
    """The login-server's full response shape. `characters` is a list of
    (world_id, player row) pairs; a character on a world missing from `worlds`
    is dropped rather than sent, since the client cannot display it."""
    known = {w.id for w in worlds}
    chars = [character_entry(row, wid) for wid, row in characters if wid in known]
    premium_until = int(account.get("premium_ends_at") or 0)
    last_login = max((int(row.get("lastlogin") or 0) for _, row in characters), default=0)
    return {
        "session": {"sessionkey": session_key, "premiumuntil": premium_until,
                    "ispremium": premium_until > now, "lastlogintime": last_login,
                    "status": "active", "fpstracking": False, "optiontracking": False,
                    "isreturner": False, "returnernotification": False,
                    "showrewardnews": False, "recoverysetupcomplete": False},
        "playdata": {"worlds": [world_entry(w) for w in worlds], "characters": chars},
        "loginemail": login_email,
        "devicecookie": "",
    }


def _describe(exc: BaseException) -> str:
    """An exception for the log without its message: a driver's message can
    quote the statement, and the statement can carry an account name or a
    session hash. The class and the MySQL error number are enough to act on."""
    number = exc.args[0] if exc.args and isinstance(exc.args[0], int) else None
    return f"{type(exc).__name__}" + (f" (errno {number})" if number is not None else "")


class Store:
    """The database side of a login, over a connection factory that returns
    pymysql connections with DictCursor (the web app's `db`)."""

    PLAYER_COLUMNS = ("name, level, sex, vocation, looktype, lookhead, lookbody, looklegs, "
                      "lookfeet, lookaddons, lastlogin")

    def __init__(self, connect):
        self.connect = connect
        self._ban_has_name = None

    def _all(self, sql, args=()):
        with self.connect() as conn, conn.cursor() as cur:
            cur.execute(sql, args)
            return cur.fetchall()

    def find_accounts(self, identifier: str):
        # Emails are not unique in the schema, so every match is returned.
        return self._all("""SELECT id, name, password, secret, type, premium_ends_at
                            FROM accounts WHERE name=%s OR email=%s""", (identifier, identifier))

    def ban_has_name(self) -> bool:
        # Asked once; migration 7 adds the column, and a restart picks it up.
        if self._ban_has_name is None:
            rows = self._all("""SELECT count(*) c FROM information_schema.COLUMNS
                                WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'account_bans'
                                  AND COLUMN_NAME = 'banned_by_name'""")
            self._ban_has_name = bool(rows and rows[0]["c"])
        return self._ban_has_name

    def ban_of(self, account_id: int):
        extra = ", banned_by_name" if self.ban_has_name() else ""
        rows = self._all(f"SELECT reason, expires_at{extra} FROM account_bans WHERE account_id=%s",
                         (account_id,))
        return rows[0] if rows else None

    def characters_on(self, world, account_id: int):
        return self._all(f"""SELECT {self.PLAYER_COLUMNS} FROM {world.sql}.players
                             WHERE account_id=%s AND deletion=0 ORDER BY name""", (account_id,))

    def create_session(self, key_hash: str, account_id: int, ip: int, created: int, expires: int):
        with self.connect() as conn, conn.cursor() as cur:
            cur.execute("""INSERT INTO account_sessions (id, account_id, ip, created, expires, character_name)
                           VALUES (%s, %s, %s, %s, %s, NULL)""",
                        (key_hash, account_id, ip, created, expires))

    def purge_sessions(self, before: int):
        with self.connect() as conn, conn.cursor() as cur:
            cur.execute("DELETE FROM account_sessions WHERE expires < %s", (before,))


class ClientLogin:
    def __init__(self, store, registry, limiter, clock=time.time):
        self.store = store
        self.registry = registry
        self.limiter = limiter
        self.clock = clock
        self._last_purge = 0

    def parse(self, body: bytes, content_type: str):
        """The request body as a dict, or an error response. Only JSON is taken:
        the client always sends it, and it keeps plain cross-site form posts out."""
        if not (content_type or "").lower().startswith("application/json"):
            return None, error(CODE_BAD_REQUEST, MSG_BAD_REQUEST)
        if body is None or len(body) > MAX_BODY:
            return None, error(CODE_BAD_REQUEST, MSG_BAD_REQUEST)
        try:
            data = json.loads(body)
        except (ValueError, UnicodeDecodeError):
            return None, error(CODE_BAD_REQUEST, MSG_BAD_REQUEST)
        if not isinstance(data, dict):
            return None, error(CODE_BAD_REQUEST, MSG_BAD_REQUEST)
        return data, None

    def login(self, data: dict, ip: str) -> dict:
        """One `type: login` request, already parsed. Always returns a response
        body; never raises for anything a request or the database can cause."""
        email, password = data.get("email"), data.get("password")
        if (not isinstance(email, str) or not isinstance(password, str)
                or not email.strip() or not password
                or len(email.encode()) > MAX_FIELD or len(password.encode()) > MAX_FIELD):
            return error(CODE_INVALID_CREDENTIALS, MSG_INVALID)
        identifier = email.strip()

        if not self.limiter.allow(ip, identifier):
            log.warning("Client login refused by the rate limiter for %s", ip)
            return error(CODE_RATE_LIMITED, ratelimit.MESSAGE)

        if not self.registry.ok:
            log.error("Client login refused: the world list is invalid (%s)", self.registry.error)
            return error(CODE_WORLDS_UNAVAILABLE, MSG_WORLDS)

        try:
            return self._login(identifier, password, ip)
        except Exception as exc:          # noqa: BLE001 — the client must get JSON, not a 500
            log.error("Client login failed with an internal error: %s", _describe(exc))
            return error(CODE_INTERNAL, MSG_INTERNAL)

    def _login(self, identifier: str, password: str, ip: str) -> dict:
        now = int(self.clock())
        given = sha1(password).encode()
        account = None
        rows = self.store.find_accounts(identifier)
        for row in rows:
            stored = str(row.get("password") or "").encode()
            if hmac.compare_digest(stored, given) and account is None:
                account = row
        if not rows:
            hmac.compare_digest(_DUMMY_HASH.encode(), given)
        if account is None:
            self.limiter.failed(ip, identifier)
            log.info("Client login: wrong account name or password from %s", ip)
            return error(CODE_INVALID_CREDENTIALS, MSG_INVALID)
        self.limiter.succeeded(ip, identifier)
        aid = int(account["id"])

        if account.get("secret"):
            log.info("Client login refused for account %d: it has an authenticator secret", aid)
            return error(CODE_AUTHENTICATOR_UNSUPPORTED, MSG_AUTHENTICATOR)

        ban = self.store.ban_of(aid)
        if ban_is_active(ban, now):
            log.info("Client login refused for account %d: banned", aid)
            return error(CODE_BANNED, ban_message(ban.get("reason"), ban.get("expires_at"),
                                                  ban.get("banned_by_name")))

        characters = []
        for world in self.registry.worlds:
            try:
                rows = self.store.characters_on(world, aid)
            except Exception as exc:      # noqa: BLE001 — one broken world must not hide the rest
                log.warning("Client login: could not read schema %s of world %r (id %d); it "
                            "contributes no characters to account %d's list: %s",
                            world.schema, world.name, world.id, aid, _describe(exc))
                continue
            characters.extend((world.id, row) for row in rows)

        self._purge(now)
        key = new_session_key()
        self.store.create_session(session_hash(key), aid, ratelimit.ipv4_int(ip),
                                  now, now + SESSION_LIFETIME)
        log.info("Client login: session issued for account %d from %s", aid, ip)
        return success(key, account, self.registry.worlds, characters, identifier, now)

    def _purge(self, now: int):
        if now - self._last_purge < PURGE_EVERY:
            return
        self._last_purge = now
        try:
            self.store.purge_sessions(now - 3600)
        except Exception as exc:          # noqa: BLE001 — housekeeping never blocks a login
            log.warning("Client login: expired sessions could not be purged: %s", _describe(exc))
