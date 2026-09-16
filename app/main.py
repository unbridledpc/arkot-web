"""Arkenfall website — the player-facing site for the BlackTek 15.25 server.

Shares the live game database with the game server, the login webservice and
the legacy Znote AAC (mounted under /legacy). Every write is Znote- and
login-server-compatible: sha1 passwords, same account/player column values.
"""
import hashlib
import hmac
import os
import re
import secrets
import time
import urllib.parse

import json
import logging
import pathlib

import pymysql
from fastapi import FastAPI, Form, Query, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from itsdangerous import BadSignature, URLSafeSerializer

from app import clientlogin, ratelimit, worlds

DB = dict(
    host=os.environ.get("DB_HOST", "btdb"),
    port=int(os.environ.get("DB_PORT", "3306")),
    user=os.environ.get("DB_USER", "forgottenserver"),
    password=os.environ["DB_PASS"],
    database=os.environ.get("DB_NAME", "blacktek"),
    cursorclass=pymysql.cursors.DictCursor,
    autocommit=True,
    charset="utf8mb4",
)
SECRET = os.environ["APP_SECRET"]
signer = URLSafeSerializer(SECRET, salt="arkot-session")

# The site's own staff list, so a mistyped account type can never lock the owner
# out of his own game; anything the game itself calls a god or a community
# manager is staff here too.
ADMIN_EMAILS = {e.strip().lower() for e in
                os.environ.get("ADMIN_EMAILS", "joshwall488@gmail.com").split(",") if e.strip()}
ADMIN_ACCOUNT_TYPE = 5
ADMIN_LOG = pathlib.Path(os.environ.get("ADMIN_LOG", "var/admin-log.jsonl"))

SITE_NAME = "Arkenfall"
WORLD_NAME = os.environ.get("WORLD_NAME", "Arkenfall")
GAME_HOST = os.environ.get("GAME_HOST", "bt.tibtool.com")
LOGIN_PORT = 7171
GAME_PORT = 7172
log = logging.getLogger("uvicorn.error.arkot")

# Every game world, from the servers' own worlds.toml (WORLDS_FILE). The site's
# browse pages still read the home schema, DB_NAME; the client login, the
# account page and character creation span every world.
WORLDS = worlds.load()
HOME_SCHEMA = DB["database"]
if WORLDS.ok and not any(w.schema == HOME_SCHEMA for w in WORLDS.worlds):
    log.warning("No world in the world list uses DB_NAME (%s); the browse pages read a "
                "schema no client can log into.", HOME_SCHEMA)
LOGIN_LIMITER = ratelimit.LoginLimiter()
# While the list is broken, client logins and character creation are refused;
# the account page still shows the home world's characters.
ACCOUNT_WORLDS = WORLDS.worlds if WORLDS.ok else worlds.load(
    {k: v for k, v in os.environ.items() if k != "WORLDS_FILE"}).worlds
CLIENT_LOGIN = clientlogin.ClientLogin(clientlogin.Store(lambda: db()),   # db() is defined below
                                       WORLDS, LOGIN_LIMITER)

VOCATIONS = clientlogin.VOCATIONS
SPELLS = json.loads((pathlib.Path(__file__).parent / "data" / "spells.json").read_text())
QUESTS = json.loads((pathlib.Path(__file__).parent / "data" / "quests.json").read_text())
QUEST_BY_SLUG = {q["slug"]: q for q in QUESTS["quests"]}
QUEST_TAGS = {
    "outfit": "Outfits", "addon": "Addons", "mount": "Mounts",
    "access": "Access", "experience": "Experience", "treasure": "Treasure",
}
GROUP_NAMES = {2: "Tutor", 3: "Senior Tutor", 4: "Gamemaster", 5: "Community Manager", 6: "God"}
HIGHSCORE_CATS = {
    "experience": ("Experience", "experience", "experience"),
    "maglevel": ("Magic Level", "maglevel", "maglevel"),
    "sword": ("Sword Fighting", "skill_sword", "skill_sword"),
    "axe": ("Axe Fighting", "skill_axe", "skill_axe"),
    "club": ("Club Fighting", "skill_club", "skill_club"),
    "dist": ("Distance Fighting", "skill_dist", "skill_dist"),
    "shielding": ("Shielding", "skill_shielding", "skill_shielding"),
    "fist": ("Fist Fighting", "skill_fist", "skill_fist"),
    "fishing": ("Fishing", "skill_fishing", "skill_fishing"),
}

# The left-hand menu: (key, label, glyph, [(name, href), ...]).
MENU = [
    ("news", "News", "N", [("Latest news", "/"), ("News archive", "/news"), ("Changelog", "/changelog")]),
    ("community", "Community", "C", [
        ("Highscores", "/highscores"), ("Who is online", "/online"), ("Character search", "/search"),
        ("Latest deaths", "/deaths"), ("Kill statistics", "/killstats"), ("Guilds", "/guilds"),
        ("Houses", "/houses"), ("Team", "/team")]),
    ("library", "Library", "L", [("Server information", "/server"), ("Quests", "/quests"),
                                 ("Spells", "/spells"), ("Rules", "/rules")]),
    ("support", "Support", "S", [("Helpdesk", "/legacy/helpdesk.php"), ("Lost account", "/legacy/sub.php?page=recover"), ("Rules", "/rules")]),
    ("account", "Account", "A", [("Account management", "/account"), ("Create account", "/register"), ("Create character", "/character/create"), ("Log in", "/login")]),
    ("download", "Download", "D", [("Get the client", "/downloads")]),
]
SECTION_OF = {href.split("?")[0]: key for key, _, _, items in MENU for _, href in items}
SECTION_OF["/"] = "news"


def active_section(path: str) -> str:
    if path in SECTION_OF:
        return SECTION_OF[path]
    for prefix, key in (("/character/", "community"), ("/guild/", "community"),
                        ("/quest/", "library"), ("/account", "account"),
                        ("/admin", "account"), ("/news", "news")):
        if path.startswith(prefix):
            return key
    return "news"


# Client packages: too large for git, so they sit beside the app and are served
# straight off disk. DOWNLOADS_DIR is the deploy's copy; the manifest that
# describes them ships with the code.
DOWNLOADS_DIR = pathlib.Path(os.environ.get("DOWNLOADS_DIR", "downloads"))
DOWNLOADS_DIR.mkdir(parents=True, exist_ok=True)
DOWNLOADS_FILE = pathlib.Path(__file__).parent / "data" / "downloads.json"

