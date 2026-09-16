#!/bin/bash
# Builds the Arkenfall client packages the site hands out.
#
# The client is OTClient (opentibiabr/otclient), MIT licensed: this script takes
# an upstream release — its source tree for the Lua/data side, its published
# binaries for Windows and Linux — applies the Arkenfall overlay (branding, the
# preconfigured server, a handful of interface fixes) and writes the two archives
# plus the manifest the /downloads page reads.
#
# It ships NO game graphics: the client fetches those itself on first start, so
# nothing here redistributes CipSoft's assets.
#
#   tools/build-client.sh [work-dir]
#
# Downloads are cached in the work directory, so a rebuild after editing the
# overlay costs no bandwidth.
set -euo pipefail

UPSTREAM_TAG=${UPSTREAM_TAG:-4.1}
CLIENT_VERSION=${CLIENT_VERSION:-1.0}
REPO=$(cd "$(dirname "$0")/.." && pwd)
WORK=${1:-$REPO/build-client}
OUT=$REPO/downloads
BASE=https://github.com/opentibiabr/otclient

mkdir -p "$WORK" "$OUT"
cd "$WORK"

fetch() { # url file
	[ -s "$2" ] || { echo "fetching $2"; curl -fsSL --retry 3 -o "$2" "$1"; }
}

fetch "$BASE/archive/refs/tags/$UPSTREAM_TAG.tar.gz"                            "src.tar.gz"
fetch "$BASE/releases/download/$UPSTREAM_TAG/otclient-windows-solution-directx.zip" "win.zip"
fetch "$BASE/releases/download/$UPSTREAM_TAG/otclient-linux-release.zip"            "linux.zip"

rm -rf src stage
mkdir -p src
tar xzf src.tar.gz -C src --strip-components=1

# --- the tree both platforms share -------------------------------------------
STAGE=$WORK/stage/Arkenfall
mkdir -p "$STAGE"
cp -a src/data src/modules src/mods "$STAGE"/
cp -a src/init.lua src/otclientrc.lua src/meta.lua src/config.ini \
      src/cacert.pem src/LICENSE src/AUTHORS "$STAGE"/

# Arkenfall's rules ban bots and macros, so the official download ships without one.
# The mod loader lists it in load-later and refuses to boot if it is simply missing.
rm -rf "$STAGE/mods/game_bot"
sed -i '/^    - game_bot$/d' "$STAGE/mods/client_mods/mods.otmod"
grep -q game_bot "$STAGE/mods/client_mods/mods.otmod" && { echo "game_bot still referenced" >&2; exit 1; }

patch -p1 -d "$STAGE" --no-backup-if-mismatch < "$REPO/tools/client-overlay.patch"

# The user script hook is named after the compact name init.lua sets, so the
# renamed client looks for arkenfallrc.lua and would ignore otclientrc.lua.
mv "$STAGE/otclientrc.lua" "$STAGE/arkenfallrc.lua"

cp -a "$REPO"/tools/client-files/. "$STAGE"/
chmod +x "$STAGE/arkenfall.sh"

# --- Windows -----------------------------------------------------------------
rm -rf win && mkdir win
unzip -q win.zip otclient_dx_x64.exe -d win          # the .pdb in the zip is debug symbols
cp -a "$WORK/stage/Arkenfall" win/Arkenfall
mv win/otclient_dx_x64.exe win/Arkenfall/Arkenfall.exe
rm -f win/Arkenfall/arkenfall.sh
win_zip=$OUT/arkenfall-client-$CLIENT_VERSION-windows-x64.zip
rm -f "$win_zip"
(cd win && zip -qr9 "$win_zip" Arkenfall)

# --- Linux -------------------------------------------------------------------
rm -rf lin && mkdir lin
unzip -q linux.zip otclient -d lin
strip lin/otclient                                    # the release binary carries debug info
cp -a "$WORK/stage/Arkenfall" lin/Arkenfall
mv lin/otclient lin/Arkenfall/arkenfall
chmod +x lin/Arkenfall/arkenfall
lin_tar=$OUT/arkenfall-client-$CLIENT_VERSION-linux-x64.tar.gz
rm -f "$lin_tar"
(cd lin && tar czf "$lin_tar" Arkenfall)

# --- the manifest the site reads ---------------------------------------------
MANIFEST=$REPO/app/data/downloads.json python3 - "$CLIENT_VERSION" "$UPSTREAM_TAG" "$win_zip" "$lin_tar" <<'PY'
import hashlib, json, os, pathlib, sys, time

version, upstream, *files = sys.argv[1:]
manifest = pathlib.Path(os.environ["MANIFEST"])

def entry(path, platform, requirement):
    data = pathlib.Path(path)
    digest = hashlib.sha256(data.read_bytes()).hexdigest()
    return {"platform": platform, "file": data.name, "bytes": data.stat().st_size,
            "sha256": digest, "requires": requirement}

manifest.write_text(json.dumps({
    "version": version,
    "base": f"OTClient {upstream}",
    "protocol": "15.25",
    "released": time.strftime("%Y-%m-%d"),
    "packages": [
        entry(files[0], "Windows", "Windows 10 or newer, 64-bit"),
        entry(files[1], "Linux", "64-bit Linux desktop with OpenGL 2.0"),
    ],
}, indent=2) + "\n")
print("manifest written:", manifest)
PY

echo
echo "packages in $OUT:"
ls -la "$OUT"
