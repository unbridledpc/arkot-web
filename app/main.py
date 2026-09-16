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

import json
import pathlib

import pymysql
from fastapi import FastAPI, Form, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from itsdangerous import BadSignature, URLSafeSerializer

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

SITE_NAME = "Arkenfall"
WORLD_NAME = os.environ.get("WORLD_NAME", "Arkenfall")
GAME_HOST = os.environ.get("GAME_HOST", "bt.tibtool.com")
LOGIN_PORT = 7171
GAME_PORT = 7172

VOCATIONS = {0: "None", 1: "Sorcerer", 2: "Druid", 3: "Paladin", 4: "Knight",
             5: "Master Sorcerer", 6: "Elder Druid", 7: "Royal Paladin", 8: "Elite Knight"}
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
                        ("/quest/", "library"), ("/account", "account"), ("/news", "news")):
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
    return q("SELECT id, name, email, creation FROM accounts WHERE id=%s", (s["aid"],), one=True)


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


def towns():
    return q("SELECT id, name FROM towns ORDER BY id")


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
    acc = q("SELECT id, password FROM accounts WHERE name=%s OR email=%s",
            (username.strip(), username.strip()), one=True)
    if not acc or not hmac.compare_digest(acc["password"], sha1(password)):
        return render(request, "login.html", errors=["Wrong account name/email or password."])
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
    chars = q("""SELECT name, level, vocation, town_id, lastlogin,
                 (SELECT count(*) FROM players_online o WHERE o.player_id = players.id) online
                 FROM players WHERE account_id=%s AND deletion=0 ORDER BY level DESC""",
              (acc["id"],))
    return render(request, "account.html", chars=chars, welcome=welcome,
                  towns={t["id"]: t["name"] for t in towns()})


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
        chars = q("SELECT name, level, vocation, town_id, lastlogin, 0 online FROM players WHERE account_id=%s AND deletion=0", (acc["id"],))
        return render(request, "account.html", chars=chars, welcome=0, pw_error=err,
                      towns={t["id"]: t["name"] for t in towns()})
    q("UPDATE accounts SET password=%s WHERE id=%s", (sha1(new), acc["id"]))
    return RedirectResponse("/account?pwchanged=1", status_code=303)


@app.get("/character/create", response_class=HTMLResponse)
def create_char_form(request: Request):
    if not account_of(request):
        return RedirectResponse("/login", status_code=303)
    return form_page(request, "create_character.html", errors=[], towns=towns(), form={})


@app.post("/character/create", response_class=HTMLResponse)
def create_char(request: Request, name: str = Form(""), vocation: int = Form(1),
                sex: int = Form(1), town: int = Form(1), token: str = Form("")):
    acc = account_of(request)
    if not acc:
        return RedirectResponse("/login", status_code=303)
    errors = []
    name = re.sub(r"\s+", " ", name.strip()).title()
    town_row = q("SELECT id, posx, posy, posz FROM towns WHERE id=%s", (town,), one=True)
    if not csrf_ok(request, token):
        errors.append("Session expired — please try again.")
    if not re.fullmatch(r"[a-zA-Z][a-zA-Z ]{1,28}[a-zA-Z]", name):
        errors.append("Name must be 3–30 letters (spaces allowed in the middle).")
    if vocation not in (1, 2, 3, 4):
        errors.append("Pick a vocation.")
    if sex not in (0, 1):
        errors.append("Pick a sex.")
    if not town_row:
        errors.append("Pick a town.")
    if not errors and q("SELECT id FROM players WHERE name=%s", (name,), one=True):
        errors.append("That name is taken.")
    if not errors and len(q("SELECT id FROM players WHERE account_id=%s AND deletion=0", (acc["id"],))) >= 10:
        errors.append("Character limit reached (10).")
    if errors:
        return render(request, "create_character.html", errors=errors, towns=towns(),
                      form={"name": name, "vocation": vocation, "sex": sex, "town": town})
    looktype = 128 if sex == 1 else 136
    # Level-8 template mirrored from a Znote-created character verified in-game.
    q("""INSERT INTO players
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
    pid = q("SELECT id FROM players WHERE name=%s", (name,), one=True)["id"]
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
                             "image": "https://arkenfall.org/static/store/home/banner_realworld.png"})
    return JSONResponse({"errorCode": 3, "errorMessage": "Unknown request type."})


@app.get("/api/status")
def api_status():
    return JSONResponse({**server_status(), "world": WORLD_NAME,
                         "host": GAME_HOST, "login": LOGIN_PORT, "game": GAME_PORT,
                         "protocol": "15.25"})