app = FastAPI(title=SITE_NAME)
app.mount("/static", StaticFiles(directory="app/static"), name="static")
app.mount("/files", StaticFiles(directory=DOWNLOADS_DIR), name="files")
templates = Jinja2Templates(directory="app/templates")
templates.env.globals.update(vocname=lambda v: VOCATIONS.get(v, "?"), menu=MENU,
                             active_section=active_section, world_name=WORLD_NAME, site_name=SITE_NAME)
templates.env.filters["megabytes"] = lambda n: ("%.0f MB" % (n / 1048576)) if n else "—"
templates.env.filters["timestamp"] = (
    lambda t: time.strftime("%b %d, %Y", time.localtime(int(t))) if t else "never")


def db():
    return pymysql.connect(**DB)


def q(sql, args=None, one=False):
    with db() as conn, conn.cursor() as cur:
        cur.execute(sql, args or ())
        return cur.fetchone() if one else cur.fetchall()


def sha1(s: str) -> str:
    return hashlib.sha1(s.encode()).hexdigest()


def request_ip(request: Request) -> str:
    peer = request.client.host if request.client else ""
    return ratelimit.client_ip(peer, request.headers.get("x-forwarded-for", ""))


def revoke_sessions(aid: int):
    """Throw away every game-client session the account holds, so a changed
    password or a ban also shuts the door on keys already handed out."""
    try:
        q("DELETE FROM account_sessions WHERE account_id=%s", (aid,))
    except pymysql.err.ProgrammingError as err:
        if err.args and err.args[0] == 1146:      # no such table: no sessions to revoke
            log.warning("account_sessions does not exist; no client sessions to revoke")
            return
        raise


def world_or_none(wid):
    return WORLDS.find(wid) if WORLDS.ok else None


def account_characters(aid: int):
    """The account's characters on every world, each tagged with its world and
    town name. A world whose schema cannot be read is skipped and logged."""
    chars = []
    for w in ACCOUNT_WORLDS:
        try:
            town_names = {t["id"]: t["name"] for t in towns(w)}
            rows = q(f"""SELECT name, level, vocation, town_id, lastlogin,
                       (SELECT count(*) FROM {w.sql}.players_online o WHERE o.player_id = p.id) online
                       FROM {w.sql}.players p WHERE account_id=%s AND deletion=0""", (aid,))
        except pymysql.MySQLError as err:
            log.warning("Account page: could not read world %r (schema %s): %s",
                        w.name, w.schema, type(err).__name__)
            continue
        for row in rows:
            row["world"] = w.name
            row["town_name"] = town_names.get(row["town_id"], "?")
        chars.extend(rows)
    chars.sort(key=lambda c: -c["level"])
    return chars


# ---- sessions -------------------------------------------------------------
def get_session(request: Request) -> dict:
    raw = request.cookies.get("arkot")
    if not raw:
        return {}
    try:
        return signer.loads(raw)
    except BadSignature:
        return {}


def account_of(request: Request):
    s = get_session(request)
    if not s.get("aid"):
        return None
    acc = q("SELECT id, name, email, creation, type, coins FROM accounts WHERE id=%s",
            (s["aid"],), one=True)
    if acc:
        acc["is_admin"] = is_admin(acc)
    return acc


def is_admin(acc) -> bool:
    return bool(acc) and (acc.get("type", 1) >= ADMIN_ACCOUNT_TYPE
                          or (acc.get("email") or "").lower() in ADMIN_EMAILS)


def admin_of(request: Request):
    """The signed-in account, but only if it is allowed to run the place."""
    acc = account_of(request)
    return acc if is_admin(acc) else None


def admin_log(acc, action: str, target: str = "", detail: str = ""):
    """Every staff action lands in a file beside the app: who, what, when."""
    ADMIN_LOG.parent.mkdir(parents=True, exist_ok=True)
    with ADMIN_LOG.open("a") as fh:
        fh.write(json.dumps({"at": int(time.time()), "by": acc["name"],
                             "action": action, "target": target, "detail": detail}) + "\n")


def admin_history(limit: int = 200):
    try:
        lines = ADMIN_LOG.read_text().splitlines()
    except OSError:
        return []
    out = []
    for line in reversed(lines[-limit:]):
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def csrf_for(sid: str) -> str:
    return hmac.new(SECRET.encode(), f"csrf:{sid}".encode(), hashlib.sha256).hexdigest()[:32]


def csrf_token(request: Request) -> str:
    return csrf_for(get_session(request).get("sid", "anon"))


def csrf_ok(request: Request, token: str) -> bool:
    return hmac.compare_digest(csrf_token(request), token or "")


def render(request: Request, template: str, **ctx):
    ctx.setdefault("account", account_of(request))
    ctx.setdefault("status", server_status())
    csrf_sid = ctx.pop("csrf_sid", None)
    ctx["csrf"] = csrf_for(csrf_sid) if csrf_sid else csrf_token(request)
    ctx["request"] = request
    return templates.TemplateResponse(request, template, ctx)


def login_response(url: str, aid: int, existing_sid=None):
    resp = RedirectResponse(url, status_code=303)
    resp.set_cookie(
        "arkot", signer.dumps({"aid": aid, "sid": existing_sid or secrets.token_hex(8)}),
        max_age=86400 * 30, httponly=True, samesite="lax")
    return resp


def form_page(request: Request, template: str, **ctx):
    """Render a page with a form: the CSRF token and the (possibly new) session
    cookie must be derived from the SAME sid, so mint the sid first."""
    s = get_session(request)
    sid = s.get("sid")
    is_new = sid is None
    if is_new:
        sid = secrets.token_hex(8)
    resp = render(request, template, csrf_sid=sid, **ctx)
    if is_new:
        resp.set_cookie("arkot", signer.dumps({"sid": sid}),
                        max_age=86400, httponly=True, samesite="lax")
    return resp


# ---- shared queries -------------------------------------------------------
def server_status():
    online = q("SELECT count(*) c FROM players_online", one=True)["c"]
    accounts = q("SELECT count(*) c FROM accounts", one=True)["c"]
    players = q("SELECT count(*) c FROM players WHERE deletion=0", one=True)["c"]
    return {"online": online, "accounts": accounts, "characters": players}


def towns(world=None):
    if world is None:
        return q("SELECT id, name FROM towns ORDER BY id")
    return q(f"SELECT id, name FROM {world.sql}.towns ORDER BY id")


# ---- pages ----------------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    news = q(
        """SELECT n.title, n.text, n.date, COALESCE(p.name, 'Staff') author
           FROM znote_news n LEFT JOIN players p ON p.id = n.pid
           ORDER BY n.date DESC LIMIT 6""")
    top = q("SELECT name, level, vocation FROM players WHERE group_id=1 AND deletion=0 ORDER BY experience DESC LIMIT 5")
    return form_page(request, "index.html", news=news, top=top,
                     status=server_status(), now=int(time.time()))


