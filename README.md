# Arkenfall website

The player-facing site for [Arkenfall](https://arkenfall.org), the free online role-playing
world served by [ArkOT](https://github.com/arken-fall/ArkOT) on the 15.25 client protocol.

A small FastAPI + Jinja2 application over the live game database: news, account creation and
login, character creation, highscores, who is online, character search, deaths, kill statistics,
guilds, houses, spells, server information and downloads. The layout is the classic
three-column game site: a carved menu on the left, news in the middle, account and world status
on the right, on a parchment sheet inside a stone frame. All art is CSS; nothing is copied from
any other site.

## Running

```
docker build -t arkot-web .
docker run -d --name arkotweb --network <game-db-network> \
  -e DB_HOST=btdb -e DB_USER=forgottenserver -e DB_PASS=... -e DB_NAME=blacktek \
  -e APP_SECRET=<random> -e WORLD_NAME=Arkenfall -e GAME_HOST=bt.tibtool.com \
  -v $PWD:/srv arkot-web
```

The app listens on 8090. `Caddyfile` shows the front door used in production: the site on `/`,
the legacy Znote pages under `/legacy/`.

## The quest section

`/quests` and `/quest/<slug>` are built from `app/data/quests.json`, which
`tools/build-quests.py` assembles from two sources: the game server's own quest data
(`data/quests/*.toml`, the in-game quest log, and `data/scripts/quests/`, what is actually
scripted) decides which quests exist here and what their missions are, and the Tibia Wiki's
quest infoboxes supply the facts a player wants first — starting town, level, premium, reward,
dangers. Only facts are taken from the wiki and every quest links back to its page; the
walkthroughs stay there. Wiki answers are cached under `build-client/`, so re-runs are free:

```
tools/build-quests.py [path-to-ArkOT-server] [--refresh]
```

A script folder earns a place on the site only if the wiki confirms it is a quest, which keeps
area and world-change scripts out; anything in the quest log is always listed, wiki page or not.

## The client download

`/downloads` hands out the game client itself. The packages are built by
`tools/build-client.sh`, which takes an upstream OTClient release — its source tree for the
Lua and data side, its published Windows and Linux binaries — applies `tools/client-overlay.patch`
(branding, the preconfigured Arkenfall server, a few interface fixes, no bot module) and the extra
files in `tools/client-files/`, then writes:

* `downloads/arkenfall-client-<version>-windows-x64.zip`
* `downloads/arkenfall-client-<version>-linux-x64.tar.gz`
* `app/data/downloads.json` — the manifest the page reads (sizes and SHA-256)

The packages carry no game graphics: the client fetches those on first start, so nothing here
redistributes anyone else's assets. `downloads/` is out of git — copy the two files to the
deploy's `downloads/` directory (it is mounted at `/srv/downloads`, `DOWNLOADS_DIR` overrides it)
and the site serves them from `/files/<name>`. A package the manifest names but that is not on
disk is shown as "Preparing" rather than a dead link.

Rebuild for a newer client with `UPSTREAM_TAG=4.2 CLIENT_VERSION=1.1 tools/build-client.sh`; if a
patch hunk stops applying, that file changed upstream and the fix wants re-checking.

Passwords are stored SHA-1 as the game server, the login webservice and Znote expect, so an
account created here logs into the game and the legacy site alike.
