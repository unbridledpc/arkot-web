#!/usr/bin/env python3
"""Builds app/data/quests.json — the data behind the site's quest section.

Two sources meet here:

* the game server itself (`data/quests/*.toml`, the quest log the player sees in
  game, and `data/scripts/quests/`, what is actually scripted), which decides
  *which* quests exist on Arkenfall and what their missions are;
* the Tibia Wiki's quest infoboxes, for the facts a player wants before starting
  one — where it begins, the level it wants, premium, the reward, the dangers.

Only facts are taken from the wiki, and every quest keeps a link back to the page
they came from; the walkthroughs stay over there. Wiki answers are cached in the
work directory, so a re-run costs no requests.

    tools/build-quests.py [path-to-ArkOT-server] [--refresh]
"""
import html
import json
import os
import pathlib
import re
import sys
import time
import tomllib
import urllib.parse
import urllib.request

REPO = pathlib.Path(__file__).resolve().parent.parent
SERVER = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 and not sys.argv[1].startswith("-")
                      else os.path.expanduser("~/Documents/BlackTek-Server"))
OUT = REPO / "app" / "data" / "quests.json"
CACHE = REPO / "build-client" / "wiki-quests.json"
API = "https://tibia.fandom.com/api.php"
AGENT = "ArkenfallSiteBot/1.0 (https://arkenfall.org; quest metadata for a fan server)"
REFRESH = "--refresh" in sys.argv

# Quests whose folder name or log name does not lead to the wiki page on its own.
TITLE_HINTS = {
    "adventurers_guild": "Adventurers Guild (Quest)",
    "a_pirates_tail": "A Pirate's Tail Quest",
    "barbarian_test": "The Barbarian Test",
    "bigfoot_burden": "Bigfoot's Burden Quest",
    "dangerous_depth": "Dangerous Depths Quest",
    "deeper_fibula": "Deeper Fibula Quest",
    "demon_oak": "Demon Oak Quest",
    "devil_helmet": "Devil Helmet Quest",
    "edron_rope": "Edron Rope Quest",
    "extension_mota": "Mind Over Matter Quest",
    "fathers_burden": "A Father's Burden",
    "formogar_mine_hoist": "The Ice Islands Quest",
    "giant_smithhammer": "Giant Smithhammer Quest",
    "killing_in_the_name_of": "Killing in the Name of... Quest",
    "koshei_the_deathless_quest": "Koshei The Deathless Quest",
    "liquid_black": "Liquid Black Quest",
    "mysterious_ornate": "Mysterious Ornate Chest Quest",
    "rottin_wood_and_married_men": "Rottin' Wood and Married Men Quest",
    "the_order_of_lion": "The Order of the Lion Quest",
    "their_masters_voice": "Their Master's Voice Quest",
    "thieves_guild": "The Thieves Guild Quest",
    "tinder_box_quest_chyllfroest": "The Tinder Box Quest",
    "waterfall": "Waterfall Quest",
    "white_pearl": "White Pearl Quest",
}

# Folders that hold shared plumbing rather than a quest of their own.
NOT_A_QUEST = {"others", "lib"}
STOPWORDS = {"the", "of", "a", "an", "and", "quest", "quests", "in", "to", "for", "s"}

# Folders that implement a quest the log already names differently.
SAME_QUEST = {
    "spirit_hunters": "spirithunters",
    "the_spike_tasks": "spike-task",
    "spike_tasks": "spike-task",
    "the_djinn_war_quest": "the-djinn-war-efreet-faction",
}


def slugify(text):
    """The site's id for a quest. "X", "X Quest" and "X (Quest)" are one quest."""
    text = re.sub(r"\s*\((?:quest)\)$", "", text.strip(), flags=re.I)
    text = re.sub(r"\s+quests?$", "", text, flags=re.I)
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


# ---- the server's own quest data -------------------------------------------
def quest_log():
    """The in-game quest log: quest name, its missions and each mission's steps."""
    out = {}
    for path in sorted((SERVER / "data" / "quests").glob("*.toml")):
        for key, q in tomllib.loads(path.read_text()).items():
            missions = [{
                "name": m.get("name", ""),
                "steps": [s.get("description", "") for s in m.get("states", [])],
            } for m in q.get("missions", [])]
            name = q.get("name") or key
            out[slugify(name)] = {"name": name, "missions": missions, "source": path.name}
    return out