@app.get("/news", response_class=HTMLResponse)
def news_archive(request: Request):
    news = q(
        """SELECT n.title, n.text, n.date, COALESCE(p.name, 'Staff') author
           FROM znote_news n LEFT JOIN players p ON p.id = n.pid
           ORDER BY n.date DESC LIMIT 100""")
    return form_page(request, "news.html", news=news)


@app.get("/register", response_class=HTMLResponse)
def register_form(request: Request):
    return form_page(request, "register.html", errors=[], form={})


@app.post("/register", response_class=HTMLResponse)
def register(request: Request, username: str = Form(""), email: str = Form(""),
             password: str = Form(""), password2: str = Form(""), token: str = Form("")):
    errors = []
    username = username.strip()
    email = email.strip()
    if not csrf_ok(request, token):
        errors.append("Session expired — please try again.")
    if not re.fullmatch(r"[a-zA-Z0-9]{4,32}", username):
        errors.append("Account name must be 4–32 letters/digits.")
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
        errors.append("That email address does not look valid.")
    if len(password) < 6 or len(password) > 29:
        errors.append("Password must be 6–29 characters.")
    if password != password2:
        errors.append("Passwords do not match.")
    if not errors and q("SELECT id FROM accounts WHERE name=%s", (username,), one=True):
        errors.append("That account name is taken.")
    if not errors and q("SELECT id FROM accounts WHERE email=%s", (email,), one=True):
        errors.append("An account with that email already exists.")
    if errors:
        return render(request, "register.html", errors=errors,
                      form={"username": username, "email": email})
    now = int(time.time())
    with db() as conn, conn.cursor() as cur:
        cur.execute(
            """INSERT INTO accounts (name, password, secret, type, premium_ends_at,
                                     email, creation, premdays, lastday)
               VALUES (%s, %s, '', 1, 0, %s, %s, 0, 0)""",
            (username, sha1(password), email, now))
        aid = cur.lastrowid
        cur.execute(
            """INSERT INTO znote_accounts (account_id, ip, created, points, cooldown,
                                           active, active_email, activekey, flag, secret)
               VALUES (%s, 0, %s, 0, 0, 1, 1, 0, 'us', '')""", (aid, now))
    return login_response("/account?welcome=1", aid, get_session(request).get("sid"))


@app.get("/login", response_class=HTMLResponse)
def login_form(request: Request):
    return form_page(request, "login.html", errors=[])


@app.post("/login", response_class=HTMLResponse)
def login(request: Request, username: str = Form(""), password: str = Form(""),
          token: str = Form("")):
    if not csrf_ok(request, token):
        return render(request, "login.html", errors=["Session expired — please try again."])
    ip = request_ip(request)
    if not LOGIN_LIMITER.allow(ip, username):
        return render(request, "login.html", errors=[ratelimit.MESSAGE])
    acc = q("SELECT id, password FROM accounts WHERE name=%s OR email=%s",
            (username.strip(), username.strip()), one=True)
    if not acc or not hmac.compare_digest(acc["password"], sha1(password)):
        LOGIN_LIMITER.failed(ip, username)
        return render(request, "login.html", errors=["Wrong account name/email or password."])
    LOGIN_LIMITER.succeeded(ip, username)
    return login_response("/account", acc["id"], get_session(request).get("sid"))


@app.get("/logout")
def logout():
    resp = RedirectResponse("/", status_code=303)
    resp.delete_cookie("arkot")
    return resp


@app.get("/account", response_class=HTMLResponse)
def account_page(request: Request, welcome: int = 0):
    acc = account_of(request)
    if not acc:
        return RedirectResponse("/login", status_code=303)
    return render(request, "account.html", chars=account_characters(acc["id"]), welcome=welcome)


@app.post("/account/password", response_class=HTMLResponse)
def change_password(request: Request, current: str = Form(""), new: str = Form(""),
                    new2: str = Form(""), token: str = Form("")):
    acc = account_of(request)
    if not acc:
        return RedirectResponse("/login", status_code=303)
    full = q("SELECT password FROM accounts WHERE id=%s", (acc["id"],), one=True)
    err = None
    if not csrf_ok(request, token):
        err = "Session expired — please try again."
    elif not hmac.compare_digest(full["password"], sha1(current)):
        err = "Current password is wrong."
    elif len(new) < 6 or len(new) > 29:
        err = "New password must be 6–29 characters."
    elif new != new2:
        err = "New passwords do not match."
    if err:
        return render(request, "account.html", chars=account_characters(acc["id"]), welcome=0,
                      pw_error=err)
    q("UPDATE accounts SET password=%s WHERE id=%s", (sha1(new), acc["id"]))
    revoke_sessions(acc["id"])
    return RedirectResponse("/account?pwchanged=1", status_code=303)


def creation_worlds():
    """The worlds a character can be made on: every world in the list, or none
    while the list is broken (a name could not be checked on every world)."""
    return WORLDS.worlds if WORLDS.ok else ()


def world_towns(world):
    if not world:
        return []
    try:
        return towns(world)
    except pymysql.MySQLError as err:
        log.warning("Character creation: could not read towns of world %r (schema %s): %s",
                    world.name, world.schema, type(err).__name__)
        return []


@app.get("/character/create", response_class=HTMLResponse)
def create_char_form(request: Request, world: int = -1):
    if not account_of(request):
        return RedirectResponse("/login", status_code=303)
    choices = creation_worlds()
    chosen = world_or_none(world) or (choices[0] if choices else None)
    errors = [] if choices else ["Character creation is unavailable right now. Please try again later."]
    return form_page(request, "create_character.html", errors=errors, towns=world_towns(chosen),
                     worlds=choices, form={"world": chosen.id if chosen else None})


