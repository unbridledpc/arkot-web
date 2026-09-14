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

Passwords are stored SHA-1 as the game server, the login webservice and Znote expect, so an
account created here logs into the game and the legacy site alike.
