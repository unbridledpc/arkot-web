Arkenfall — game client 1.0
https://arkenfall.org

WINDOWS
  1. Unpack this folder anywhere you like (Desktop, Documents, a games folder).
     Do not run it from inside the .zip, and not in Program Files.
  2. Run Arkenfall.exe.
  3. Type the e-mail address and password you registered at
     https://arkenfall.org/register — the server is already filled in — and
     press Enter Game.
  4. The first time you do that the client offers to download the game
     graphics and sounds (about 230 MB, roughly 400 MB installed). Say yes
     and wait for the bar; it logs you in by itself afterwards, and later
     starts go straight in.

LINUX
  1. Unpack the archive:  tar xzf arkenfall-client-1.0-linux-x64.tar.gz
  2. cd Arkenfall && ./arkenfall
  3. Steps 3 and 4 above are the same.

  The binary needs a normal desktop with OpenGL 2.0 and the usual system
  libraries (libGL, libX11, libstdc++). No installation is required.

NOTHING TO CONFIGURE
  The client already knows where Arkenfall is. If you ever need it by hand:
  HTTP login, server http://bt.tibtool.com:7171/login, client version 15.25.

ACCOUNTS
  Create an account at https://arkenfall.org/register, then create a character
  at https://arkenfall.org/character/create. You log in with your e-mail
  address, not your account name.

WHERE YOUR SETTINGS LIVE
  Windows: %APPDATA%\arkenfall
  Linux:   ~/.local/share/arkenfall
  Deleting that folder resets the client to its defaults. Your own Lua goes in
  arkenfallrc.lua, which runs once every module has loaded.

NO BOT
  Arkenfall's rules ban bots, macros and automation, so this build ships
  without the bot module that the upstream client carries.

CREDITS AND LICENCE
  This client is a build of OTClient (opentibiabr/otclient, release 4.1),
  which is free software under the MIT licence — see LICENSE and AUTHORS in
  this folder. Arkenfall changes its branding, points it at the Arkenfall
  server and carries a handful of interface fixes. It ships no game graphics:
  those are fetched on first start.

  Arkenfall is a free, non-commercial fan project and is not affiliated with,
  endorsed by or connected to CipSoft GmbH.