@app.post("/character/create", response_class=HTMLResponse)
def create_char(request: Request, name: str = Form(""), vocation: int = Form(1),
                sex: int = Form(1), town: int = Form(1), world: int = Form(-1),
                token: str = Form("")):
    acc = account_of(request)
    if not acc:
        return RedirectResponse("/login", status_code=303)
    errors = []
    name = re.sub(r"\s+", " ", name.strip()).title()
    choices = creation_worlds()
    target = world_or_none(world)
    town_row = None
    if target:
        try:
            town_row = q(f"SELECT id, posx, posy, posz FROM {target.sql}.towns WHERE id=%s",
                         (town,), one=True)
        except pymysql.MySQLError as err:
            log.warning("Character creation: could not read towns of world %r: %s",
                        target.name, type(err).__name__)
            errors.append("That world is unavailable right now. Please try again later.")
    if not csrf_ok(request, token):
        errors.append("Session expired — please try again.")
    if not choices:
        errors.append("Character creation is unavailable right now. Please try again later.")
    elif not target:
        errors.append("Pick a world.")
    if not re.fullmatch(r"[a-zA-Z][a-zA-Z ]{1,28}[a-zA-Z]", name):
        errors.append("Name must be 3–30 letters (spaces allowed in the middle).")
    if vocation not in (1, 2, 3, 4):
        errors.append("Pick a vocation.")
    if sex not in (0, 1):
        errors.append("Pick a sex.")
    if target and not errors and not town_row:
        errors.append("Pick a town.")
    if not errors:
        # A name is refused if ANY world has it, so /character/<name> stays
        # unambiguous; the limit counts the account's characters on every
        # world. A world that cannot be read fails closed.
        try:
            taken = any(q(f"SELECT id FROM {w.sql}.players WHERE name=%s", (name,), one=True)
                        for w in choices)
            owned = sum(q(f"SELECT count(*) c FROM {w.sql}.players WHERE account_id=%s AND deletion=0",
                          (acc["id"],), one=True)["c"] for w in choices)
        except pymysql.MySQLError as err:
            log.warning("Character creation: could not check every world: %s", type(err).__name__)
            errors.append("Character creation is unavailable right now. Please try again later.")
        else:
            if taken:
                errors.append("That name is taken.")
            elif owned >= 10:
                errors.append("Character limit reached (10).")
    if errors:
        return render(request, "create_character.html", errors=errors, towns=world_towns(target),
                      worlds=choices,
                      form={"name": name, "vocation": vocation, "sex": sex, "town": town,
                            "world": target.id if target else None})
    looktype = 128 if sex == 1 else 136
    # Level-8 template mirrored from a Znote-created character verified in-game.
    with db() as conn, conn.cursor() as cur:
        cur.execute(f"""INSERT INTO {target.sql}.players
         (name, group_id, account_id, level, vocation, health, healthmax, experience,
          lookbody, lookfeet, lookhead, looklegs, looktype, lookaddons, direction,
          maglevel, mana, manamax, manaspent, soul, town_id, posx, posy, posz,
          conditions, cap, sex, lastlogin, lastip, save, skull, skulltime, lastlogout,
          blessings, onlinetime, deletion, balance, offlinetraining_time,
          offlinetraining_skill, stamina,
          skill_fist, skill_fist_tries, skill_club, skill_club_tries,
          skill_sword, skill_sword_tries, skill_axe, skill_axe_tries,
          skill_dist, skill_dist_tries, skill_shielding, skill_shielding_tries,
          skill_fishing, skill_fishing_tries)
         VALUES (%s, 1, %s, 8, %s, 185, 185, 4200,
                 68, 76, 78, 58, %s, 0, 2,
                 0, 90, 90, 0, 100, %s, %s, %s, %s,
                 '', 470, %s, 0, 0, 1, 0, 0, 0,
                 0, 0, 0, 0, 43200, -1, 2520,
                 10, 0, 10, 0, 10, 0, 10, 0, 10, 0, 10, 0, 10, 0)""",
                    (name, acc["id"], vocation, looktype, town_row["id"],
                     town_row["posx"], town_row["posy"], town_row["posz"], sex))
        pid = cur.lastrowid
    if target.schema != HOME_SCHEMA:
        # The legacy site (znote_players) and this site's character page only
        # know the home world, so a character elsewhere goes back to the account.
        return RedirectResponse("/account", status_code=303)
    q("INSERT INTO znote_players (player_id, created, hide_char, comment) VALUES (%s, %s, 0, '')",
      (pid, int(time.time())))
    return RedirectResponse(f"/character/{name}", status_code=303)


@app.get("/character/{name}", response_class=HTMLResponse)
def character(request: Request, name: str):
    p = q("""SELECT p.*, t.name town_name,
             (SELECT count(*) FROM players_online o WHERE o.player_id=p.id) online
             FROM players p LEFT JOIN towns t ON t.id=p.town_id
             WHERE p.name=%s AND p.deletion=0""", (name,), one=True)
    if not p:
        return render(request, "character.html", player=None, deaths=[], siblings=[])
    deaths = q("""SELECT time, level, killed_by, is_player FROM player_deaths
                  WHERE player_id=%s ORDER BY time DESC LIMIT 10""", (p["id"],))
    siblings = q("""SELECT name, level, vocation FROM players
                    WHERE account_id=%s AND deletion=0 AND id != %s ORDER BY level DESC""",
                 (p["account_id"], p["id"]))
    return render(request, "character.html", player=p, deaths=deaths, siblings=siblings)


@app.get("/highscores", response_class=HTMLResponse)
def highscores(request: Request, cat: str = "experience", page: int = 0):
    cat = cat if cat in HIGHSCORE_CATS else "experience"
    label, col, shown = HIGHSCORE_CATS[cat]
    page = max(0, min(page, 50))
    rows = q(f"""SELECT name, level, vocation, {col} value, {shown} shown
                 FROM players WHERE group_id=1 AND deletion=0
                 ORDER BY {col} DESC, name ASC LIMIT 25 OFFSET %s""", (page * 25,))
    return render(request, "highscores.html", rows=rows, cat=cat, label=label,
                  cats=HIGHSCORE_CATS, page=page)


@app.get("/online", response_class=HTMLResponse)
def online(request: Request):
    rows = q("""SELECT p.name, p.level, p.vocation FROM players_online o
                JOIN players p ON p.id = o.player_id ORDER BY p.level DESC""")
    return render(request, "online.html", rows=rows)


@app.get("/search", response_class=HTMLResponse)
def search(request: Request, name: str = ""):
    rows = []
    name = name.strip()
    if len(name) >= 2:
        rows = q("""SELECT name, level, vocation FROM players
                    WHERE name LIKE %s AND deletion=0 ORDER BY level DESC LIMIT 50""",
                 (f"%{name}%",))
    return render(request, "search.html", rows=rows, query=name)


@app.get("/deaths", response_class=HTMLResponse)
def deaths(request: Request):
    rows = q("""SELECT d.time, d.level, d.killed_by, d.is_player, p.name
                FROM player_deaths d JOIN players p ON p.id = d.player_id
                ORDER BY d.time DESC LIMIT 50""")
    return render(request, "deaths.html", rows=rows)