def scripted():
    """Folder names under data/scripts/quests — quests the server actually runs."""
    root = SERVER / "data" / "scripts" / "quests"
    return sorted(d.name for d in root.iterdir()
                  if d.is_dir() and d.name not in NOT_A_QUEST)


def titled(folder):
    words = folder.replace("_quest", "").split("_")
    small = {"of", "the", "in", "and", "for", "a", "to"}
    title = " ".join(w.capitalize() if i == 0 or w not in small else w
                     for i, w in enumerate(words))
    return title


# ---- the wiki's facts -------------------------------------------------------
def api(params):
    params = dict(params, format="json", formatversion="2")
    url = API + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": AGENT})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())


def strip_markup(text):
    text = re.sub(r"<!--.*?-->", "", text, flags=re.S)
    text = re.sub(r"<br\s*/?>", ", ", text, flags=re.I)
    text = re.sub(r"\{\{[^{}]*\}\}", "", text)          # inline templates
    text = re.sub(r"\[\[[^\]|]*\|([^\]]*)\]\]", r"\1", text)  # [[Page|label]]
    text = re.sub(r"\[\[([^\]]*)\]\]", r"\1", text)      # [[Page]]
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"[\r\n]+\s*[*#]+\s*", "; ", text)   # a wiki list reads as clauses here
    text = re.sub(r"(?m)^[*#:;]+\s*", "", text)
    text = text.replace("'''", "").replace("''", "")
    text = re.sub(r"\s*,\s*,", ",", text)
    return re.sub(r"\s+", " ", html.unescape(text)).strip(" ,.")


def infobox(wikitext):
    """The fields of an {{Infobox Quest}}, flattened to plain text."""
    start = wikitext.find("{{Infobox Quest")
    if start < 0:
        return None
    depth, i = 0, start
    while i < len(wikitext):
        if wikitext.startswith("{{", i):
            depth += 1
            i += 2
        elif wikitext.startswith("}}", i):
            depth -= 1
            i += 2
            if depth == 0:
                break
        else:
            i += 1
    body = wikitext[start:i]
    fields, depth, cur = {}, 0, ""
    for ch in body[2:-2]:
        if ch in "{[":
            depth += 1
        elif ch in "}]":
            depth -= 1
        if ch == "|" and depth == 0:
            if "=" in cur:
                k, v = cur.split("=", 1)
                fields[k.strip().lower()] = v.strip()
            cur = ""
        else:
            cur += ch
    if "=" in cur:
        k, v = cur.split("=", 1)
        fields[k.strip().lower()] = v.strip()
    return {k: strip_markup(v) for k, v in fields.items()}


def fetch_pages(titles, cache):
    """Wiki text for a batch of titles, following redirects, cached on disk."""
    wanted = [t for t in titles if t not in cache]
    for i in range(0, len(wanted), 20):
        batch = wanted[i:i + 20]
        data = api({"action": "query", "prop": "revisions", "rvprop": "content",
                    "rvslots": "main", "redirects": "1", "titles": "|".join(batch)})
        query = data.get("query", {})
        resolved = {r["from"]: r["to"] for r in query.get("redirects", [])}
        normal = {n["from"]: n["to"] for n in query.get("normalized", [])}
        pages = {}
        for page in query.get("pages", []):
            if page.get("missing"):
                pages[page["title"]] = None
                continue
            pages[page["title"]] = page["revisions"][0]["slots"]["main"]["content"]
        for title in batch:
            final = normal.get(title, title)
            final = resolved.get(final, final)
            cache[title] = {"title": final, "text": pages.get(final)}
        time.sleep(0.5)
    return cache


def search_titles(name, cache):
    """Ask the wiki to find the quest we could not name, and keep only a page
    whose title really is about the same quest."""
    try:
        data = api({"action": "query", "list": "search", "srlimit": "5",
                    "srsearch": f'intitle:quest {name}'})
    except Exception:
        return []
    hits = [h["title"] for h in data.get("query", {}).get("search", [])]
    if not hits:
        return []
    fetch_pages(hits, cache)
    words = {w for w in re.findall(r"[a-z]+", name.lower()) if w not in STOPWORDS}
    keep = []
    for title in hits:
        text = (cache.get(title) or {}).get("text") or ""
        if "{{Infobox Quest" not in text:
            continue
        theirs = {w for w in re.findall(r"[a-z]+", title.lower()) if w not in STOPWORDS}
        if words and len(words & theirs) / len(words) >= 0.6:
            keep.append(title)
    return keep


