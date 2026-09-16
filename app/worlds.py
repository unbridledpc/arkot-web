"""The world list: which game worlds exist, where clients dial them, and which
database schema holds each one's characters.

It is read from the same `worlds.toml` the game servers boot from, so the site
and the servers can never disagree about a world's name — the name the client
is handed here is the name it announces to the game server, and a mismatch is
a refused login. The validation below mirrors the server's `Registry::Load`
(`src/world.cpp`): a file the server would refuse to boot from is refused here
too, as a whole, and never half-used.

One key is the site's own. `address` must equal the game server's bind IP (its
boot check insists), which on a real host is often 127.0.0.1 — useless to a
player. `public_address` is what clients are told to dial; the server ignores
it as an unknown key. Without it, `address` is advertised as before.

This module has no import-time side effects, so it can be tested on its own.
"""
import dataclasses
import logging
import os
import re
import tomllib

# A child of uvicorn's error logger, so these lines land in the container log
# with uvicorn's own formatting; outside uvicorn they propagate to the root.
log = logging.getLogger("uvicorn.error.arkot.worlds")

WHITESPACE = " \t\r\n"
SCHEMA_RE = re.compile(r"[A-Za-z0-9_]+")
PVP_TYPES = range(0, 5)          # the client's display labels (characterlist.lua)


class RegistryError(ValueError):
    """The world list is unusable; the message says why."""


@dataclasses.dataclass(frozen=True)
class World:
    id: int
    name: str
    address: str
    port: int
    schema: str
    public_address: str = ""
    pvptype: int = 0

    @property
    def dial_address(self) -> str:
        """What a client is told to connect to. Never `address` when a public
        address is declared: `address` is the server's bind IP."""
        return self.public_address or self.address

    @property
    def sql(self) -> str:
        """The schema as an SQL identifier. Safe to format into a statement:
        the name was checked against SCHEMA_RE when the row was built."""
        return f"`{self.schema}`"


def _ascii_lower(text: str) -> str:
    # The server compares names with per-byte tolower() in the C locale, which
    # only folds ASCII; str.lower() would also fold letters it leaves alone.
    return text.translate(_ASCII_FOLD)


_ASCII_FOLD = {c: c + 32 for c in range(ord("A"), ord("Z") + 1)}


def names_match(a: str, b: str) -> bool:
    """The server's NameMatches: trimmed, ASCII case-insensitive."""
    return _ascii_lower(a.strip(WHITESPACE)) == _ascii_lower(b.strip(WHITESPACE))


def _text(row: dict, key: str) -> str:
    # toml++ value_or<std::string>("") yields "" for a missing key or one of
    # another type; either way the field then counts as empty.
    value = row.get(key, "")
    return value.strip(WHITESPACE) if isinstance(value, str) else ""


def _integer(row: dict, key: str, missing: int) -> int:
    value = row.get(key, missing)
    # bool is an int subclass in Python; TOML true is not a number.
    if isinstance(value, bool) or not isinstance(value, int):
        return missing
    return value


def build_world(row: dict) -> World:
    """One [[world]] table, validated as Registry::Load does, plus the site's
    own rules (schema characters, public_address, pvptype)."""
    name = _text(row, "name")
    address = _text(row, "address")
    schema = _text(row, "schema")
    wid = _integer(row, "id", -1)
    port = _integer(row, "port", 0)
    if not name or not address or not schema or not 0 <= wid <= 255 or not 0 < port <= 65535:
        raise RegistryError(
            "a [[world]] entry is missing or out of range on a required field "
            f"(id, name, address, port, schema); id = {wid}, name = {name!r}")
    if not SCHEMA_RE.fullmatch(schema):
        raise RegistryError(f"world id {wid} has schema {schema!r}; only letters, digits and _ are allowed")

    public = row.get("public_address", "")
    if not isinstance(public, str):
        raise RegistryError(f"world id {wid} has a public_address that is not text")
    pvptype = row.get("pvptype", 0)
    if isinstance(pvptype, bool) or not isinstance(pvptype, int) or pvptype not in PVP_TYPES:
        raise RegistryError(f"world id {wid} has pvptype {pvptype!r}; it must be a whole number 0-4")
    return World(id=wid, name=name, address=address, port=port, schema=schema,
                 public_address=public.strip(WHITESPACE), pvptype=pvptype)


def parse_worlds(data: dict) -> tuple:
    """Every world in a parsed worlds.toml, in file order, or RegistryError."""
    rows = data.get("world")
    if not isinstance(rows, list) or not rows:
        raise RegistryError("the file declares no [[world]] entries")
    worlds = []
    for row in rows:
        if not isinstance(row, dict):
            raise RegistryError("the file contains a [[world]] element that is not a table")
        world = build_world(row)
        if any(w.id == world.id for w in worlds):
            raise RegistryError(f"the file declares world id {world.id} more than once")
        if any(names_match(w.name, world.name) for w in worlds):
            raise RegistryError(f"the file declares the world name {world.name!r} more than once")
        worlds.append(world)
    return tuple(worlds)


def parse_worlds_text(text: str) -> tuple:
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as err:
        raise RegistryError(f"the file could not be parsed: {err}") from None
    return parse_worlds(data)


@dataclasses.dataclass(frozen=True)
class Registry:
    """The loaded list. `worlds` is empty exactly when `error` is set, so a
    caller can never use part of a broken file."""
    worlds: tuple = ()
    error: str = ""
    source: str = ""

    @property
    def ok(self) -> bool:
        return not self.error and bool(self.worlds)

    def find(self, wid) -> "World | None":
        return next((w for w in self.worlds if w.id == wid), None)


def load(environ=None) -> Registry:
    """The registry the environment describes. Never raises: a broken file
    comes back as a Registry carrying the reason, which is also logged."""
    env = os.environ if environ is None else environ
    path = env.get("WORLDS_FILE", "")
    if not path:
        try:
            world = build_world({
                "id": 0,
                "name": env.get("WORLD_NAME", "Arkenfall"),
                "address": env.get("GAME_HOST", "bt.tibtool.com"),
                "port": int(env.get("GAME_PORT", "7172")),
                "schema": env.get("DB_NAME", "blacktek"),
            })
        except (RegistryError, ValueError) as err:
            log.error("World list: WORLDS_FILE is unset and the single-world fallback is invalid: %s", err)
            return Registry(error=f"fallback world is invalid: {err}", source="environment")
        log.warning("World list: WORLDS_FILE is unset; serving the single world %r at %s:%d "
                    "(schema %s) built from WORLD_NAME, GAME_HOST, GAME_PORT and DB_NAME. "
                    "Production must mount the game server's worlds.toml.",
                    world.name, world.dial_address, world.port, world.schema)
        return Registry(worlds=(world,), source="environment")
    try:
        with open(path, encoding="utf-8") as fh:
            worlds = parse_worlds_text(fh.read())
    except OSError as err:
        reason = f"{path} could not be read: {err.strerror or err}"
    except UnicodeDecodeError:
        reason = f"{path} is not UTF-8 text"
    except RegistryError as err:
        reason = f"{path}: {err}"
    else:
        log.info("World list: %d world(s) from %s: %s", len(worlds), path,
                 ", ".join(f"{w.id}={w.name!r}" for w in worlds))
        return Registry(worlds=worlds, source=path)
    log.error("World list is invalid, so client logins will be refused until it is fixed: %s", reason)
    return Registry(error=reason, source=path)