@app.get("/guilds", response_class=HTMLResponse)
def guilds(request: Request):
    rows = q("""SELECT g.name, g.motd, p.name leader,
                (SELECT count(*) FROM guild_membership m WHERE m.guild_id = g.id) members
                FROM guilds g JOIN players p ON p.id = g.ownerid ORDER BY members DESC""")
    return render(request, "guilds.html", rows=rows)


@app.get("/guild/{name}", response_class=HTMLResponse)
def guild(request: Request, name: str):
    g = q("SELECT id, name, motd, creationdata, ownerid FROM guilds WHERE name=%s", (name,), one=True)
    if not g:
        return render(request, "guild.html", guild=None, ranks=[])
    ranks = q("""SELECT r.name rank_name, r.level, p.name, p.level plevel, p.vocation, m.nick
                 FROM guild_ranks r
                 LEFT JOIN guild_membership m ON m.rank_id = r.id
                 LEFT JOIN players p ON p.id = m.player_id
                 WHERE r.guild_id = %s ORDER BY r.level DESC, p.level DESC""", (g["id"],))
    return render(request, "guild.html", guild=g, ranks=ranks)


@app.get("/houses", response_class=HTMLResponse)
def houses(request: Request, town: int = 0):
    where, args = "", []
    if town:
        where, args = "WHERE h.town_id = %s", [town]
    rows = q(f"""SELECT h.name, h.rent, h.size, h.beds, h.town_id, t.name town_name, p.name owner_name
                 FROM houses h LEFT JOIN towns t ON t.id = h.town_id
                 LEFT JOIN players p ON p.id = h.owner {where}
                 ORDER BY t.name, h.name""", args)
    return render(request, "houses.html", rows=rows, towns=towns(), town=town)


@app.get("/killstats", response_class=HTMLResponse)
def killstats(request: Request):
    top = q("""SELECT killed_by name, count(*) frags FROM player_deaths
               WHERE is_player = 1 GROUP BY killed_by ORDER BY frags DESC LIMIT 25""")
    recent = q("""SELECT d.time, d.level, d.killed_by, p.name victim
                  FROM player_deaths d JOIN players p ON p.id = d.player_id
                  WHERE d.is_player = 1 ORDER BY d.time DESC LIMIT 25""")
    return render(request, "killstats.html", top=top, recent=recent)


@app.get("/spells", response_class=HTMLResponse)
def spells(request: Request, cat: str = "all", voc: str = "all"):
    cats = sorted({s["category"] for s in SPELLS})
    rows = [s for s in SPELLS
            if (cat == "all" or s["category"] == cat)
            and (voc == "all" or voc in s["vocations"])]
    rows.sort(key=lambda s: (s["level"], s["name"]))
    return render(request, "spells.html", rows=rows, cats=cats, cat=cat, voc=voc)


LEVEL_BANDS = {
    "any": ("Any level", lambda lv: True),
    "open": ("No level given", lambda lv: lv == 0),
    "low": ("Level 1-50", lambda lv: 1 <= lv <= 50),
    "mid": ("Level 51-100", lambda lv: 51 <= lv <= 100),
    "high": ("Level 100+", lambda lv: lv > 100),
}


@app.get("/quests", response_class=HTMLResponse)
def quests(request: Request, tag: str = "all", access: str = "all", band: str = "any",
           find: str = Query("", alias="q")):
    tag = tag if tag in QUEST_TAGS else "all"
    band = band if band in LEVEL_BANDS else "any"
    access = access if access in ("free", "premium", "log") else "all"
    needle = find.strip().lower()
    keeps = LEVEL_BANDS[band][1]
    rows = [item for item in QUESTS["quests"]
            if (tag == "all" or tag in item["tags"])
            and (access == "all"
                 or (access == "free" and not item["premium"])
                 or (access == "premium" and item["premium"])
                 or (access == "log" and item["in_log"]))
            and keeps(item["level_sort"])
            and (not needle or needle in item["name"].lower()
                 or needle in item["location"].lower()
                 or needle in item["reward"].lower())]
    rows.sort(key=lambda item: (item["level_sort"], item["name"]))
    return render(request, "quests.html", rows=rows, tag=tag, access=access, band=band,
                  search=find.strip(), tags=QUEST_TAGS, bands=LEVEL_BANDS,
                  total=len(QUESTS["quests"]), built=QUESTS["built"])


@app.get("/quest/{slug}", response_class=HTMLResponse)
def quest(request: Request, slug: str):
    item = QUEST_BY_SLUG.get(slug)
    if not item:
        return RedirectResponse("/quests", status_code=303)
    missions = sum(len(m["steps"]) for m in item["missions"])
    return render(request, "quest.html", quest=item, steps=missions, tags=QUEST_TAGS)


@app.get("/changelog", response_class=HTMLResponse)
def changelog(request: Request):
    rows = q("SELECT text, time FROM znote_changelog ORDER BY time DESC LIMIT 50")
    return render(request, "changelog.html", rows=rows)


@app.get("/team", response_class=HTMLResponse)
def team(request: Request):
    rows = q("""SELECT name, group_id, lastlogin,
                (SELECT count(*) FROM players_online o WHERE o.player_id = players.id) online
                FROM players WHERE group_id > 1 AND deletion = 0 ORDER BY group_id DESC""")
    return render(request, "team.html", rows=rows, groups=GROUP_NAMES)


@app.get("/server", response_class=HTMLResponse)
def server(request: Request):
    return render(request, "server.html", status=server_status(), towns=towns(),
                  game_host=GAME_HOST, login_port=LOGIN_PORT, game_port=GAME_PORT)


def client_packages():
    """The manifest, checked against what is actually on disk.

    A package whose file is missing is still listed, marked as unavailable, so a
    half-finished upload reads as "not ready yet" instead of a broken link.
    """
    try:
        manifest = json.loads(DOWNLOADS_FILE.read_text())
    except (OSError, ValueError):
        return {"version": "", "base": "", "packages": []}
    for pkg in manifest.get("packages", []):
        path = DOWNLOADS_DIR / pkg["file"]
        try:
            pkg["bytes"] = path.stat().st_size
            pkg["ready"] = True
        except OSError:
            pkg["bytes"] = pkg.get("bytes", 0)
            pkg["ready"] = False
    return manifest


@app.get("/downloads", response_class=HTMLResponse)
def downloads(request: Request):
    return render(request, "downloads.html", client=client_packages(),
                  game_host=GAME_HOST, login_port=LOGIN_PORT)


@app.get("/rules", response_class=HTMLResponse)
def rules(request: Request):
    return render(request, "rules.html")