def first_number(text):
    """"10 - 80+" advises level 10, not 1080."""
    found = re.search(r"\d+", text or "")
    return int(found.group()) if found else 0


def reward_tags(reward):
    """What a player gets out of it, in the words the reward line uses."""
    text = reward.lower()
    tags = []
    # whole words only: "Mount Sternum" and "the mountain" are not mounts
    for tag, pattern in (("mount", r"\bmounts?\b"), ("outfit", r"\boutfits?\b"),
                         ("addon", r"\baddons?\b"), ("access", r"\b(?:access|passage)\b"),
                         ("experience", r"\b(?:exp|experience)\b")):
        if re.search(pattern, text):
            tags.append(tag)
    if reward and not tags:
        tags.append("treasure")
    return tags


def main():
    log = quest_log()
    folders = scripted()

    # every quest we know of, and the wiki titles worth trying for it
    entries = {}
    for slug, q in log.items():
        entries[slug] = {"name": q["name"], "missions": q["missions"],
                         "in_log": True, "scripted": False, "source": q["source"]}
    for folder in folders:
        name = TITLE_HINTS.get(folder, titled(folder))
        slug = SAME_QUEST.get(folder) or slugify(name)
        if slug in entries:
            entries[slug]["scripted"] = True
            continue
        entries[slug] = {"name": name.replace(" (Quest)", ""), "missions": [],
                         "in_log": False, "scripted": True, "source": folder}

    candidates = {}
    for slug, e in entries.items():
        base = e["name"]
        tries = [base]
        if not base.endswith("Quest"):
            tries += [base + " Quest", base + " (Quest)"]
        candidates[slug] = tries

    cache = {}
    if CACHE.exists() and not REFRESH:
        cache = json.loads(CACHE.read_text())
    CACHE.parent.mkdir(parents=True, exist_ok=True)
    fetch_pages([t for tries in candidates.values() for t in tries], cache)

    # whatever the guessed titles missed, ask the wiki's own search for
    extra = {}
    for slug, e in entries.items():
        if any((cache.get(t) or {}).get("text") for t in candidates[slug]):
            continue
        found = search_titles(e["name"], cache)
        if found:
            extra[slug] = found
    CACHE.write_text(json.dumps(cache))

    quests, matched = [], 0
    for slug, e in sorted(entries.items()):
        facts, page = {}, None
        for title in candidates[slug] + extra.get(slug, []):
            hit = cache.get(title) or {}
            if not hit.get("text"):
                continue
            if "{{Infobox Quest" in hit["text"]:
                facts = infobox(hit["text"]) or {}
                page = hit["title"]
                break
            page = page or hit["title"]      # a collection page: link it, no facts
        if facts:
            matched += 1
        if not facts and not e["in_log"]:
            continue     # an area or world-change script, not a quest of its own
        lvl = facts.get("lvl", "") or "0"
        quests.append({
            "slug": slug,
            "name": e["name"] if e["in_log"] else (facts.get("name") or e["name"]),
            "in_log": e["in_log"],
            "scripted": e["scripted"],
            "missions": e["missions"],
            "location": facts.get("location", ""),
            "level": int(lvl) if lvl.isdigit() else 0,
            "level_rec": facts.get("lvlrec", ""),
            "premium": facts.get("premium", "").lower().startswith("y"),
            "reward": facts.get("reward", ""),
            "dangers": facts.get("dangers", ""),
            "aka": facts.get("aka", ""),
            "implemented": facts.get("implemented", ""),
            "tags": reward_tags(facts.get("reward", "")),
            "level_sort": int(lvl) if lvl.isdigit() and lvl != "0" else first_number(
                facts.get("lvlrec", "")),
            "wiki": ("https://tibia.fandom.com/wiki/" +
                     urllib.parse.quote(page.replace(" ", "_"))) if page else "",
        })

    OUT.write_text(json.dumps({
        "built": time.strftime("%Y-%m-%d"),
        "quests": quests,
    }, indent=1, ensure_ascii=False) + "\n")
    print(f"{OUT}: {len(quests)} quests, {matched} with wiki facts, "
          f"{sum(len(q['missions']) for q in quests)} missions")


if __name__ == "__main__":
    main()
