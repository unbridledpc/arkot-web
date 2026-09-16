import hashlib
import json
import logging
import re
import time

import pytest

from app import clientlogin, ratelimit, worlds
from app.clientlogin import ClientLogin

NOW = 1_800_000_000
PASSWORD = "correct horse battery"
KEY_RE = re.compile(r"^[0-9a-f]{64}$")


def player(name, **over):
    row = {"name": name, "level": 8, "sex": 1, "vocation": 4, "looktype": 128, "lookhead": 78,
           "lookbody": 68, "looklegs": 58, "lookfeet": 76, "lookaddons": 0, "lastlogin": 0}
    row.update(over)
    return row


class FakeStore:
    def __init__(self):
        self.accounts = [{"id": 7, "name": "owner", "password": hashlib.sha1(PASSWORD.encode()).hexdigest(),
                          "secret": "", "type": 1, "premium_ends_at": 0, "email": "o@example.com"}]
        self.players = {"blacktek": {7: [player("Knight One"), player("Sorc", vocation=1, sex=0)]},
                        "arkot_test": {7: [player("Tester", level=20, lastlogin=NOW - 10)]}}
        self.bans = {}
        self.broken = set()
        self.sessions = []
        self.fail_find = None

    def find_accounts(self, identifier):
        if self.fail_find:
            raise self.fail_find
        return [a for a in self.accounts if identifier in (a["name"], a["email"])]

    def ban_of(self, aid):
        return self.bans.get(aid)

    def characters_on(self, world, aid):
        if world.schema in self.broken:
            raise RuntimeError(f"schema {world.schema} is gone")
        return list(self.players.get(world.schema, {}).get(aid, []))

    def create_session(self, key_hash, aid, ip, created, expires):
        self.sessions.append((key_hash, aid, ip, created, expires))

    def purge_sessions(self, before):
        pass


def registry():
    return worlds.Registry(worlds=worlds.parse_worlds_text("""
[[world]]
id = 0
name = "ArkOT"
address = "127.0.0.1"
public_address = "bt.tibtool.com"
port = 7172
schema = "blacktek"

[[world]]
id = 1
name = "ArkOT Test"
address = "127.0.0.1"
port = 7272
schema = "arkot_test"
"""))


def service(store=None, reg=None):
    store = store or FakeStore()
    lim = ratelimit.LoginLimiter()
    return ClientLogin(store, reg or registry(), lim, clock=lambda: NOW), store


def login(svc, email="owner", password=PASSWORD, ip="203.0.113.9"):
    body = json.dumps({"type": "login", "email": email, "password": password,
                       "stayloggedin": True}).encode()
    data, refused = svc.parse(body, "application/json")
    assert refused is None
    reply = svc.login(data, ip)
    json.dumps(reply)                          # always serialisable
    return reply


def assert_error(reply, code=None):
    assert set(reply) == {"errorCode", "errorMessage"}
    assert type(reply["errorCode"]) is int
    assert reply["errorCode"] not in (0, 6)
    assert isinstance(reply["errorMessage"], str) and reply["errorMessage"]
    if code is not None:
        assert reply["errorCode"] == code


# ---- success shape --------------------------------------------------------
def test_success_shape_and_types():
    svc, store = service()
    reply = login(svc)
    assert "errorCode" not in reply
    s = reply["session"]
    assert KEY_RE.match(s["sessionkey"]) and "\n" not in s["sessionkey"]
    assert type(s["premiumuntil"]) is int
    for k in ("ispremium", "fpstracking", "optiontracking", "isreturner", "returnernotification",
              "showrewardnews", "recoverysetupcomplete"):
        assert type(s[k]) is bool
    assert type(s["lastlogintime"]) is int and isinstance(s["status"], str)

    pd = reply["playdata"]
    assert isinstance(pd["worlds"], list) and isinstance(pd["characters"], list)
    for w in pd["worlds"]:
        assert type(w["id"]) is int and isinstance(w["name"], str)
        for k in ("externaladdressprotected", "externaladdressunprotected", "location"):
            assert isinstance(w[k], str)
        for k in ("externalportprotected", "externalportunprotected", "previewstate", "pvptype"):
            assert type(w[k]) is int
        assert type(w["anticheatprotection"]) is bool
    for c in pd["characters"]:
        assert type(c["worldid"]) is int
        assert isinstance(c["name"], str) and isinstance(c["vocation"], str)
        for k in ("level", "dailyrewardstate", "outfitid", "headcolor", "torsocolor", "legscolor",
                  "detailcolor", "addonsflags"):
            assert type(c[k]) is int
        for k in ("ismale", "tutorial", "ismaincharacter", "ishidden"):
            assert type(c[k]) is bool
    assert isinstance(reply["loginemail"], str) and isinstance(reply["devicecookie"], str)