# ---- staff pages ----------------------------------------------------------
# Everything under /admin is gated on admin_of(); a signed-in player who is not
# staff is sent back to their own account page, and a stranger to the login form.
ACCOUNT_TYPES = {1: "Player", 2: "Tutor", 3: "Senior tutor", 4: "Gamemaster",
                 5: "Community manager", 6: "God"}
PLAYER_GROUPS = {1: "Player", 2: "Tutor", 3: "Senior tutor", 4: "Gamemaster",
                 5: "Community manager", 6: "God"}
NEWS_TITLE_MAX = 30       # znote_news.title is varchar(30)


def deny(request: Request):
    """Where a non-admin goes: signed in, back to their account; otherwise log in."""
    return RedirectResponse("/account" if account_of(request) else "/login", status_code=303)


def admin_page(request: Request, template: str, **ctx):
    ctx.setdefault("msg", "")
    ctx.setdefault("bad", "")
    return render(request, template, **ctx)


def back(where: str, msg: str = "", bad: str = ""):
    sep = "&" if "?" in where else "?"
    if msg:
        where += f"{sep}msg={urllib.parse.quote(msg)}"
    elif bad:
        where += f"{sep}bad={urllib.parse.quote(bad)}"
    return RedirectResponse(where, status_code=303)


@app.get("/admin", response_class=HTMLResponse)
def admin_home(request: Request, msg: str = "", bad: str = ""):
    acc = admin_of(request)
    if not acc:
        return deny(request)
    counts = q("""SELECT (SELECT count(*) FROM accounts) accounts,
                         (SELECT count(*) FROM players WHERE deletion=0) characters,
                         (SELECT count(*) FROM players_online) online,
                         (SELECT count(*) FROM account_bans) bans,
                         (SELECT count(*) FROM znote_news) news""", one=True)
    online = q("""SELECT p.id, p.name, p.level, p.vocation, p.group_id
                  FROM players_online o JOIN players p ON p.id = o.player_id
                  ORDER BY p.level DESC""")
    newest = q("""SELECT id, name, email, creation, type FROM accounts
                  ORDER BY creation DESC LIMIT 8""")
    chars = q("""SELECT id, name, level, vocation, account_id FROM players
                 WHERE deletion=0 ORDER BY id DESC LIMIT 8""")
    bans = q("""SELECT b.account_id, a.name, b.reason, b.expires_at
                FROM account_bans b LEFT JOIN accounts a ON a.id = b.account_id
                ORDER BY b.banned_at DESC LIMIT 8""")
    return admin_page(request, "admin.html", counts=counts, online=online, newest=newest,
                      chars=chars, bans=bans, recent=admin_history(8), msg=msg, bad=bad,
                      types=ACCOUNT_TYPES, now=int(time.time()))


# ---- news and changelog ---------------------------------------------------
@app.get("/admin/news", response_class=HTMLResponse)
def admin_news(request: Request, edit: int = 0, msg: str = "", bad: str = ""):
    acc = admin_of(request)
    if not acc:
        return deny(request)
    news = q("""SELECT n.id, n.title, n.text, n.date, n.pid, COALESCE(p.name, 'Staff') author
                FROM znote_news n LEFT JOIN players p ON p.id = n.pid
                ORDER BY n.date DESC LIMIT 50""")
    log = q("SELECT id, text, time FROM znote_changelog ORDER BY time DESC LIMIT 30")
    authors = q("""SELECT id, name FROM players WHERE account_id=%s AND deletion=0
                   ORDER BY level DESC""", (acc["id"],))
    editing = next((n for n in news if n["id"] == edit), None)
    return admin_page(request, "admin_news.html", news=news, log=log, authors=authors,
                      editing=editing, msg=msg, bad=bad, title_max=NEWS_TITLE_MAX)


@app.post("/admin/news")
def admin_news_save(request: Request, id: int = Form(0), title: str = Form(""),
                    text: str = Form(""), pid: int = Form(0), token: str = Form("")):
    acc = admin_of(request)
    if not acc:
        return deny(request)
    title, text = title.strip(), text.strip()
    if not csrf_ok(request, token):
        return back("/admin/news", bad="Session expired — please try again.")
    if not title or not text:
        return back("/admin/news", bad="A news post needs a title and a body.")
    if len(title) > NEWS_TITLE_MAX:
        return back("/admin/news", bad=f"The title has to fit in {NEWS_TITLE_MAX} characters.")
    if id:
        q("UPDATE znote_news SET title=%s, text=%s WHERE id=%s", (title, text, id))
        admin_log(acc, "news.edit", f"news {id}", title)
        return back("/admin/news", msg="News post updated.")
    q("INSERT INTO znote_news (title, text, date, pid) VALUES (%s, %s, %s, %s)",
      (title, text, int(time.time()), pid or 0))
    admin_log(acc, "news.post", "", title)
    return back("/admin/news", msg="News posted — it is on the front page now.")


@app.post("/admin/news/{id}/delete")
def admin_news_delete(request: Request, id: int, token: str = Form("")):
    acc = admin_of(request)
    if not acc:
        return deny(request)
    if not csrf_ok(request, token):
        return back("/admin/news", bad="Session expired — please try again.")
    row = q("SELECT title FROM znote_news WHERE id=%s", (id,), one=True)
    q("DELETE FROM znote_news WHERE id=%s", (id,))
    admin_log(acc, "news.delete", f"news {id}", (row or {}).get("title", ""))
    return back("/admin/news", msg="News post deleted.")


@app.post("/admin/changelog")
def admin_changelog_add(request: Request, text: str = Form(""), token: str = Form("")):
    acc = admin_of(request)
    if not acc:
        return deny(request)
    text = text.strip()
    if not csrf_ok(request, token):
        return back("/admin/news", bad="Session expired — please try again.")
    if not text:
        return back("/admin/news", bad="A changelog line needs some text.")
    q("INSERT INTO znote_changelog (text, time, report_id, status) VALUES (%s, %s, 0, 0)",
      (text[:255], int(time.time())))
    admin_log(acc, "changelog.add", "", text[:80])
    return back("/admin/news", msg="Changelog line added.")


@app.post("/admin/changelog/{id}/delete")
def admin_changelog_delete(request: Request, id: int, token: str = Form("")):
    acc = admin_of(request)
    if not acc:
        return deny(request)
    if not csrf_ok(request, token):
        return back("/admin/news", bad="Session expired — please try again.")
    q("DELETE FROM znote_changelog WHERE id=%s", (id,))
    admin_log(acc, "changelog.delete", f"entry {id}")
    return back("/admin/news", msg="Changelog line removed.")


