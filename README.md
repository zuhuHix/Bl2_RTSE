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
- **World:** missions (including how far through each objective you are), challenges, fast travel stations and playthrough (Normal / TVHM / UVHM).
- **Inspector:** browse the game's own live data and change plain numbers, switches and text in it, for the things that don't have a page of their own.
- **Weapon lab:** snapshot a gun, swap a part, and see every stat that changed side by side. Then record the damage you actually deal (hits, crits, DPS, per-pellet) and chart it.

## A tour of the editor

The screenshots below were taken with the demo data from `dev/mock_server.py` (see [For tinkerers](#for-tinkerers)), not from a live game, so the names and numbers are placeholders. In the real thing they come from your character.

The left side is always your **Loadout**: seven pages pinned at the top (Character, Ammo, Upgrades, Skills, World, Inspector, Weapon lab), then a search box and everything you have equipped or in your backpack.

![The editor with nothing selected](docs/screenshots/01-home.png)

### Gear

Click any weapon, shield, grenade mod, relic or class mod in the list.

![A pistol open in the gear page, with the 3D view and part slots](docs/screenshots/02-gear.png)

- **3D view.** Drag to orbit, or use **Side / Angle / Top / Blueprint**. Click a part on the model to swap it. Colours show which manufacturer made each part.
- **Level.** Change the item's level with the stepper and hit **Set**.
- **Part slots.** One tile for each slot (body, grip, barrel, sight, stock, element, accessories, material). Empty slots are dashed. Click a tile to swap that part.

![The part picker for a pistol barrel](docs/screenshots/03-part-picker.png)

- **Part picker.** **Legal** shows only parts this gun could roll in the game. **Any gun** shows parts from other guns of the same type. **Everything** shows every part in the game. You can search by name, maker or element, filter by manufacturer, and see a preview of the gun with the part on it. Double-click a part to bolt it on, or **Remove current part**.

![The stats bench with damage, ammo and accuracy numbers](docs/screenshots/04-stats-bench.png)

- **Stats bench.** The gun's real numbers (damage, magazine size, reload time, spread, range and so on) with the game's base value next to each one. Type a number and **Set**, or use **/2, x2, x10**. Your tuned values are saved in `rtse/overrides.json` and re-applied whenever the game rebuilds that weapon.
- **Item info & dev tools** (below the stats) has the item's id, class, level requirement and game stage, plus the **Save ... dump** buttons for bug reports.

### Character

![The character page](docs/screenshots/05-character.png)

Your level and XP, plus unspent **skill points**, **cash**, **eridium**, **seraph crystals** and **torgue tokens**. Each one has a box to type an exact value and **Set**, quick **+** buttons, and **Max** where it makes sense. The game caps cash and tokens at two billion.

### Ammo

![The ammo page](docs/screenshots/06-ammo.png)

One card per ammo type with how much you have, your capacity and how full that is. **Set** an exact amount, **Fill** to capacity, **Empty** it, change the capacity directly, or change the ammo SDU level. **Fill everything** tops up all types at once.

### Upgrades

![The upgrades page](docs/screenshots/07-upgrades.png)

Your SDU (Storage Deck Upgrade) levels, the same idea as the Gibbed editor. **Backpack** and **Bank** slots, and **Ammo capacity** for every ammo type. Step the level up or down, **Set** it, or **Max** it. **Max everything** does the lot. Each card says where the game's number came from.

### Skills

![The skill tree page, with Allow above max ticked and one skill set past its maximum](docs/screenshots/08-skills.png)

The real skill tree for your class (Zer0 in this picture), straight from the game's own data. Your action skill is at the top. Click any skill to read what it does and set it to an exact rank with the stepper, **Max** or **Zero**. This ignores tier locks, so you can put points into locked skills. It doesn't spend skill points. You can also set your **Unspent** points, or **Reset tree**.

Tick **Allow above max** to go past a skill's normal maximum rank (up to 255). The game never offers ranks like that, so it might cap the number, ignore it, or act strangely. Each change still reports what the game says afterwards, and this hasn't been tried in a live game yet, so back up your saves.

### World

The **World** page has four tabs.

![World page, missions tab](docs/screenshots/09-world-missions.png)

- **Missions.** Every mission, grouped by storyline, with its state. Pick the playthrough (PT1 Normal, PT2 TVHM, PT3 UVHM) and filter by Active, Ready, Not started, Complete or Failed. Search by name. **Reset** one mission, or **Complete all shown / Reset all shown** for whatever the filters currently show.

![World page, challenges tab](docs/screenshots/10-world-challenges.png)

- **Challenges.** Each challenge with a progress bar. **Complete** or **Reset** one at a time, or everything shown.

![World page, fast travel tab](docs/screenshots/11-world-fast-travel.png)

- **Fast travel.** Every fast travel station, locked or unlocked. **Unlock** or **Lock** one, or **Unlock all shown**.

![World page, playthrough tab](docs/screenshots/12-world-playthrough.png)

![A mission's objectives panel, with progress numbers per objective](docs/screenshots/13-world-objectives.png)

- **Objectives (on each mission).** Click **Objectives** on a mission to see its objectives and how far along each one is, like "2 / 4". Type a number and **Set**, or use **Max** and **Zero**. If a mission has several stages, the **Stage** buttons move it between them, and **Track on HUD** makes it your tracked mission. Objectives that aren't part of the current stage are dimmed.

- **Playthrough.** Shows which playthrough you're in and lets you switch between Normal, TVHM and UVHM. Read the note on that page first: save and reload afterwards, since switching mid-playthrough can leave your mission log out of step.

### Inspector

![The inspector showing one mission's raw fields](docs/screenshots/14-inspector.png)

The closest thing to the raw tab in the Gibbed editor, except it shows the live game instead of a save file. Start from your player controller, your character's body, the skill tree, the mission tracker and a few other objects, and click through into their fields, lists and sub-objects (the trail at the top shows where you are). Every field shows the game's real name, its type and its current value.

- You can change whole numbers, decimals, true/false, text and enum values (type the enum's number). References, structs and lists are read-only here, and nothing in the game is ever called.
- This skips every check, so a wrong value can confuse the game or crash it, and some fields will do nothing. Back up your saves first.
- It's also the quickest way to find out what the game really holds when one of the other pages says "unavailable".

### Weapon lab

![The weapon lab recording damage, with stat tiles and the hit chart](docs/screenshots/15-weapon-lab.png)

Two halves:

- **A/B compare.** Pick a weapon and press **Snapshot A**. Swap parts right there (the part pickers use the same edit as the Gear page, so the change is real), then press **Snapshot B**, or skip B to compare A with the gun as it is now. The table lists every stat with A, B, the change and the % change, and which parts differ. The two bottom rows, damage per shot and "card DPS", are worked out from the stats (damage x pellets / fire interval), so they're marked **estimated**.
- **Measured damage.** Press **Start**, shoot something, press **Stop**. While it records, the page shows hits, crits and crit rate, total damage, DPS, average and max hit, crit vs non-crit, shots, pellets per shot, per-pellet damage, hits per shot and kills, plus a chart of every hit (crits in red) and the damage in the last second. Pick a **Target** to see one enemy only: its DPS is timed over that enemy's hits, and the shot-based tiles (shots, pellets, per pellet, hits per shot) read "all targets only", since a shot can't be tied to an enemy. **Only this weapon** skips hits and shots from your other guns. Every hit is kept up to the **Cap** (default 2,000; the oldest drop off after that), and **Reset** clears them without stopping.

Where the numbers come from: each hit is the game's own damage summary for that hit, read when the game tells the player controller it dealt damage (`NotifyDamageDealt`). Shots are counted when your gun uses ammo (`ConsumeAmmo`). If the first never fires, RTSE falls back to the damage passed to each enemy's `TakeDamage`, which is before armour and resistances, and the page then says every damage number is an estimate. Each hit is recorded once. The fallback only records until `NotifyDamageDealt` has fired once since the mod was loaded, and then it stays off, so at most the first few hits of your very first recording may come from it (marked estimated).

Which gun a hit belongs to comes from the game's `DamageCauser` on `TakeDamage`: the gun itself for bullets, or the gun that owns the projectile for rockets and grenades from a launcher. When the game gives no causer, the hit is credited to the gun you're holding when it lands. That's approximate (a rocket or burn landing after you swap guns counts for the new gun), so the page says how many hits were credited that way, and **Only this weapon** keeps them rather than guess. Tiles that are arithmetic over the readings (crit multiplier, per-pellet, hits per shot, kills) are always marked **estimated**. The hooks only do any work while a recording is running.

**In-game test checklist** (nobody has run this in the game yet; please try it and send the lab dump):

1. Open **Weapon lab**, pick an equipped gun, **Snapshot A**. Change its barrel with the part picker. The table should show B (live) values that differ, and the barrel under Parts. Check in game that the gun really changed.
2. **Snapshot B**, then swap the barrel back. The table should still compare A with the saved B.
3. **Start**, then fire single shots at an enemy. **Hits** should go up about once per shot (a shotgun: up to once per pellet), and **Shots** once per trigger pull. If Shots stays at 0, `ConsumeAmmo` isn't firing.
4. Compare a few hits with the damage numbers floating in the game. Shoot a weak spot: those hits should be counted as crits.
5. Hit a shielded enemy: **Total damage** should mention "shield absorbed".
6. Kill something and check **Kills** goes up.
7. Tick **Only this weapon**, then shoot with another gun: Shots shouldn't move and no hits should be added. If hits still appear, check the "credited to the held weapon" note: those hits had no causer.
8. Fire a rocket or a slow projectile and swap guns before it lands: the hit should still count for the launcher. If the page says those hits were "credited to the held weapon", the causer isn't reaching RTSE. Note that down.
9. Watch the "Hooks" line: `NotifyDamageDealt` should be counting. If only `TakeDamage` counts, the page shows the "pre-mitigation estimate" banner. Note that down.
10. Press **Save lab dump** and send `rtse/debug_lab.json` (it holds the raw damage summary fields of the first 20 hits).
11. Press F8 twice and toggle the mod off and on in the mod menu, then record again. Hits must not be counted twice.

## Heads up before you start

- **Back up your saves first.** They're in `Documents\My Games\Borderlands 2\WillowGame\SaveData`. Copy that folder somewhere. It takes ten seconds and saves you if you break something.
- **Play solo.** Don't use this in public online games.
- **This is early.** Gear is the page that's had the most real use. Character, Ammo, Upgrades, Skills, World (including objectives) and Inspector were built from the game's own data files, so the names they use are real, but nobody has confirmed every button does what it says in a live game yet. If something shows "unavailable" or does nothing, see [When something doesn't work](#when-something-doesnt-work).

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

- Click a page on the left (Character, Ammo, Upgrades, Skills, World, Inspector) or pick a gun or item from the list.
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

### Running the tests

The game modules (`unrealsdk`, `mods_base`) are stubbed by `dev/unreal_stub.py`, so everything below runs without the game. Install pytest once (`python -m pip install pytest`).

Full suite, before opening a pull request:

```
python -I -m pytest dev/tests
python -I dev/smoke_server.py
python -I dev/mock_server.py 8766      # in another terminal, then:
node dev/shot.mjs dev/steps_pages.json # screenshots through headless Edge; any console error or HTTP 4xx/5xx is printed
```

While iterating, run only the tests for what you touched: `python -I -m pytest dev/tests -k <module>` (e.g. `-k stats`, `-k idempotency`).

### Adding a feature

Every feature is a backend module, a page and a mock. None of them needs edits to shared files beyond one line each:

1. **Backend:** write `rtse/<feature>.py`. At its bottom, declare `ROUTE_ERROR` (your exception class, with a `.status`), `GET_ROUTES` and `POST_ROUTES` (`{"/api/<feature>...": function(params: dict) -> dict}`). Add the module to `ROUTE_MODULES` in `rtse/api.py`. That one line is all `api.py` needs. Every POST you declare automatically goes through `session_guard.can_write()` and runs inside a `journal.transaction()`, and an `events.EDIT_APPLIED` event is published after it. Inside your write functions, call `journal.record(kind, target, before, after, undo)` with the values you read back from the game.
2. **Page:** write `rtse/web/page_<feature>.js` (exporting `load(ctx)` and `render(root, ctx)`, plus an optional `pin(ctx)`) and optionally `page_<feature>.css`. Then add one entry to `PAGES` in `rtse/web/pages.js`. The server already serves `page_*.js` and `page_*.css`, and `index.html` doesn't change.
3. **Mock:** write `dev/mock_<feature>.py` with `handle(path, params, shared) -> dict | None` (`None` means "not my route"; raise `LookupError` for 404, `ValueError` for 400). `dev/mock_server.py` picks it up automatically.
4. **Tests:** put them in `dev/tests/test_<feature>.py`. The game modules are already stubbed, and the `game` fixture lets you set `game.pc` and the objects `find_object` returns. Run them with `python -I -m pytest dev/tests -k <feature>`.

Shared building blocks: `rtse/events.py` (`subscribe` / `publish` for game.damage, game.pickup, game.mission and rtse.edit), `rtse/journal.py` (the edit history; a no-op for now), `rtse/session_guard.py` (`can_write(route) -> (bool, reason)`; always allows for now) and `rtse/limits.py` (level caps). Their docstrings are the contracts.

### What is not verified in a live game

Tests and `audit_names.py` prove that names exist and that the logic does what it says with fake game objects. They can't prove what the game does at runtime. These are still unconfirmed, and the UI marks them **unverified**:

- **Torgue Tokens** use currency slot 3. The game's `ECurrencyType` enum only names that slot `Reserved_A`.
- **Level caps** come from `GetMaximumPossiblePlayerLevelCap()` / `GetMaxOverpowerLevel()`. If those can't be read, RTSE falls back to level 80 and OP10, so gear tops out at level 90 (80 + OP10).
- **Per-gun stat overrides** are keyed by the weapon's `UniqueId`. That only works if the game keeps UniqueIds across a save and reload. Overrides saved by older RTSE versions couldn't tell identical guns apart. They're kept as *legacy* entries that apply to every identical copy until you edit one. The old `overrides.json` is backed up to `overrides.v1.bak.json` the first time it's upgraded.
- **SDU**: the number of backpack and bank upgrades (9 each), and which call reports the bank size.
- **Ammo pools** are matched to the game's exact `D_Resourcepools.AmmoPools.*` definitions. A pool matched only by its name is marked unverified.
- **Part effects**: attribute labels come from a hand-written table.
- **Weapon lab damage**: the hooks (`WillowPlayerController.NotifyDamageDealt`, `WillowPawn.TakeDamage` as a fallback, `WillowWeapon.ConsumeAmmo`) and the damage summary fields exist in the game's class data, but nobody has seen them fire yet. Also unconfirmed: whether `DamageDealtToShields` is the shield damage of that hit, and whether every pellet counts as a hit. DPS uses the PC's clock, not the game's, so pausing during a recording stretches it. Exact weapon attribution relies on `NotifyDamageDealt` being called inside `TakeDamage` and on projectiles being owned by their gun; neither is confirmed.

The debug dumps on each page (Character, Ammo, Upgrades, Weapon lab, gear "Dump") save what's needed to settle each of these. Send them along with a bug report.

RTSE isn't affiliated with Gearbox or 2K. Skill names and descriptions in `rtse/skilltrees.json` come from the game's own files.
