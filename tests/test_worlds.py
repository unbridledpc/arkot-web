import logging

import pytest

from app import worlds
from app.worlds import RegistryError, parse_worlds_text

TWO_WORLDS = """
[[world]]
id      = 0
name    = "ArkOT"
address = "127.0.0.1"
public_address = "bt.tibtool.com"
port    = 7172
schema  = "blacktek"

[[world]]
id      = 1
name    = "ArkOT Test"
address = "127.0.0.1"
port    = 7272
schema  = "arkot_test"
"""


def row(**over):
    base = {"id": 0, "name": "ArkOT", "address": "127.0.0.1", "port": 7172, "schema": "blacktek"}
    base.update(over)
    return base


def toml_of(*rows):
    out = []
    for r in rows:
        out.append("[[world]]")
        for k, v in r.items():
            if isinstance(v, bool):
                out.append(f"{k} = {'true' if v else 'false'}")
            elif isinstance(v, (int, float)):
                out.append(f"{k} = {v}")
            else:
                out.append(f"{k} = {toml_string(v)}")
    return "\n".join(out) + "\n"


def toml_string(v):
    return '"' + v.replace("\\", "\\\\").replace('"', '\\"').replace("\t", "\\t") \
        .replace("\r", "\\r").replace("\n", "\\n") + '"'


def test_valid_file_keeps_order_and_fields():
    ws = parse_worlds_text(TWO_WORLDS)
    assert [w.id for w in ws] == [0, 1]
    assert [w.name for w in ws] == ["ArkOT", "ArkOT Test"]
    assert ws[0].schema == "blacktek" and ws[0].port == 7172
    assert ws[0].sql == "`blacktek`"


def test_file_order_is_kept_even_when_ids_are_not_sorted():
    ws = parse_worlds_text(toml_of(row(id=5, name="B", schema="b"), row(id=2, name="A", schema="a")))
    assert [w.id for w in ws] == [5, 2]


def test_public_address_is_what_clients_dial():
    ws = parse_worlds_text(TWO_WORLDS)
    assert ws[0].dial_address == "bt.tibtool.com"
    assert ws[0].address == "127.0.0.1"


def test_public_address_falls_back_to_address():
    ws = parse_worlds_text(TWO_WORLDS)
    assert ws[1].dial_address == "127.0.0.1"
    blank = parse_worlds_text(toml_of(row(public_address=" \t\r\n", address="game.example")))
    assert blank[0].dial_address == "game.example"


def test_public_address_is_trimmed():
    ws = parse_worlds_text(toml_of(row(public_address="  bt.tibtool.com\r\n")))
    assert ws[0].dial_address == "bt.tibtool.com"


def test_names_keep_inner_spaces_and_lose_outer_whitespace():
    ws = parse_worlds_text(toml_of(row(name=" \t ArkOT Test \r\n", schema=" blacktek\t")))
    assert ws[0].name == "ArkOT Test"
    assert ws[0].schema == "blacktek"


@pytest.mark.parametrize("text", [
    "",                                             # no [[world]] entries
    "world = []\n",
    "world = [1]\n",                                # element not a table
    "[[world\nid=0\n",                              # unparseable
])
def test_structural_refusals(text):
    with pytest.raises(RegistryError):
        parse_worlds_text(text)


@pytest.mark.parametrize("over", [
    {"id": -1}, {"id": 256}, {"id": "0"}, {"id": True}, {"id": 1.5},
    {"port": 0}, {"port": 65536}, {"port": -5}, {"port": "7172"},
    {"name": ""}, {"name": " \t\r\n"}, {"name": 5},
    {"address": ""}, {"address": "  "},
    {"schema": ""}, {"schema": "black-tek"}, {"schema": "a`b"}, {"schema": "a b"},
    {"schema": "blacktek; DROP TABLE x"},
    {"public_address": 5},
    {"pvptype": 7}, {"pvptype": "open"},
])
def test_field_refusals(over):
    with pytest.raises(RegistryError):
        parse_worlds_text(toml_of(row(**over)))


@pytest.mark.parametrize("missing", ["id", "name", "address", "port", "schema"])
def test_missing_required_field(missing):
    r = row()
    del r[missing]
    with pytest.raises(RegistryError):
        parse_worlds_text(toml_of(r))


def test_boundaries_accepted():
    ws = parse_worlds_text(toml_of(row(id=255, port=65535), row(id=0, name="Other", port=1)))
    assert [(w.id, w.port) for w in ws] == [(255, 65535), (0, 1)]


def test_duplicate_id_refused():
    with pytest.raises(RegistryError, match="id 0"):
        parse_worlds_text(toml_of(row(), row(name="Other")))


@pytest.mark.parametrize("second", ["arkot", "ARKOT", " ArkOT ", "ArkOT\r\n", "\tarkOT"])
def test_duplicate_name_is_case_and_whitespace_insensitive(second):
    with pytest.raises(RegistryError, match="more than once"):
        parse_worlds_text(toml_of(row(), row(id=1, name=second)))


def test_names_differing_only_by_inner_space_are_distinct():
    ws = parse_worlds_text(toml_of(row(name="ArkOT Test"), row(id=1, name="ArkOTTest")))
    assert len(ws) == 2


def test_names_match_follows_the_server():
    assert worlds.names_match(" ArkOT Test\r\n", "arkot test")
    assert not worlds.names_match("ArkOT", "ArkOT Test")


def test_unknown_keys_are_ignored():
    ws = parse_worlds_text(toml_of(row(slug="x", motd="hello")))
    assert ws[0].name == "ArkOT"


def test_load_reads_the_file(tmp_path):
    f = tmp_path / "worlds.toml"
    f.write_text(TWO_WORLDS)
    reg = worlds.load({"WORLDS_FILE": str(f)})
    assert reg.ok and [w.name for w in reg.worlds] == ["ArkOT", "ArkOT Test"]
    assert reg.find(1).name == "ArkOT Test" and reg.find(9) is None


def test_load_invalid_file_has_no_worlds_and_logs_reason(tmp_path, caplog):
    f = tmp_path / "worlds.toml"
    f.write_text(toml_of(row(), row(name="arkot", id=1)))
    with caplog.at_level(logging.ERROR):
        reg = worlds.load({"WORLDS_FILE": str(f)})
    assert not reg.ok and reg.worlds == () and "more than once" in reg.error
    assert "more than once" in caplog.text


def test_load_missing_file_is_invalid(tmp_path):
    reg = worlds.load({"WORLDS_FILE": str(tmp_path / "nope.toml")})
    assert not reg.ok and reg.worlds == ()


def test_load_without_file_builds_fallback_and_warns(caplog):
    with caplog.at_level(logging.WARNING):
        reg = worlds.load({"WORLD_NAME": "ArkOT", "GAME_HOST": "bt.tibtool.com",
                           "GAME_PORT": "7172", "DB_NAME": "blacktek"})
    assert reg.ok and len(reg.worlds) == 1
    w = reg.worlds[0]
    assert (w.id, w.name, w.dial_address, w.port, w.schema) == (0, "ArkOT", "bt.tibtool.com", 7172, "blacktek")
    assert "WORLDS_FILE is unset" in caplog.text


def test_load_fallback_with_bad_schema_is_invalid():
    reg = worlds.load({"DB_NAME": "bad-name"})
    assert not reg.ok and reg.worlds == ()