def test_characters_from_every_world_tagged():
    svc, _ = service()
    pd = login(svc)["playdata"]
    got = {(c["worldid"], c["name"]) for c in pd["characters"]}
    assert got == {(0, "Knight One"), (0, "Sorc"), (1, "Tester")}
    ids = {w["id"] for w in pd["worlds"]}
    assert all(c["worldid"] in ids for c in pd["characters"])
    assert [w["name"] for w in pd["worlds"]] == ["ArkOT", "ArkOT Test"]


def test_worlds_advertise_public_address_never_bind_address():
    svc, _ = service()
    w0, w1 = login(svc)["playdata"]["worlds"]
    assert w0["externaladdressprotected"] == w0["externaladdressunprotected"] == "bt.tibtool.com"
    assert w0["externalportprotected"] == 7172
    assert w1["externaladdressprotected"] == "127.0.0.1"      # no public_address declared


def test_session_is_stored_hashed_with_24h_expiry():
    svc, store = service()
    key = login(svc)["session"]["sessionkey"]
    [(key_hash, aid, ip, created, expires)] = store.sessions
    assert key_hash == hashlib.sha256(key.encode()).hexdigest() != key
    assert (aid, created, expires) == (7, NOW, NOW + 86400)
    assert ip == 0xCB007109


def test_session_keys_are_fresh():
    svc, _ = service()
    assert login(svc)["session"]["sessionkey"] != login(svc)["session"]["sessionkey"]


def test_empty_account_gets_empty_array_and_all_worlds():
    store = FakeStore()
    store.players = {}
    svc, _ = service(store)
    pd = login(svc)["playdata"]
    assert pd["characters"] == [] and len(pd["worlds"]) == 2


def test_login_by_email():
    svc, _ = service()
    assert "session" in login(svc, email="o@example.com")


def test_premium_from_premium_ends_at():
    store = FakeStore()
    store.accounts[0]["premium_ends_at"] = NOW + 3600
    svc, _ = service(store)
    s = login(svc)["session"]
    assert s["premiumuntil"] == NOW + 3600 and s["ispremium"] is True


def test_broken_world_is_skipped_not_fatal(caplog):
    store = FakeStore()
    store.broken.add("arkot_test")
    svc, _ = service(store)
    with caplog.at_level(logging.WARNING):
        pd = login(svc)["playdata"]
    assert {c["worldid"] for c in pd["characters"]} == {0}
    assert len(pd["worlds"]) == 2
    assert "arkot_test" in caplog.text


def test_character_on_unknown_world_is_never_sent():
    reply = clientlogin.success("0" * 64, {"premium_ends_at": 0}, registry().worlds,
                                [(0, player("A")), (42, player("Ghost"))], "x", NOW)
    ids = {w["id"] for w in reply["playdata"]["worlds"]}
    assert [c["name"] for c in reply["playdata"]["characters"]] == ["A"]
    assert all(c["worldid"] in ids for c in reply["playdata"]["characters"])


def test_new_session_key_shape():
    for _ in range(50):
        assert KEY_RE.match(clientlogin.new_session_key())


# ---- refusals -------------------------------------------------------------
@pytest.mark.parametrize("email,password", [
    ("owner", "wrong"), ("nobody", PASSWORD), ("", PASSWORD), ("owner", ""),
    ("x" * 300, PASSWORD), ("owner", "p" * 300),
])
def test_bad_credentials_code_3(email, password):
    svc, store = service()
    assert_error(login(svc, email, password), 3)
    assert store.sessions == []


@pytest.mark.parametrize("data", [
    {"type": "login"}, {"type": "login", "email": 5, "password": PASSWORD},
    {"type": "login", "email": "owner", "password": None},
])
def test_malformed_fields(data):
    svc, store = service()
    assert_error(svc.login(data, "1.2.3.4"))
    assert store.sessions == []


@pytest.mark.parametrize("body,ctype", [
    (b'{"type":"login"}', "text/plain"),
    (b'{"type":"login"}', ""),
    (b'email=a&password=b', "application/x-www-form-urlencoded"),
    (b"not json", "application/json"),
    (b"[1,2]", "application/json"),
    (b"\xff\xfe", "application/json"),
    (b'{"a":"' + b"x" * 5000 + b'"}', "application/json"),
])
def test_parse_refusals(body, ctype):
    svc, _ = service()
    data, refused = svc.parse(body, ctype)
    assert data is None
    assert_error(refused)