# ---- accounts and characters ----------------------------------------------
@app.get("/admin/accounts", response_class=HTMLResponse)
def admin_accounts(request: Request, find: str = Query("", alias="q"),
                   msg: str = "", bad: str = ""):
    acc = admin_of(request)
    if not acc:
        return deny(request)
    needle = find.strip()
    if needle:
        like = f"%{needle}%"
        rows = q("""SELECT DISTINCT a.id, a.name, a.email, a.type, a.creation,
                           a.premium_ends_at, a.coins
                    FROM accounts a LEFT JOIN players p ON p.account_id = a.id
                    WHERE a.name LIKE %s OR a.email LIKE %s OR p.name LIKE %s
                    ORDER BY a.id LIMIT 50""", (like, like, like))
    else:
        rows = q("""SELECT id, name, email, type, creation, premium_ends_at, coins
                    FROM accounts ORDER BY creation DESC LIMIT 25""")
    return admin_page(request, "admin_accounts.html", rows=rows, search=needle,
                      types=ACCOUNT_TYPES, msg=msg, bad=bad, now=int(time.time()))


@app.get("/admin/account/{aid}", response_class=HTMLResponse)
def admin_account(request: Request, aid: int, msg: str = "", bad: str = ""):
    acc = admin_of(request)
    if not acc:
        return deny(request)
    target = q("""SELECT id, name, email, type, creation, premium_ends_at, coins,
                         coins_transferable FROM accounts WHERE id=%s""", (aid,), one=True)
    if not target:
        return back("/admin/accounts", bad="No such account.")
    chars = q("""SELECT p.id, p.name, p.level, p.vocation, p.group_id, p.lastlogin,
                        p.deletion, (SELECT count(*) FROM players_online o
                                     WHERE o.player_id = p.id) online
                 FROM players p WHERE p.account_id=%s ORDER BY p.level DESC""", (aid,))
    ban = q("""SELECT b.reason, b.banned_at, b.expires_at, a.name banned_by
               FROM account_bans b LEFT JOIN accounts a ON a.id = b.banned_by
               WHERE b.account_id=%s""", (aid,), one=True)
    history = q("""SELECT h.reason, h.banned_at, h.expired_at, a.name banned_by
                   FROM account_ban_history h LEFT JOIN accounts a ON a.id = h.banned_by
                   WHERE h.account_id=%s ORDER BY h.banned_at DESC LIMIT 10""", (aid,))
    return admin_page(request, "admin_account.html", target=target, chars=chars, ban=ban,
                      history=history, types=ACCOUNT_TYPES, groups=PLAYER_GROUPS,
                      msg=msg, bad=bad, now=int(time.time()), self_id=acc["id"])


@app.post("/admin/account/{aid}/premium")
def admin_premium(request: Request, aid: int, days: int = Form(0), token: str = Form("")):
    acc = admin_of(request)
    if not acc:
        return deny(request)
    where = f"/admin/account/{aid}"
    if not csrf_ok(request, token):
        return back(where, bad="Session expired — please try again.")
    row = q("SELECT premium_ends_at FROM accounts WHERE id=%s", (aid,), one=True)
    if not row:
        return back("/admin/accounts", bad="No such account.")
    now = int(time.time())
    if days == 0:
        ends = 0
    else:
        base = max(row["premium_ends_at"], now)
        ends = max(0, base + days * 86400)
        if ends <= now:
            ends = 0
    q("UPDATE accounts SET premium_ends_at=%s WHERE id=%s", (ends, aid))
    admin_log(acc, "account.premium", f"account {aid}",
              "cleared" if not ends else f"{days:+d} days")
    return back(where, msg="Premium cleared." if not ends else f"Premium changed by {days:+d} days.")


@app.post("/admin/account/{aid}/coins")
def admin_coins(request: Request, aid: int, coins: int = Form(0), token: str = Form("")):
    acc = admin_of(request)
    if not acc:
        return deny(request)
    where = f"/admin/account/{aid}"
    if not csrf_ok(request, token):
        return back(where, bad="Session expired — please try again.")
    row = q("SELECT coins FROM accounts WHERE id=%s", (aid,), one=True)
    if not row:
        return back("/admin/accounts", bad="No such account.")
    total = max(0, row["coins"] + coins)
    q("UPDATE accounts SET coins=%s WHERE id=%s", (total, aid))
    admin_log(acc, "account.coins", f"account {aid}", f"{coins:+d} (now {total})")
    return back(where, msg=f"Coins changed by {coins:+d}; the account now holds {total}.")


@app.post("/admin/account/{aid}/type")
def admin_account_type(request: Request, aid: int, type: int = Form(1), token: str = Form("")):
    acc = admin_of(request)
    if not acc:
        return deny(request)
    where = f"/admin/account/{aid}"
    if not csrf_ok(request, token):
        return back(where, bad="Session expired — please try again.")
    if type not in ACCOUNT_TYPES:
        return back(where, bad="That is not an account type.")
    q("UPDATE accounts SET type=%s WHERE id=%s", (type, aid))
    admin_log(acc, "account.type", f"account {aid}", ACCOUNT_TYPES[type])
    return back(where, msg=f"Account is now a {ACCOUNT_TYPES[type].lower()} account.")


@app.post("/admin/account/{aid}/password")
def admin_password(request: Request, aid: int, password: str = Form(""), token: str = Form("")):
    acc = admin_of(request)
    if not acc:
        return deny(request)
    where = f"/admin/account/{aid}"
    if not csrf_ok(request, token):
        return back(where, bad="Session expired — please try again.")
    if not 6 <= len(password) <= 29:
        return back(where, bad="A password has to be 6 to 29 characters.")
    q("UPDATE accounts SET password=%s WHERE id=%s", (sha1(password), aid))
    revoke_sessions(aid)
    admin_log(acc, "account.password", f"account {aid}", "reset")
    return back(where, msg="Password reset. Tell the owner to change it once they are in.")


@app.post("/admin/account/{aid}/ban")
def admin_ban(request: Request, aid: int, reason: str = Form(""), days: int = Form(0),
              token: str = Form("")):
    acc = admin_of(request)
    if not acc:
        return deny(request)
    where = f"/admin/account/{aid}"
    if not csrf_ok(request, token):
        return back(where, bad="Session expired — please try again.")
    if aid == acc["id"]:
        return back(where, bad="Banning yourself is not one of the duties.")
    target = q("SELECT type FROM accounts WHERE id=%s", (aid,), one=True)
    if not target:
        return back("/admin/accounts", bad="No such account.")
    reason = (reason.strip() or "No reason given")[:255]
    now = int(time.time())
    expires = now + days * 86400 if days > 0 else 0      # 0 = until it is lifted
    q("""INSERT INTO account_bans (account_id, reason, banned_at, expires_at, banned_by)
         VALUES (%s, %s, %s, %s, %s)
         ON DUPLICATE KEY UPDATE reason=VALUES(reason), banned_at=VALUES(banned_at),
                                 expires_at=VALUES(expires_at), banned_by=VALUES(banned_by)""",
      (aid, reason, now, expires, acc["id"]))
    revoke_sessions(aid)
    admin_log(acc, "account.ban", f"account {aid}",
              f"{reason} ({'permanent' if not expires else f'{days} days'})")
    return back(where, msg="Account banned.")


