# RTSE

A save editor for Borderlands 2 that works while you're playing.

You don't quit the game, find your save file and edit it in some other tool. You hit **F8** in game, a page opens in your browser, and whatever you change shows up in the game right away. Change a gun's parts, bump your level, fill your ammo, unlock a fast travel station, whatever. Save in game like normal and it sticks.

It runs on the [bl-sdk](https://bl-sdk.github.io/willow2-mod-db/) mod loader, so it's just one more mod in your `sdk_mods` folder.

## What you can do with it

- **Gear:** open any weapon or item in your backpack or equipped, swap its parts, change its level, tweak its stats. There's a 3D view of the gun that updates as you go.
- **Character:** level, XP, skill points, cash, eridium, seraph crystals, torgue tokens.
- **Ammo:** see and set every ammo type.
- **Upgrades:** backpack, bank and ammo SDU levels, like the Gibbed editor has.
- **Skills:** your real skill tree for your class. Set any skill to any rank (even locked ones), change unspent points, or reset the whole tree.
- **World:** missions, challenges, fast travel stations and playthrough (Normal / TVHM / UVHM).

## Heads up before you start

- **Back up your saves first.** They're in `Documents\My Games\Borderlands 2\WillowGame\SaveData`. Copy that folder somewhere. It takes ten seconds and saves you if you break something.
- **Play solo.** Don't use this in public online games.
- **This is early.** Gear is the page that's had the most real use. Character, Ammo, Upgrades, Skills and World were built from the game's own data files, so the names they use are real, but nobody has confirmed every button does what it says in a live game yet. If something shows "unavailable" or does nothing, see [When something doesn't work](#when-something-doesnt-work).

## Easiest way to install: let Claude do it

Send this repo to Claude (Claude Code works great, since it can touch your files) and say something like:

> Install RTSE from https://github.com/zuhuHix/Bl2_RTSE into my Borderlands 2. Set up the bl-sdk mod loader first if I don't have it.

It'll find your game folder, set up the mod loader if needed, and put RTSE in the right place. Other coding assistants that can work on your computer will do the same job.

## Install it yourself

1. **Get the mod loader (bl-sdk) if you don't have it.**
   - Install the latest [Microsoft Visual C++ Redistributable](https://learn.microsoft.com/en-us/cpp/windows/latest-supported-vc-redist) if you haven't.
   - Download the latest release from [bl-sdk/willow2-mod-manager](https://github.com/bl-sdk/willow2-mod-manager/releases) (the release zip, not "Source code").
   - Unzip it straight into your Borderlands 2 folder, usually `C:\Program Files (x86)\Steam\steamapps\common\Borderlands 2`. Let it merge with what's there.
   - Start the game once. If you see a **MODS** option on the main menu, it worked.
   - On Linux / Steam Deck, add `WINEDLLOVERRIDES="ddraw=n,b" %command% -pf_tricks=vcrun2022` to the game's launch options.

2. **Put RTSE in the mod folder.**
   - Download this repo (green **Code** button, then **Download ZIP**) and unzip it.
   - Copy the `rtse` folder into the game's `sdk_mods` folder, so you end up with `Borderlands 2\sdk_mods\rtse\__init__.py`. Don't nest it an extra folder deep.

3. **Play.** Launch the game and load into a character. Press **F8**. Your browser opens with the editor.

If F8 does nothing, open the mod menu and check RTSE is enabled. You can also rebind the key there. The editor's address (it starts with `http://127.0.0.1`) is printed in the game's console and in `unrealsdk.log`, so you can paste it into a browser yourself.

## Using it

- Click a page on the left (Character, Ammo, Upgrades, Skills, World) or pick a gun or item from the list.
- Changes happen in your live game. **Save in game afterwards** (quit to the menu or use a save point) or they're gone when you leave.
- Each change tells you what the game reports back. If you ask for 500 and the game says 300, it'll tell you, since the game sometimes caps things.
- The editor only listens on your own computer, and the link has a random key in it. Nobody else on your network can use it.

## When something doesn't work

Every page has a **dev tools** dropdown at the bottom with a **Save ... dump** button. Click it while you're in game and RTSE writes a small file, `rtse/debug_<page>.json`, showing what the game actually has. If a page says "unavailable" or a button does nothing, open an [issue](https://github.com/zuhuHix/Bl2_RTSE/issues) and attach that file. That's how the guesses get fixed.

## For tinkerers

You can try the whole editor without the game, using fake data:

```
python dev/mock_server.py 8766
```

then open `http://127.0.0.1:8766/?t=mock`.

Other bits in `dev/`:

- `smoke_server.py` checks the web server's security basics.
- `audit_names.py` checks every game-side name RTSE uses against the game's real class definitions (it reads your installed game).
- `extract_skills.py` rebuilds `rtse/skilltrees.json` (the skill trees) from your installed game.

The mod itself is plain Python and vanilla JavaScript, no build step.

RTSE isn't affiliated with Gearbox or 2K. Skill names and descriptions in `rtse/skilltrees.json` come from the game's own files.