def test_parse_accepts_charset_suffix():
    svc, _ = service()
    data, refused = svc.parse(b'{"type":"login"}', "application/json; charset=utf-8")
    assert refused is None and data == {"type": "login"}


def test_authenticator_secret_refused_without_code_6():
    store = FakeStore()
    store.accounts[0]["secret"] = "JBSWY3DPEHPK3PXP"
    svc, _ = service(store)
    reply = login(svc)
    assert_error(reply, clientlogin.CODE_AUTHENTICATOR_UNSUPPORTED)
    assert store.sessions == []


def test_secret_not_revealed_on_wrong_password():
    store = FakeStore()
    store.accounts[0]["secret"] = "JBSWY3DPEHPK3PXP"
    svc, _ = service(store)
    assert_error(login(svc, password="wrong"), 3)


def test_permanent_ban_uses_server_text():
    store = FakeStore()
    store.bans[7] = {"reason": "botting", "expires_at": 0, "banned_by_name": "Staff"}
    svc, _ = service(store)
    reply = login(svc)
    assert_error(reply, clientlogin.CODE_BANNED)
    assert reply["errorMessage"] == "Your account has been permanently banned by Staff.\n\nReason specified:\nbotting"
    assert store.sessions == []


def test_timed_ban_uses_server_text_and_date():
    store = FakeStore()
    until = NOW + 86400
    store.bans[7] = {"reason": "", "expires_at": until}          # no banned_by_name column
    svc, _ = service(store)
    msg = login(svc)["errorMessage"]
    assert msg == (f"Your account has been banned until {time.strftime('%d %b %Y', time.localtime(until))} "
                   "by .\n\nReason specified:\n(none)")


def test_expired_ban_allows_login():
    store = FakeStore()
    store.bans[7] = {"reason": "x", "expires_at": NOW - 1, "banned_by_name": "S"}
    svc, _ = service(store)
    assert "session" in login(svc)


def test_invalid_registry_refuses_every_login():
    svc, store = service(reg=worlds.Registry(error="duplicate name", source="x"))
    assert_error(login(svc), clientlogin.CODE_WORLDS_UNAVAILABLE)
    assert store.sessions == []


def test_database_error_is_generic():
    store = FakeStore()
    store.fail_find = RuntimeError("boom")
    svc, _ = service(store)
    assert_error(login(svc), clientlogin.CODE_INTERNAL)


def test_rate_limited_after_repeated_failures():
    svc, store = service()
    svc.limiter = ratelimit.LoginLimiter(rate_per_minute=10_000, burst=10_000)
    for _ in range(ratelimit.FAILURES_PER_PAIR):
        assert_error(login(svc, password="wrong"), 3)
    reply = login(svc)                                          # even the right password
    assert_error(reply, clientlogin.CODE_RATE_LIMITED)
    assert reply["errorMessage"] == ratelimit.MESSAGE
    assert store.sessions == []


def test_error_never_emits_zero_or_six():
    assert clientlogin.error(6, "x")["errorCode"] not in (0, 6)
    assert clientlogin.error(0, "x")["errorCode"] not in (0, 6)


# ---- logging --------------------------------------------------------------
def test_failed_login_logs_no_password_or_key(caplog, capsys):
    secret_pw = "Sup3r-Secret-Pa55"
    store = FakeStore()
    store.accounts[0]["password"] = hashlib.sha1(secret_pw.encode()).hexdigest()
    svc, _ = service(store)
    with caplog.at_level(logging.DEBUG):
        wrong = login(svc, password=secret_pw + "x")
        ok = login(svc, password=secret_pw)
        store.fail_find = RuntimeError(f"statement failed near '{secret_pw}'")
        broken = login(svc, password=secret_pw)
    assert_error(wrong, 3)
    assert_error(broken, clientlogin.CODE_INTERNAL)
    key = ok["session"]["sessionkey"]
    out = capsys.readouterr()
    logged = caplog.text + out.out + out.err + "".join(r.getMessage() for r in caplog.records)
    for needle in (secret_pw, secret_pw + "x", key, hashlib.sha256(key.encode()).hexdigest(),
                   hashlib.sha1(secret_pw.encode()).hexdigest(),
                   hashlib.sha1((secret_pw + "x").encode()).hexdigest()):
        assert needle not in logged
    assert caplog.records                                       # it did log something