@app.post("/admin/account/{aid}/unban")
def admin_unban(request: Request, aid: int, token: str = Form("")):
    acc = admin_of(request)
    if not acc:
        return deny(request)
    where = f"/admin/account/{aid}"
    if not csrf_ok(request, token):
        return back(where, bad="Session expired — please try again.")
    row = q("SELECT reason, banned_at, banned_by FROM account_bans WHERE account_id=%s",
            (aid,), one=True)
    if not row:
        return back(where, bad="That account is not banned.")
    q("""INSERT INTO account_ban_history (account_id, reason, banned_at, expired_at, banned_by)
         VALUES (%s, %s, %s, %s, %s)""",
      (aid, row["reason"], row["banned_at"], int(time.time()), row["banned_by"]))
    q("DELETE FROM account_bans WHERE account_id=%s", (aid,))
    admin_log(acc, "account.unban", f"account {aid}", row["reason"])
    return back(where, msg="Ban lifted.")


@app.post("/admin/player/{pid}/group")
def admin_player_group(request: Request, pid: int, group: int = Form(1), token: str = Form("")):
    acc = admin_of(request)
    if not acc:
        return deny(request)
    player = q("""SELECT p.name, p.account_id,
                         (SELECT count(*) FROM players_online o WHERE o.player_id = p.id) online
                  FROM players p WHERE p.id=%s""", (pid,), one=True)
    if not player:
        return back("/admin/accounts", bad="No such character.")
    where = f"/admin/account/{player['account_id']}"
    if not csrf_ok(request, token):
        return back(where, bad="Session expired — please try again.")
    if group not in PLAYER_GROUPS:
        return back(where, bad="That is not a group.")
    if player["online"]:
        return back(where, bad=f"{player['name']} is online — the server would write the old "
                               "group back on its next save. Ask them to log out first.")
    q("UPDATE players SET group_id=%s WHERE id=%s", (group, pid))
    admin_log(acc, "player.group", player["name"], PLAYER_GROUPS[group])
    return back(where, msg=f"{player['name']} is now a {PLAYER_GROUPS[group].lower()}.")


@app.get("/admin/log", response_class=HTMLResponse)
def admin_log_page(request: Request, msg: str = "", bad: str = ""):
    acc = admin_of(request)
    if not acc:
        return deny(request)
    return admin_page(request, "admin_log.html", rows=admin_history(200), msg=msg, bad=bad)


# ---- the game client's launcher webservice -------------------------------
# The 12.x+ client posts {"type": ...} to Services.status (client init.lua)
# from the login screen: player counts, the boosted creature and boss of the
# day, the event calendar and the "show off" box.
CREATURES = json.loads((pathlib.Path(__file__).parent / "data" / "creatures.json").read_text())
EVENTS_FILE = pathlib.Path(__file__).parent / "data" / "events.json"


def boosted_of_the_day():
    """Deterministic pick by date so every client sees the same pair."""
    day = int(time.time() // 86400)
    pool = [c for c in CREATURES if c["raceid"] > 0]
    creature = pool[day % len(pool)]
    boss = pool[(day * 7 + 3) % len(pool)]
    return creature, boss


def load_events():
    try:
        return json.loads(EVENTS_FILE.read_text())
    except (OSError, ValueError):
        return []


@app.post("/api/client")
async def client_service(request: Request):
    try:
        body = await request.json()
    except ValueError:
        body = {}
    return client_service_reply(body)


def client_service_reply(body):
    """The answer to one launcher request, whichever route it came in on."""
    kind = (body or {}).get("type", "")
    if kind == "cacheinfo":
        online = q("SELECT count(*) c FROM players_online", one=True)["c"]
        return JSONResponse({"playersonline": online, "twitchstreams": 0, "twitchviewer": 0,
                             "gamingyoutubestreams": 0, "gamingyoutubeviewer": 0,
                             "discord_online": 0, "discord_link": "", "youtube_link": ""})
    if kind == "boostedcreature":
        creature, boss = boosted_of_the_day()
        return JSONResponse({"boostedcreature": True, "creatureraceid": creature["raceid"],
                             "bossraceid": boss["raceid"], "raceid": creature["raceid"]})
    if kind == "eventschedule":
        events = load_events()
        return JSONResponse({"eventlist": events, "lastupdatetimestamp": int(EVENTS_FILE.stat().st_mtime) if EVENTS_FILE.exists() else int(time.time())})
    if kind == "showoff":
        return JSONResponse({"title": "The real world is open",
                             "description": "Twenty-three cities, thousands of hunting grounds and every quest of the real world, played on the 15.25 client. Create an account at arkenfall.org and step off the boat in Thais.",
                             "image": "https://arkenfall.org/static/store/home/showoff_realworld.png"})
    return JSONResponse({"errorCode": 3, "errorMessage": "Unknown request type."})


@app.get("/api/status")
def api_status():
    return JSONResponse({**server_status(), "world": WORLD_NAME,
                         "host": GAME_HOST, "login": LOGIN_PORT, "game": GAME_PORT,
                         "protocol": "15.25"})


# ---- the game client's login ---------------------------------------------
# The client logs in over plain HTTP (OTClient httpLogin) through a proxy on
# port 7171 that rewrites every POST to this path; /login is the web form's.
# Answers are always HTTP 200 JSON, errors included, as the client expects.
@app.post("/api/login")
async def client_login(request: Request):
    body = bytearray()
    async for chunk in request.stream():
        body += chunk
        if len(body) > clientlogin.MAX_BODY:
            break
    data, refused = CLIENT_LOGIN.parse(bytes(body), request.headers.get("content-type", ""))
    if refused:
        reply = refused
    elif data.get("type") == "login":
        reply = await run_in_threadpool(CLIENT_LOGIN.login, data, request_ip(request))
    else:
        return await run_in_threadpool(client_service_reply, data)
    return JSONResponse(reply, headers={"Cache-Control": "no-store"})
