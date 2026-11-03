"""Save-file checkpoints: copies of the character's save, kept in rtse/backups/<id>/, that can be restored from the editor.

    create(reason, note)            copy the character's .sav (and profile.bin) into a new checkpoint
    before_first_write(label)       journal begin hook: backs a VERIFIED save up before the session's first write to
                                    it; otherwise refuses the write with 423 until the user backs up or skips
    stage_restore(params)           stage a checkpoint for restore; nothing in the save folder changes yet
    pump()                          called every game tick; drives the staged restore (see "Staged restore" below)
    state() / debug_info()          the GET /api/checkpoints shape, and the raw values locate() read (for debugging)

Safety rules: the live save folder is only ever read, except by a restore the user staged and confirmed, and that is
written only at the main menu with no character loaded, after the files stopped changing, with a safety copy taken
first and rolled back from on any failure. Retention never deletes a pinned checkpoint (a staged restore's checkpoint
and safety copy, or one kept after a restore went wrong), and a restore whose safety copy must stay pinned can't be
cancelled until that pin is safely on disk.

Threads: everything here runs on the game thread, on purpose. A restore checks its gate (main menu, no character
loaded, files unchanged for STABLE_SECONDS) and then takes the safety copy, replaces the files, verifies them and saves
its state, all in ONE game-thread call: while that call runs the game can't load a character or save, so nothing can
change between the check and the write. Save files are small, so this briefly pauses the game at the main menu; the
pause is logged and shown in the restore result ("paused_ms"). The first-write backup runs inside the POST it guards,
on the game thread too. If the mod is disabled with a restore pending, its state stays on disk and the next enable
carries on from it (an interrupted "applying" is rolled back).

Where the save is comes from three places, in this order (see locate()): the game's own record of the last loaded
file, the game's save file id, and finally the newest Save*.sav in the default Documents folder. The game reads are
UNVERIFIED in a live game (LastLoadedFilePath, SaveGameFileId, the save manager lookup and the menu map name); each
read is guarded so a missing attribute becomes a note instead of an exception.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import mods_base
from mods_base import get_pc
from unrealsdk import find_all, logging

from . import journal, session_guard
from .routing import ApiError, Params, Route

BACKUPS_DIR = Path(__file__).with_name("backups")
GUESS_ROOT = Path.home() / "Documents" / "My Games" / "Borderlands 2" / "WillowGame" / "SaveData"
DEFAULT_RETENTION = 20
RETENTION_RANGE = (1, 200)
PUMP_EVERY = 60  # game ticks between checks for a staged restore (about once a second at 60 fps)
NOTE_MAX = 500
PROFILE_NAME = "profile.bin"
MENU_MAP = "menumap"  # UNVERIFIED: the main menu's map name as GetMapName reports it; check it in game
STABLE_SECONDS = 5.0  # a restore waits until the files it replaces haven't changed for this long (the game may be saving)
VERIFY_SECONDS = 5.0  # ...and checks them again this long after writing, in case the game saved over them
ORPHAN_GRACE_SECONDS = 3600.0  # a checkpoint folder without manifest.json (an interrupted copy) is pruned after this
IGNORED_LABEL_PREFIXES = ("/api/checkpoints",)  # backup actions; an undo (/api/history/undo) is a game write, so not here
IGNORED_LABELS = ("/api/parts/rescan",)
_ID_RE = re.compile(r"\d{8}-\d{6}(?:-\d+)?")



def _fresh_auto() -> dict[str, Any]:
    return {"done": False, "error": None, "checkpoint": None, "skipped": False}


# Module state for this session (reset by _reset_for_tests). The restore's own state is on disk (pending_restore.json).
_auto: dict[str, Any] = _fresh_auto()
_backed_up: set[str] = set()  # character file paths backed up this session
_stable: dict[str, tuple[Any, float]] = {}  # restore target -> (size/mtime signature, when it was first seen like that)
_pump_calls = 0
_remove_begin_hook: Callable[[], None] | None = None
_clock = time.monotonic  # for "unchanged for N seconds"; tests replace it
_write_started: float | None = None  # perf_counter() when the restore's write began (for "paused_ms")


def _settings_path() -> Path:
    return BACKUPS_DIR / "settings.json"


def _pending_path() -> Path:
    return BACKUPS_DIR / "pending_restore.json"


def _now() -> float:
    return time.time()


# --- Small file helpers -------------------------------------------------------------------------------------------


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_json(path: Path, data: Any) -> None:
    """Writes JSON durably: a temp file, flushed and fsynced, then replaced over the old one in one step."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        fh.write(json.dumps(data, indent=2))
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _discard(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def _num(value: Any) -> float:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0.0


# --- Locating the save --------------------------------------------------------------------------------------------


def _field(obj: Any, name: str) -> Any:
    """getattr that also survives a property that raises. UNVERIFIED: whether the game exposes these fields."""
    try:
        return getattr(obj, name, None)
    except Exception:  # noqa: BLE001
        return None


def _path_name(obj: Any) -> str:
    try:
        fn = getattr(obj, "_path_name", None)
        return str(fn()) if callable(fn) else ""
    except Exception:  # noqa: BLE001
        return ""


def _text(value: Any) -> str | None:
    return None if value is None else str(value)


def _map_name() -> str | None:
    """The current map's name, or None. UNVERIFIED in a live game: ENGINE is a mods_base global that bl-sdk sets, and
    GetCurrentWorldInfo().GetMapName(False) is the call this reads. Kept as one helper so tests can replace it."""
    try:
        engine = getattr(mods_base, "ENGINE", None)
        if engine is None:
            return None
        name = engine.GetCurrentWorldInfo().GetMapName(False)
        return None if name is None else str(name)
    except Exception:  # noqa: BLE001
        return None


def _save_manager(pc: Any, notes: list[str]) -> Any:
    """The game's WillowSaveGameManager: from the player controller first, else a search of the loaded objects."""
    if pc is not None:
        try:
            manager = pc.GetWillowGlobals().GetWillowSaveGameManager()
            if manager is not None:
                return manager
            notes.append("The player controller has no save game manager yet.")
        except Exception as ex:  # noqa: BLE001
            notes.append(f"Couldn't read the save game manager from the player controller ({ex!r}).")
    try:
        for obj in find_all("WillowSaveGameManager"):
            if "Default__" not in _path_name(obj):
                return obj
        notes.append("Searching the game found no WillowSaveGameManager object (other than the class default).")
    except Exception as ex:  # noqa: BLE001
        notes.append(f"Searching the game for the save game manager failed ({ex!r}).")
    return None


def _probe(notes: list[str]) -> dict[str, Any]:
    """Reads the raw game values locate() uses (and debug_info() dumps). Never raises; failures become notes."""
    try:
        pc = get_pc(possibly_loading=True)
    except Exception as ex:  # noqa: BLE001
        notes.append(f"Couldn't ask the game for the player controller ({ex!r}).")
        pc = None
    manager = _save_manager(pc, notes)
    fid = _field(pc, "SaveGameFileId")
    return {
        "player_controller": pc is not None,
        "save_manager": manager is not None,
        "LastLoadedFilePath": _text(_field(manager, "LastLoadedFilePath")),
        "SaveGameName": _text(_field(manager, "SaveGameName")),
        "SaveGameFileId": fid if isinstance(fid, int) and not isinstance(fid, bool) else None,
        "map": _map_name(),
    }


def _guessed_dirs() -> list[Path]:
    """The SaveData subfolders (one per SteamID64, usually one). Empty when the default folder doesn't exist."""
    try:
        if not GUESS_ROOT.is_dir():
            return []
        return sorted(p for p in GUESS_ROOT.iterdir() if p.is_dir())
    except OSError:
        return []


def _save_files(folder: Path) -> list[Path]:
    """Save*.sav files in a folder. The glob requires the name to end in .sav, so *.sav.bak is never listed."""
    try:
        return [p for p in folder.glob("Save*.sav") if p.is_file()]
    except OSError:
        return []


def _find_character(notes: list[str]) -> tuple[Path | None, str | None, bool]:
    """(path, source, verified). Verified: the game named the file (its full path, or a bare name found in exactly one
    SaveData folder), or its save file id matched a file in exactly one folder. A guess is never verified."""
    probe = _probe(notes)
    folders = _guessed_dirs()

    # a. The game's own record of the last loaded file (UNVERIFIED: whether it is absolute or a bare name).
    last = probe["LastLoadedFilePath"]
    if last:
        path = Path(last)
        if path.is_absolute():
            if path.is_file():
                notes.append(f"The game's last loaded save is {last}.")
                return path, "game", True
            notes.append(f"The game named {last} as its last loaded save, but that file isn't on disk.")
        else:
            hits = [folder / path for folder in folders if (folder / path).is_file()]
            if hits:
                unique = len(hits) == 1
                notes.append(f"The game named {last} as its last loaded save; found it in {hits[0].parent}"
                             + ("." if unique else f" (and {len(hits) - 1} other folder(s), so it isn't certain)."))
                return hits[0], "game", unique
            notes.append(f"The game named {last} (a bare name) as its last loaded save; it isn't in the default folder.")
    elif probe["save_manager"]:
        notes.append("The game didn't report a last loaded save file.")
    else:
        notes.append("The game's save manager isn't available, so its last loaded save file is unknown.")

    # b. The game's save file id, as the file name the game would use (hex is uppercase, e.g. Save000A.sav).
    fid = probe["SaveGameFileId"]
    if fid is not None and fid >= 0:
        names = dict.fromkeys((f"Save{fid:04X}.sav", f"Save{fid:04d}.sav"))
        hits = [folder / name for folder in folders for name in names if (folder / name).is_file()]
        if hits:
            unique = len(hits) == 1
            notes.append(f"The game's save file id is {fid}; found {hits[0].name} in {hits[0].parent}"
                         + ("." if unique else f" (and {len(hits) - 1} other match(es), so it isn't certain)."))
            return hits[0], "game_id", unique
        notes.append(f"The game's save file id is {fid}, but no Save{fid:04X}.sav is in the default folder.")
    else:
        notes.append("The game didn't report a save file id.")

    # c. Guessed: the newest Save*.sav in the default Documents folder.
    if not folders:
        notes.append(f"The default save folder {GUESS_ROOT} doesn't exist.")
        return None, None, False
    newest: tuple[float, Path] | None = None
    for folder in folders:
        for candidate in _save_files(folder):
            try:
                modified = candidate.stat().st_mtime
            except OSError:
                continue
            if newest is None or modified > newest[0]:
                newest = (modified, candidate)
    if newest is not None:
        notes.append(f"Guessed the newest save in the default folder: {newest[1].name}. It may not be your character.")
        return newest[1], "guessed", False
    notes.append(f"No Save*.sav file is in the default save folder {GUESS_ROOT}.")
    return None, None, False


def locate() -> dict[str, Any]:
    """The contract's `save` object: where the character's save is and how it was found. Never raises."""
    notes: list[str] = []
    save: dict[str, Any] = {"dir": None, "character_file": None, "profile_file": None, "source": None,
                            "verified": False, "notes": notes}
    try:
        character, source, verified = _find_character(notes)
    except Exception as ex:  # noqa: BLE001
        notes.append(f"Looking for the save file failed ({ex!r}).")
        character, source, verified = None, None, False
    if character is None:
        notes.append("No character save file was found.")
        return save
    save["dir"] = str(character.parent)
    save["character_file"] = str(character)
    save["source"] = source
    save["verified"] = verified
    profile = character.parent / PROFILE_NAME
    if profile.is_file():
        save["profile_file"] = str(profile)
    else:
        notes.append(f"There is no {PROFILE_NAME} next to the save, so the profile isn't backed up.")
    return save


# --- Checkpoints --------------------------------------------------------------------------------------------------


def _checkpoint_folders() -> list[Path]:
    """Every folder under BACKUPS_DIR named like a checkpoint id, with or without a manifest."""
    try:
        if not BACKUPS_DIR.is_dir():
            return []
        return [p for p in BACKUPS_DIR.iterdir() if p.is_dir() and _ID_RE.fullmatch(p.name)]
    except OSError:
        return []


def _manifests() -> list[dict[str, Any]]:
    """Every readable checkpoint manifest, oldest first. The manifest's "id" is its folder name."""
    found: list[dict[str, Any]] = []
    for folder in _checkpoint_folders():
        data = _read_json(folder / "manifest.json")
        if data is None:
            continue
        data["id"] = folder.name
        found.append(data)
    found.sort(key=lambda m: (_num(m.get("time")), m["id"]))
    return found


def _load_manifest(checkpoint_id: Any) -> dict[str, Any] | None:
    if not isinstance(checkpoint_id, str) or not _ID_RE.fullmatch(checkpoint_id):
        return None
    data = _read_json(BACKUPS_DIR / checkpoint_id / "manifest.json")
    if data is None:
        return None
    data["id"] = checkpoint_id
    return data


def _norm(path: Any) -> str:
    return os.path.normcase(os.path.normpath(str(path)))


def _pins() -> dict[str, str]:
    """checkpoint id -> why retention must keep it. A staged (or failed) restore pins its checkpoint and its safety
    copy until it succeeds or is cancelled; a manifest's own "pinned" (set when a restore went wrong) pins it until
    the user unpins it."""
    pins: dict[str, str] = {}
    for manifest in _manifests():  # a manifest's own reason is the more specific one, so it wins
        if manifest.get("pinned"):
            pins[manifest["id"]] = str(manifest["pinned"])
    pending = _read_pending()
    if pending:
        pins.setdefault(pending["id"], "A restore of this backup is staged.")
        if pending.get("safety_checkpoint"):
            pins.setdefault(str(pending["safety_checkpoint"]), "Safety copy of your save from just before the staged restore.")
    return pins


def _view(manifest: dict[str, Any], pins: dict[str, str], current_file: str | None) -> dict[str, Any]:
    """A manifest as the contract's Checkpoint shape. character_file is also where a restore writes the .sav."""
    files = [
        {"name": f.get("name"), "size": f.get("size"), "sha256": f.get("sha256"), "kind": f.get("kind")}
        for f in manifest.get("files", [])
        if isinstance(f, dict)
    ]
    character = manifest.get("character_file")
    matches = None if current_file is None or not character else _norm(character) == _norm(current_file)
    return {
        "id": manifest["id"],
        "time": manifest.get("time"),
        "reason": manifest.get("reason"),
        "note": manifest.get("note") or "",
        "character_file": character,
        "files": files,
        "pending": manifest["id"] == _pending_id(),
        "pinned": pins.get(manifest["id"]),
        "matches_current": matches,
    }


def _new_checkpoint_folder() -> tuple[str, Path]:
    BACKUPS_DIR.mkdir(parents=True, exist_ok=True)
    base = time.strftime("%Y%m%d-%H%M%S")
    checkpoint_id, n = base, 2
    while (BACKUPS_DIR / checkpoint_id).exists():
        checkpoint_id = f"{base}-{n}"
        n += 1
    folder = BACKUPS_DIR / checkpoint_id
    folder.mkdir()
    return checkpoint_id, folder


def create(reason: str, note: str = "") -> dict[str, Any]:
    """Copies the located character save (and profile.bin) into a new checkpoint and prunes by retention.

    The source files are only read. Raises CheckpointError: 404 when no character save was found, 409 when the copy
    fails (no partial checkpoint is left behind).
    """
    save = locate()
    if save["character_file"] is None:
        raise CheckpointError(404, "No character save file was found, so there is nothing to back up.")
    sources: list[tuple[str, Path]] = [("character", Path(save["character_file"]))]
    if save["profile_file"]:
        sources.append(("profile", Path(save["profile_file"])))
    return _snapshot(reason, note, sources, save["source"])


def _snapshot(reason: str, note: str, sources: list[tuple[str, Path]], found_by: str | None) -> dict[str, Any]:
    """Copies exactly these files (kind, path) into a new checkpoint. The first one is the character save. The manifest
    is written last, so a folder without one is an interrupted copy (pruned after a grace period)."""
    character = sources[0][1]
    try:
        checkpoint_id, folder = _new_checkpoint_folder()
    except OSError as ex:
        raise CheckpointError(409, f"Couldn't create the backups folder ({ex.strerror or ex}).") from None
    try:
        files: list[dict[str, Any]] = []
        for kind, source in sources:
            target = folder / source.name
            shutil.copy2(source, target)
            files.append({
                "name": source.name,
                "size": target.stat().st_size,
                "sha256": _sha256(target),
                "kind": kind,
                "original": str(source),
            })  # fmt: skip
        manifest: dict[str, Any] = {
            "id": checkpoint_id,
            "time": _now(),
            "reason": reason,
            "note": note,
            "source": found_by,
            "character_file": str(character),
            "profile_file": next((str(path) for kind, path in sources if kind == "profile"), None),
            "files": files,
        }
        _write_json(folder / "manifest.json", manifest)
    except OSError as ex:
        shutil.rmtree(folder, ignore_errors=True)
        raise CheckpointError(409, f"Couldn't copy the save files ({ex.strerror or ex}); no checkpoint was made.") from None

    try:
        _prune()
    except Exception as ex:  # noqa: BLE001 - a failed prune must not fail the checkpoint that was just made
        logging.error(f"RTSE: pruning checkpoints failed: {ex!r}")
    return _view(manifest, _pins(), str(character))


def _set_pin(checkpoint_id: str | None, reason: str | None) -> None:
    """Pins (or with None, unpins) a checkpoint in its own manifest, then reads it back. Raises CheckpointError when the
    manifest can't be read, written or doesn't read back as asked."""
    path = BACKUPS_DIR / str(checkpoint_id) / "manifest.json"
    manifest = _load_manifest(checkpoint_id)
    if manifest is None:
        raise CheckpointError(409, f"the manifest of backup {checkpoint_id} can't be read")
    if reason:
        manifest["pinned"] = reason
    else:
        manifest.pop("pinned", None)
    try:
        _write_json(path, manifest)
    except OSError as ex:
        raise CheckpointError(409, f"writing {path} failed ({ex.strerror or ex})") from None
    if (_load_manifest(checkpoint_id) or {}).get("pinned") != (reason or None):
        raise CheckpointError(409, f"{path} didn't read back as written")


def _keep_safety(pending: dict[str, Any], reason: str) -> str:
    """Pins the pending restore's safety copy for good (a restore went wrong). Returns "" when the pin is on disk, else
    a sentence for the error: the restore then can't be cancelled until the pin is written (see _release)."""
    safety_id = pending.get("safety_checkpoint")
    if not safety_id:
        return ""
    pending.update(pin_required=safety_id, pin_reason=reason)
    try:
        _set_pin(safety_id, reason)
    except CheckpointError as ex:
        logging.error(f"RTSE: couldn't pin the safety backup {safety_id}: {ex}")
        return (f" RTSE also couldn't mark the safety backup {safety_id} as kept ({ex}), so this restore stays listed "
                "(which keeps it) until that works.")
    return ""


def _release(pending: dict[str, Any] | None) -> None:
    """Before a pending restore is cancelled or replaced: if its safety copy must stay pinned, make sure the pin is on
    disk (retrying the write), so dropping the pending restore can't leave the safety copy unprotected."""
    if not pending or not pending.get("pin_required"):
        return
    safety_id = str(pending["pin_required"])
    if (_load_manifest(safety_id) or {}).get("pinned"):
        return
    try:
        _set_pin(safety_id, str(pending.get("pin_reason") or "Holds your save from just before a restore that went wrong."))
    except CheckpointError as ex:
        raise CheckpointError(
            409,
            f"Couldn't mark the safety backup {safety_id} as kept ({ex}), so the restore stays: dropping it would let "
            f"the backup be pruned. Copy {BACKUPS_DIR / safety_id} somewhere safe, or fix the problem and try again.",
        ) from None


def _prune() -> list[str]:
    """Deletes the oldest unpinned checkpoints beyond the retention limit, and interrupted copies (folders without a
    manifest) older than ORPHAN_GRACE_SECONDS, never the newest folder. Pinned checkpoints are never deleted and don't
    count toward the limit."""
    keep = _retention()
    pins = _pins()
    unpinned = [m for m in _manifests() if m["id"] not in pins]  # oldest first
    doomed = [m["id"] for m in unpinned[: max(0, len(unpinned) - keep)]]

    folders = _checkpoint_folders()
    try:
        newest = max(folders, key=lambda p: (p.stat().st_mtime, p.name)) if folders else None
    except OSError:
        newest = None
    for folder in folders:
        if folder == newest or folder.name in pins or (folder / "manifest.json").exists():
            continue
        try:
            age = time.time() - folder.stat().st_mtime
        except OSError:
            continue
        if age >= ORPHAN_GRACE_SECONDS:
            doomed.append(folder.name)

    pruned: list[str] = []
    for checkpoint_id in doomed:
        try:
            shutil.rmtree(BACKUPS_DIR / checkpoint_id)
        except OSError as ex:
            logging.error(f"RTSE: couldn't prune checkpoint {checkpoint_id}: {ex!r}")
            continue
        pruned.append(checkpoint_id)
    return pruned


def _retention() -> int:
    value = (_read_json(_settings_path()) or {}).get("retention", DEFAULT_RETENTION)
    if isinstance(value, bool) or not isinstance(value, int) or not RETENTION_RANGE[0] <= value <= RETENTION_RANGE[1]:
        return DEFAULT_RETENTION
    return value


def set_retention(params: Params) -> list[str]:
    """Saves the retention limit (whole number 1 to 200) and prunes to it. Returns the ids that were pruned."""
    value = params.get("retention")
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    if isinstance(value, bool) or not isinstance(value, int) or not RETENTION_RANGE[0] <= value <= RETENTION_RANGE[1]:
        raise CheckpointError(400, "retention must be a whole number from 1 to 200.")
    return _save_retention(value)


def _save_retention(value: int) -> list[str]:
    settings = _read_json(_settings_path()) or {}
    settings["retention"] = value
    try:
        _write_json(_settings_path(), settings)
    except OSError as ex:
        raise CheckpointError(409, f"Couldn't save the retention setting ({ex.strerror or ex}).") from None
    return _prune()


# --- The first-write backup (journal begin hook) ------------------------------------------------------------------

BLOCKED_HINT = "Open History > Save backups to back up now, or choose to continue without a backup."


def _blocking(save: dict[str, Any]) -> bool:
    """True when the next game write would be refused by before_first_write."""
    if _auto["skipped"] or (save["character_file"] is not None and save["character_file"] in _backed_up):
        return False
    return save["character_file"] is None or not save.get("verified") or _auto["error"] is not None


def before_first_write(label: str) -> dict[str, Any] | None:
    """Journal begin hook: before the session's first write to a character, backs its save up.

    Only a VERIFIED save is backed up automatically (its path came from the game; see locate()). If the save is only a
    guess, isn't found, or the copy fails, the write is refused with 423 (journal.WriteBlocked) and the error says how
    to go on: "Back up now" (the user checks the path on the page) or "continue without a backup" for this session.
    Returns {"backup": {...}} when it made a backup, which api.py adds to that write's response.
    """
    name = str(label)
    if name.startswith(IGNORED_LABEL_PREFIXES) or name in IGNORED_LABELS or _auto["skipped"]:
        return None
    try:
        save = locate()
        character = save["character_file"]
        if character is not None and character in _backed_up:
            return None
        if character is None:
            reason = "no save file was found"
        elif not save.get("verified"):
            reason = (f"RTSE couldn't confirm which save is yours (it only guessed {Path(character).name}, the newest "
                      "save it could find), so it didn't back it up automatically")
        else:
            try:
                checkpoint = create("first write")
            except CheckpointError as ex:
                reason = f"the copy failed ({ex})"
            else:
                _backed_up.add(character)
                _auto.update(done=True, error=None, checkpoint=checkpoint["id"])
                return {"backup": {"checkpoint": checkpoint["id"], "file": character}}
    except Exception as ex:  # noqa: BLE001 - an unexpected failure blocks too: a write without a backup needs a yes
        reason = f"the backup failed unexpectedly ({ex!r})"
    _auto["error"] = reason[0].upper() + reason[1:] + "."
    logging.warning(f"RTSE: write {name} refused: no backup ({reason})")
    raise journal.WriteBlocked(session_guard.BLOCKED_STATUS,
                               f"Couldn't back up your save before the first change: {reason}. {BLOCKED_HINT}")


def skip_backup(params: Params) -> None:
    """The user's explicit "continue without a backup" for this session."""
    if params.get("confirm") is not True:
        raise CheckpointError(400, 'Continuing without a backup needs {"confirm": true}.')
    _auto.update(skipped=True, error=None)
    logging.warning("RTSE: the user chose to continue without a save backup this session")


def enable() -> None:
    """Registers before_first_write with the journal. Called by the mod's enable, never at import time."""
    global _remove_begin_hook  # noqa: PLW0603
    if _remove_begin_hook is None:
        _remove_begin_hook = journal.add_begin_hook(before_first_write)


def disable() -> None:
    """Removes the journal hook. A pending restore's state stays on disk for the next enable to carry on from."""
    global _remove_begin_hook  # noqa: PLW0603
    if _remove_begin_hook is not None:
        _remove_begin_hook()
        _remove_begin_hook = None


# --- Staged restore -----------------------------------------------------------------------------------------------
#
# The restore's whole state lives in pending_restore.json, so a restart can't lose it:
#   staged     -> not at the main menu (or a character is loaded)
#   waiting    -> at the menu, waiting for the save files to stop changing for STABLE_SECONDS
#   applying   -> safety copy taken, files being replaced (only seen on disk if RTSE stopped mid-copy: rolled back)
#   verifying  -> written and read back; re-checked VERIFY_SECONDS later in case the game saved over them
#   failed     -> didn't apply (see "error"); stays until staged again or cancelled
# The outcome of the last attempt is in last_restore.json.

RESTORE_RULE = (
    f"A staged restore is written only when all of these are true: the game is on the main menu map ({MENU_MAP}), no "
    f"character is loaded (no player pawn), and each save file it replaces hasn't changed size or time for "
    f"{STABLE_SECONDS:g} seconds. RTSE first copies your current files to a safety backup, replaces each file in one "
    f"step, checks it reads back as the backup, and checks it again {VERIFY_SECONDS:g} seconds later. If anything "
    "fails, the original files are put back from the safety backup. If the game wrote over the restored file, the "
    "restore is reported as failed and the safety backup is kept. These game checks are unverified in a live game."
)
BUSY = ("applying", "verifying")


def _last_path() -> Path:
    return BACKUPS_DIR / "last_restore.json"


def _read_pending() -> dict[str, Any] | None:
    data = _read_json(_pending_path())
    if data is None or not _ID_RE.fullmatch(str(data.get("id"))) or not isinstance(data.get("files"), list):
        return None
    return data


def _pending_id() -> str | None:
    pending = _read_pending()
    return pending["id"] if pending else None


def _write_pending(pending: dict[str, Any]) -> None:
    _write_json(_pending_path(), pending)


def _restore_files(checkpoint_id: str, manifest: dict[str, Any], include_profile: bool) -> list[dict[str, Any]]:
    """The manifest files a restore will put back, after checking each backup exists and matches its checksum, and
    that it only goes back where it came from."""
    folder = BACKUPS_DIR / checkpoint_id
    wanted = [
        f
        for f in manifest.get("files", [])
        if isinstance(f, dict) and (f.get("kind") == "character" or (include_profile and f.get("kind") == "profile"))
    ]
    if not any(f.get("kind") == "character" for f in wanted):
        raise CheckpointError(409, f"Checkpoint {checkpoint_id} has no character save to restore.")
    for f in wanted:
        name, original, digest = f.get("name"), f.get("original"), f.get("sha256")
        if not isinstance(name, str) or Path(name).name != name or not isinstance(original, str) or not original:
            raise CheckpointError(409, f"Checkpoint {checkpoint_id} lists a damaged file, so it can't be restored.")
        # A restore only ever puts a save file back where it was copied from: same name, a .sav or profile.bin
        target = Path(original)
        if target.name != name or not (name.lower().endswith(".sav") or name == PROFILE_NAME) or not target.is_absolute():
            raise CheckpointError(409, f"Checkpoint {checkpoint_id} names an unexpected place to restore {name} to.")
        if not target.parent.is_dir():
            raise CheckpointError(409, f"The save folder {target.parent} isn't there any more, so {name} can't be restored.")
        source = folder / name
        if not source.is_file():
            raise CheckpointError(409, f"The backup of {name} is missing from checkpoint {checkpoint_id}.")
        try:
            matches = _sha256(source) == digest
        except OSError as ex:
            raise CheckpointError(409, f"Couldn't read the backup of {name} ({ex.strerror or ex}).") from None
        if not matches:
            raise CheckpointError(409, f"The backup of {name} doesn't match its checksum; it was not restored.")
    return wanted


def stage_restore(params: Params) -> dict[str, Any]:
    """Stages a checkpoint for restore. Nothing in the save folder changes here; pump() applies it at the main menu."""
    return _stage(dict(params))


def _stage(params: Params) -> dict[str, Any]:
    if params.get("confirm") is not True:
        raise CheckpointError(400, "Restoring replaces your save file. Send confirm: true to stage the restore.")
    checkpoint_id = params.get("id")
    if not isinstance(checkpoint_id, str) or not checkpoint_id:
        raise CheckpointError(400, "Say which checkpoint to restore (id).")
    include_profile = params.get("include_profile", False)
    if not isinstance(include_profile, bool):
        raise CheckpointError(400, "include_profile must be true or false.")
    current = _read_pending()
    if current and current.get("status") in BUSY:
        raise CheckpointError(409, "A restore is being written right now; wait a few seconds and look again.")
    manifest = _load_manifest(checkpoint_id)
    if manifest is not None:
        _release(current)  # replacing the old pending restore must not unprotect its safety copy
    if manifest is None:
        raise CheckpointError(404, f"There is no checkpoint {checkpoint_id}.")
    wanted = _restore_files(checkpoint_id, manifest, include_profile)
    pending = {
        "id": checkpoint_id,
        "include_profile": include_profile,
        "staged_at": _now(),
        "files": [{"name": f["name"], "to": f["original"], "sha256": f["sha256"], "kind": f.get("kind")} for f in wanted],
        "status": "staged",
        "detail": "Waiting for the main menu: quit to the main menu (the game saves on the way out).",
        "safety_checkpoint": None,
        "error": None,
    }
    try:
        _write_pending(pending)
    except OSError as ex:
        raise CheckpointError(409, f"Couldn't stage the restore ({ex.strerror or ex}).") from None
    _stable.clear()
    return pending


def cancel_restore() -> str | None:
    """Drops a staged (or failed) restore and its pins. Returns the cancelled checkpoint id, or None."""
    return _cancel()


def _cancel() -> str | None:
    pending = _read_pending()
    if pending and pending.get("status") in BUSY:
        raise CheckpointError(409, "The restore is being written right now; wait a few seconds and look again.")
    _release(pending)
    try:
        _pending_path().unlink(missing_ok=True)
    except OSError as ex:
        raise CheckpointError(409, f"Couldn't remove the staged restore ({ex.strerror or ex}).") from None
    _stable.clear()
    return pending["id"] if pending else None


def at_main_menu() -> bool | None:
    """True when the game is at the main menu, False when it is on another map, None when that can't be read.
    UNVERIFIED in a live game: the menu's map name (MENU_MAP) is the assumption here."""
    name = _map_name()
    if not name:
        return None
    return name.lower() == MENU_MAP


def character_loaded() -> bool | None:
    """True when a character is loaded (the player controller has a pawn), False when it has none, None when that
    can't be read. UNVERIFIED in a live game: that the main menu's controller has no pawn."""
    try:
        pc = get_pc(possibly_loading=True)
    except Exception:  # noqa: BLE001
        return None
    if pc is None:
        return None
    try:
        return pc.Pawn is not None
    except Exception:  # noqa: BLE001
        return None


def _stat(path: Path) -> os.stat_result:
    """path.stat(), as a seam for tests."""
    return path.stat()


def _signature(path: Path) -> tuple[tuple[Any, ...] | None, str | None]:
    """(signature, problem). A file that exists gives (size, mtime); a missing target gives ("missing",), which is
    stable (restoring a save that was deleted creates it); any other stat error gives (None, why): never ready."""
    try:
        st = _stat(path)
    except FileNotFoundError:
        return ("missing",), None
    except OSError as ex:
        return None, str(ex.strerror or ex)
    return (st.st_size, st.st_mtime_ns), None


def _gate(pending: dict[str, Any], menu: bool | None, loaded: bool | None) -> tuple[bool, str, str]:
    """(ready, status, detail): whether the restore may be written now, and what it is waiting for. `menu` and
    `loaded` are read from the game by the caller in the same call, right before a write."""
    if menu is not True:
        if menu is None:
            return False, "staged", "RTSE can't read the map name, so it can't tell whether you're at the main menu; the restore won't be written."
        return False, "staged", "Waiting for the main menu: quit to the main menu (the game saves on the way out)."
    if loaded is not False:
        if loaded is None:
            return False, "staged", "At the main menu, but RTSE can't tell whether a character is still loaded; the restore won't be written."
        return False, "staged", "At the main menu, but a character still seems to be loaded; waiting for it to unload."
    now = _clock()
    ages: list[float] = []
    missing: list[str] = []
    for f in pending["files"]:
        sig, problem = _signature(Path(f["to"]))
        if sig is None:
            _stable.pop(f["to"], None)
            return False, "waiting", (f"At the main menu, but RTSE can't read {Path(f['to']).name} ({problem}) to check "
                                      "that the game has stopped writing it; the restore won't be written until it can.")
        if sig == ("missing",):
            missing.append(Path(f["to"]).name)
        seen = _stable.get(f["to"])
        if seen is None or seen[0] != sig:
            _stable[f["to"]] = (sig, now)
            ages.append(0.0)
        else:
            ages.append(now - seen[1])
    least = min(ages) if ages else 0.0
    if least < STABLE_SECONDS:
        note = f" ({', '.join(missing)} isn't in the save folder; the restore will create it.)" if missing else ""
        return False, "waiting", (f"At the main menu; waiting until your save file hasn't changed for {STABLE_SECONDS:g} s "
                                  f"(the game may still be saving): {least:.0f} s so far.{note}")
    return True, "waiting", ""


def _replace(source: Path, dest: Path) -> None:
    """os.replace, as a seam for tests."""
    os.replace(source, dest)


def _atomic_copy(source: Path, dest: Path, digest: str) -> None:
    """Writes source over dest in one step (copy to a temp file in dest's folder, then replace) and checks the result
    reads back with `digest`. Raises OSError or CheckpointError."""
    tmp = dest.with_name(dest.name + ".rtse-tmp")
    try:
        shutil.copyfile(source, tmp)  # copyfile, not copy2: the restored save gets a fresh time, as if the game wrote it
        with tmp.open("rb+") as fh:
            os.fsync(fh.fileno())
        _replace(tmp, dest)
    finally:
        _discard(tmp)
    if _sha256(dest) != digest:
        raise CheckpointError(409, f"{dest.name} didn't read back as the backup after writing it")


def _paused_ms(pending: dict[str, Any]) -> int | None:
    """How long the restore's write paused the game: measured now while it runs, else as saved when it finished."""
    if _write_started is not None:
        return round((time.perf_counter() - _write_started) * 1000)
    return pending.get("paused_ms")


def _record_last(pending: dict[str, Any], status: str, error: str | None, files: list[str]) -> None:
    last = {"ok": status == "ok", "status": status, "id": pending["id"], "time": _now(), "files": files,
            "error": error, "safety_checkpoint": pending.get("safety_checkpoint"),
            "paused_ms": _paused_ms(pending)}  # fmt: skip
    try:
        _write_json(_last_path(), last)
    except OSError as ex:
        logging.error(f"RTSE: couldn't save the restore result: {ex!r}")
    (logging.info if status == "ok" else logging.error)(f"RTSE: restore of {pending['id']}: {status} {error or ''}")


def _fail(pending: dict[str, Any], status: str, error: str, files: list[str] | None = None) -> None:
    """Records a failed attempt; the pending restore stays, marked failed (so it isn't retried by itself)."""
    _record_last(pending, status, error, files or [])
    pending.update(status="failed", error=error, detail="It stays here until you stage it again or cancel it.")
    try:
        _write_pending(pending)
    except OSError as ex:
        logging.error(f"RTSE: couldn't save the failed restore state: {ex!r}")


def _rollback(pending: dict[str, Any], why: str) -> None:
    """Puts every target back from the safety checkpoint (and removes files that didn't exist before), then fails."""
    safety_id = pending.get("safety_checkpoint")
    safety = _load_manifest(safety_id) if safety_id else None
    kept = {_norm(f.get("original")): f for f in (safety or {}).get("files", []) if isinstance(f, dict)}
    created = {_norm(path) for path in pending.get("created", [])}
    problems: list[str] = []
    for f in pending["files"]:
        dest = Path(f["to"])
        original = kept.get(_norm(dest))
        try:
            if original is not None:
                _atomic_copy(BACKUPS_DIR / str(safety_id) / original["name"], dest, original["sha256"])
            elif _norm(dest) in created and dest.exists():
                dest.unlink()
            elif _norm(dest) not in created:
                raise CheckpointError(409, "the safety copy doesn't have it")
        except Exception as ex:  # noqa: BLE001
            problems.append(f"{dest.name}: {ex}")
    if problems:
        pin = _keep_safety(pending, "Holds your save from just before a restore that failed halfway. Keep it until "
                                    "you've checked your save.")
        _fail(pending, "failed",
              f"{why}, and putting your original files back failed too ({'; '.join(problems)}). Your save folder may "
              f"hold a mix of old and new files: restore backup {safety_id} (it has what was there before), or copy its "
              f"files from {BACKUPS_DIR / str(safety_id)} by hand.{pin}")
        return
    _fail(pending, "rolled_back",
          f"{why}. Your original files were put back" + (f" from the safety backup {safety_id}" if safety_id else "")
          + ", so your save is as it was before the restore.")


def _apply(pending: dict[str, Any]) -> None:
    """Writes a staged restore whose gate is open, timing how long it pauses the game."""
    global _write_started  # noqa: PLW0603
    _write_started = time.perf_counter()
    try:
        _write_restore(pending)
    finally:
        paused = _paused_ms(pending)
        _write_started = None
        logging.info(f"RTSE: writing the restore of {pending['id']} paused the game for {paused} ms")


def _write_restore(pending: dict[str, Any]) -> None:
    """Safety copy first, then each file in one step, then the state saved as "verifying"."""
    checkpoint_id = str(pending["id"])
    try:
        manifest = _load_manifest(checkpoint_id)
        if manifest is None:
            raise CheckpointError(409, f"Checkpoint {checkpoint_id} is no longer on disk, so the restore can't be applied.")
        wanted = _restore_files(checkpoint_id, manifest, bool(pending.get("include_profile")))
    except CheckpointError as ex:
        _fail(pending, "failed", f"{ex} Nothing was written.")
        return
    live = sorted(((f.get("kind") or "character", Path(f["original"])) for f in wanted if Path(f["original"]).is_file()),
                  key=lambda item: item[0] != "character")
    try:
        safety = (_snapshot("before restore", f"what was there before restoring {checkpoint_id}", live, "restore target")["id"]
                  if live else None)
    except Exception as ex:  # noqa: BLE001
        _fail(pending, "failed", f"Couldn't take a safety copy of your current save first ({ex}), so nothing was written.")
        return
    pending.update(status="applying", safety_checkpoint=safety, error=None,
                   created=[f["original"] for f in wanted if not Path(f["original"]).exists()],
                   detail="Writing the backup into your save folder.")
    try:
        _write_pending(pending)  # from here on, a restart finds "applying" and rolls back
    except OSError as ex:
        _record_last(pending, "failed", f"Couldn't save the restore's progress ({ex}), so nothing was written.", [])
        return
    written: list[str] = []
    for f in wanted:
        try:
            _atomic_copy(BACKUPS_DIR / checkpoint_id / f["name"], Path(f["original"]), f["sha256"])
        except Exception as ex:  # noqa: BLE001
            reason = ex.strerror if isinstance(ex, OSError) and ex.strerror else str(ex)
            _rollback(pending, f"Writing {f['name']} failed ({reason})")
            return
        written.append(f["name"])
    pending.update(status="verifying", verify_after=_now() + VERIFY_SECONDS, written=written, paused_ms=_paused_ms(pending),
                   detail=f"Written. Checking again in {VERIFY_SECONDS:g} s that the game didn't save over it.")
    try:
        _write_pending(pending)
    except OSError as ex:
        logging.error(f"RTSE: couldn't save the restore's progress: {ex!r}")


def _verify(pending: dict[str, Any]) -> None:
    """The second check, VERIFY_SECONDS after writing: did the game save over the restored files?"""
    if _now() < _num(pending.get("verify_after")):
        return
    changed = []
    for f in pending["files"]:
        dest = Path(f["to"])
        try:
            same = _sha256(dest) == f["sha256"]
        except OSError:
            same = False
        if not same:
            changed.append(dest.name)
    written = list(pending.get("written", []))
    safety_id = pending.get("safety_checkpoint")
    if changed:
        pin = _keep_safety(pending, "Holds your save from just before a restore that the game then wrote over.")
        _fail(pending, "overwritten",
              f"The game wrote over {', '.join(changed)} within {VERIFY_SECONDS:g} s of the restore, so it didn't stick: "
              f"your files now hold what the game saved. The safety backup {safety_id} (what was there before the "
              f"restore) is kept.{pin}", written)
        return
    _record_last(pending, "ok", None, written)
    _discard(_pending_path())
    _stable.clear()


def pump() -> None:
    """Called every game tick (on the game thread). Every PUMP_EVERY calls it takes one step of the staged restore, in
    this same call: the gate is read from the game and, if it is open, the restore is written before pump() returns, so
    the player can't load or save in between. Cheap when nothing is staged. Never raises."""
    global _pump_calls  # noqa: PLW0603
    _pump_calls += 1
    if _pump_calls % PUMP_EVERY:
        return
    _step()


def _step() -> None:
    """One look at the staged restore. The gate is read here, immediately before any write, in the same call."""
    try:
        pending = _read_pending()
        if pending is None:
            return
        status = pending.get("status", "staged")
        if status == "failed":
            return
        if status == "applying":
            _rollback(pending, "RTSE stopped in the middle of writing the restore (the game or the mod was closed)")
            return
        if status == "verifying":
            _verify(pending)
            return
        ready, new_status, detail = _gate(pending, at_main_menu(), character_loaded())
        if not ready:
            if (new_status, detail) != (status, pending.get("detail")):
                pending.update(status=new_status, detail=detail)
                _write_pending(pending)
            return
        _apply(pending)
    except Exception as ex:  # noqa: BLE001
        logging.error(f"RTSE: the staged restore's step failed: {ex!r}")


# --- State and debugging ------------------------------------------------------------------------------------------


def state(_params: Params | None = None) -> dict[str, Any]:
    """The GET /api/checkpoints response (see the HTTP contract's Checkpoints section)."""
    save = locate()
    pins = _pins()
    menu = at_main_menu()
    return {
        "save": save,
        "backups_dir": str(BACKUPS_DIR),
        "retention": _retention(),
        "retention_range": list(RETENTION_RANGE),
        "auto": {**_auto, "blocking": _blocking(save)},
        "checkpoints": [_view(m, pins, save["character_file"]) for m in reversed(_manifests())],
        "pending_restore": _read_pending(),
        "last_restore": _read_json(_last_path()),
        "at_main_menu": menu,
        "restore_gate": {
            "at_main_menu": menu,
            "character_loaded": character_loaded(),
            "stable_seconds": STABLE_SECONDS,
            "verify_seconds": VERIFY_SECONDS,
            "rule": RESTORE_RULE,
        },
    }


def debug_info() -> dict[str, Any]:
    """Everything locate() tried, with the raw game values, and the restore's state, for the debug dump. Never raises."""
    out: dict[str, Any] = {}
    try:
        notes: list[str] = []
        out["game"] = _probe(notes)
        out["game_notes"] = notes
        folders = _guessed_dirs()
        out["guessed_folders"] = [str(f) for f in folders]
        seen: list[dict[str, Any]] = []
        for folder in folders:
            for path in _save_files(folder):
                try:
                    stat = path.stat()
                except OSError:
                    continue
                seen.append({"path": str(path), "size": stat.st_size, "modified": stat.st_mtime})
        out["save_files_seen"] = seen[:100]
        out["locate"] = locate()
        out["backups_dir"] = str(BACKUPS_DIR)
        out["retention"] = _retention()
        out["auto"] = dict(_auto)
        out["pending_restore"] = _read_pending()
        out["last_restore"] = _read_json(_last_path())
        out["at_main_menu"] = at_main_menu()
        out["character_loaded"] = character_loaded()
    except Exception as ex:  # noqa: BLE001
        out["error"] = repr(ex)
    return out


def _reset_for_tests() -> None:
    """Forgets this module's session state and removes its journal hook (tests only)."""
    global _auto, _pump_calls, _write_started  # noqa: PLW0603
    disable()
    _write_started = None
    _auto = _fresh_auto()
    _backed_up.clear()
    _stable.clear()
    _pump_calls = 0


# --- Routes (paths are the API; api.py collects these) ---


class CheckpointError(ApiError):
    """A checkpoint problem with its HTTP status. Subclasses routing.ApiError so the registry passes it through."""


ROUTE_ERROR = CheckpointError


def _create_route(params: Params) -> dict[str, Any]:
    note = params.get("note", "")
    if note is None:
        note = ""
    if not isinstance(note, str):
        raise CheckpointError(400, "note must be text.")
    checkpoint = create("manual", note[:NOTE_MAX])
    # The user saw the path on the page and asked for this copy, so it counts as this save's first-write backup
    _backed_up.add(checkpoint["character_file"])
    _auto.update(done=True, error=None, checkpoint=checkpoint["id"])
    journal.record("checkpoint.create", target=checkpoint["id"], before=None, after=checkpoint["id"])
    return {"applied": {"checkpoint": checkpoint}, **state()}


def _restore_route(params: Params) -> dict[str, Any]:
    pending = stage_restore(params)
    journal.record(
        "checkpoint.restore",
        target=pending["id"],
        before=None,
        after={"staged": True, "include_profile": pending["include_profile"]},
    )
    return {"applied": {"staged": pending}, **state()}


def _cancel_route(_params: Params) -> dict[str, Any]:
    return {"applied": {"cancelled": cancel_restore()}, **state()}


def _settings_route(params: Params) -> dict[str, Any]:
    pruned = set_retention(params)
    return {"applied": {"retention": _retention(), "pruned": pruned}, **state()}


def _skip_route(params: Params) -> dict[str, Any]:
    skip_backup(params)
    return {"applied": {"skipped": True}, **state()}


def _unpin_route(params: Params) -> dict[str, Any]:
    checkpoint_id = params.get("id")
    _unpin(checkpoint_id)
    return {"applied": {"unpinned": checkpoint_id}, **state()}


def _unpin(checkpoint_id: Any) -> None:
    manifest = _load_manifest(checkpoint_id)
    if manifest is None:
        raise CheckpointError(404, f"There is no checkpoint {checkpoint_id}.")
    pending = _read_pending()
    if pending and checkpoint_id in (pending["id"], pending.get("safety_checkpoint")):
        raise CheckpointError(409, "The staged restore keeps this backup; cancel the restore first.")
    _set_pin(checkpoint_id, None)


GET_ROUTES: dict[str, Route] = {"/api/checkpoints": state}
POST_ROUTES: dict[str, Route] = {
    "/api/checkpoints/create": _create_route,
    "/api/checkpoints/restore": _restore_route,
    "/api/checkpoints/cancel_restore": _cancel_route,
    "/api/checkpoints/settings": _settings_route,
    "/api/checkpoints/skip_backup": _skip_route,
    "/api/checkpoints/unpin": _unpin_route,
}
