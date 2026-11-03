"""Fake game objects for the item code, bank and inventory tests. Not collected by pytest (no test_ prefix).

The fakes mimic only what the offline code layer needs: serials are the real code bytes (so UniqueIds, checksums and
re-signing are real), and the "game" decodes a serial back to a definition by reading its header. Everything the game
would decide (names, parts, the bank's behaviour) is a simple stand-in and says nothing about the real game.

    install(monkeypatch) -> World   sets up a manager, a bank, the game-call stand-ins and a journal recorder.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from rtse import bank, codes, gear_inventory, item_serial, journal

# Real published codes (see test_item_serial.REAL): an encrypted pistol and an encrypted item (shield).
PISTOL_CODE = "BL2(h2/V3n+mGlXGeO8zxX8PVUyUhQuLayUPJYlikvHB65/Ell/prsQL)"  # Infinity, GT
SHIELD_CODE = "BL2(B5RV2vkR7ysJhQXfl8v1nRLlvtqvzJp2ZBtfyYW/kw/XD2axxG8=)"  # Fabled Tortoise, GT
PISTOL_BYTES = item_serial.code_to_bytes(PISTOL_CODE)
SHIELD_BYTES = item_serial.code_to_bytes(SHIELD_CODE)


def bad_checksum_code(code: str = SHIELD_CODE) -> str:
    """The same code with its last byte flipped: the header parses but the checksum fails."""
    data = bytearray(item_serial.code_to_bytes(code))
    data[-1] ^= 0x01
    return item_serial.bytes_to_code(bytes(data))


def serial_of(data: bytes, *, state: int = 2, counter: int = 7) -> SimpleNamespace:
    """A fake InventorySerialNumber holding `data` (buffer padded to 40 like the game's)."""
    return SimpleNamespace(
        EncryptedLength=len(data), RunningCounter=counter, State=state, Buffer=[*data, *([0] * (40 - len(data)))],
    )


class Defn:
    """A fake struct or object: a field nobody set reads as None (like an unset game field)."""

    def __init__(self, **fields: Any) -> None:
        self.__dict__.update(fields)

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)
        return None


class Obj(Defn):
    """A UObject stand-in with a path name."""

    def __init__(self, path: str, **fields: Any) -> None:
        super().__init__(**fields)
        self._path = path

    def _path_name(self) -> str:
        return self._path


class Item(Obj):
    """A live item: DefinitionData, a class, a name, and a serial (the code bytes) it exports as."""

    def __init__(self, definition: Defn, *, weapon: bool, name: str, data: bytes | None, mark: int = 1, level: int = 30):
        super().__init__(
            f"/Game/Items/{name.replace(' ', '_')}_{definition.UniqueId}",
            Class=SimpleNamespace(Name="WillowWeapon" if weapon else "WillowItem"),
            DefinitionData=definition, ExpLevel=level, Mark=mark, Inventory=None,
        )
        self._name = name
        self._data = data

    def GetHumanReadableName(self) -> str:
        return self._name

    def CreateSerialNumber(self) -> SimpleNamespace:
        if self._data is None:
            raise RuntimeError("no serial for this fake item")
        return serial_of(self._data)


class World:
    """Everything one test sees: the manager (backpack, bank), the bank, and the journal calls."""

    def __init__(self, manager: "Manager", bank_obj: "Bank | None", journal_calls: list) -> None:
        self.manager = manager
        self.bank = bank_obj
        self.journal = journal_calls
        self.game_decodes = True  # False: UnpackSerialNumber refuses, so the Create*FromSerialNumber fallback runs
        self.stage = 30


def make_definition(uid: int, *, weapon: bool, stage: int = 30, maker: str = "Hyperion") -> Defn:
    """A definition with the fields the card code reads. Only the UniqueId is ever compared in tests."""
    fields: dict[str, Any] = {
        "UniqueId": uid, "GameStage": stage, "ManufacturerGradeIndex": 1,
        "ManufacturerDefinition": Obj(f"/Game/Manufacturers/{maker}", Name=f"Manufacturer_{maker}"),
        "BalanceDefinition": Obj("/Game/Balance/Bal_Pistol_Hyperion", Name="Bal_Pistol_Hyperion"),
    }
    if weapon:
        fields["WeaponTypeDefinition"] = Obj("/Game/Weapons/WeaponType_Pistol", Name="WeaponType_Pistol")
    else:
        fields["ItemDefinition"] = Obj("/Game/Items/Shield", Name="Shield", Class=SimpleNamespace(Name="ShieldDefinition"))
    return Defn(**fields)


class Manager(Obj):
    """WillowInventoryManager stand-in: Backpack list, slot count, and the add/remove calls RTSE uses."""

    def __init__(self, world: World, *, slots: int = 12, bank: "Bank | None" = None) -> None:
        super().__init__(
            "/Game/PC.InvManager", Class=SimpleNamespace(Name="WillowInventoryManager"),
            Backpack=[], InventoryChain=None, ItemChain=None, TheBank=bank,
        )
        self.world = world
        self.slots = slots
        self.refuse_remove = False  # True: RemoveFromInventory does nothing, so the removal check fails
        self.calls: list[tuple] = []

    def GetEmptyBackpackSlots(self) -> int:
        return self.slots - sum(1 for item in self.Backpack if item is not None)

    def ClientAddWeaponToBackpack(self, definition: Defn, mark: int, ready: bool = False) -> None:
        self._add(definition, weapon=True, mark=mark)

    def ClientAddItemToBackpack(self, definition: Defn, quantity: int, mark: int, ready: bool = False) -> None:
        self._add(definition, weapon=False, mark=mark)

    def _add(self, definition: Defn, *, weapon: bool, mark: int) -> None:
        self.calls.append(("add", weapon, definition.UniqueId))
        data = item_serial.with_unique_id(PISTOL_BYTES if weapon else SHIELD_BYTES, definition.UniqueId)
        self.Backpack.append(Item(definition, weapon=weapon, name="Added item", data=data, mark=mark))

    def RemoveFromInventory(self, item: Item, can_drop: bool) -> None:
        self.calls.append(("remove", item._path_name()))
        if not self.refuse_remove:
            self.Backpack.remove(item)


class Chest(list):
    """TheChest stand-in: a list whose `del chest[i]` can be refused (refuse_delete), so RTSE falls back to ByDef."""

    refuse_delete = False

    def __delitem__(self, index: Any) -> None:
        if self.refuse_delete:
            raise RuntimeError("this array does not support deletes")
        super().__delitem__(index)


class Bank(Obj):
    """WillowInventoryStorage stand-in. Entries hold serials (Inventory None), like the chest data the game keeps.
    Remove*ByDef removes EVERY entry with the definition's UniqueId (the worst case RTSE must survive)."""

    def __init__(self, world: World, *, max_size: int = 24) -> None:
        super().__init__(
            "/Game/PC.Bank", Class=SimpleNamespace(Name="WillowInventoryStorage"), TheChest=Chest(), MaxSlots=max_size,
        )
        self.world = world
        self.max_size = max_size
        self.calls: list[tuple] = []

    def GetMaxSize(self) -> int:
        return self.max_size

    def used(self) -> int:
        return sum(1 for entry in self.TheChest if entry.Inventory is not None or entry.InventorySerialNumber.EncryptedLength)

    def AddWeaponFromDef(self, definition: Defn, ignore_size: bool, unique: bool) -> bool:
        return self._add(definition, weapon=True)

    def AddItemFromDef(self, definition: Defn, ignore_size: bool, unique: bool) -> bool:
        return self._add(definition, weapon=False)

    def _add(self, definition: Defn, *, weapon: bool) -> bool:
        self.calls.append(("add", weapon, definition.UniqueId))
        if self.used() >= self.max_size:
            return False
        data = item_serial.with_unique_id(PISTOL_BYTES if weapon else SHIELD_BYTES, definition.UniqueId)
        self.TheChest.append(chest_entry(data, weapon=weapon))
        return True

    def RemoveWeaponsByDef(self, definition: Defn) -> None:
        self._remove_by_uid(definition.UniqueId, weapon=True)

    def RemoveItemsByDef(self, definition: Defn) -> None:
        self._remove_by_uid(definition.UniqueId, weapon=False)

    def _remove_by_uid(self, unique_id: int, *, weapon: bool) -> None:
        self.calls.append(("remove_by_def", weapon, unique_id))
        self.TheChest[:] = [e for e in self.TheChest if bank.entry_unique_id(e) != unique_id]


def chest_entry(data: bytes, *, weapon: bool = True) -> SimpleNamespace:
    """A ChestData with a serial and no live Inventory."""
    return SimpleNamespace(
        Inventory=None, InventorySerialNumber=serial_of(data),
        InventoryClass=SimpleNamespace(Name="WillowWeapon" if weapon else "WillowItem"),
    )


class _Actor:
    def __init__(self, definition: Defn, destroyed: list) -> None:
        self.DefinitionData = definition
        self._destroyed = destroyed

    def Destroy(self) -> None:
        self._destroyed.append(True)


class _FakeCDO:
    """The class default object: UnpackSerialNumber and Create*FromSerialNumber, decoding the header only."""

    def __init__(self, world: World, class_name: str) -> None:
        self.world = world
        self.class_name = class_name

    def _decode(self, serial: Any) -> Defn | None:
        data = codes.serial_bytes(serial)
        header = item_serial.read_header(data)
        if header["is_weapon"] != (self.class_name == "WillowWeapon"):
            return None
        return make_definition(header["unique_id"], weapon=header["is_weapon"], stage=self.world.stage)

    def UnpackSerialNumber(self, serial: Any) -> tuple[bool, Defn | None]:
        if not self.world.game_decodes:
            return False, None
        definition = self._decode(serial)
        return (definition is not None), definition

    def CreateWeaponFromSerialNumber(self, serial: Any, source: Any) -> _Actor | None:
        return self._create(serial)

    def CreateItemFromSerialNumber(self, serial: Any, source: Any) -> _Actor | None:
        return self._create(serial)

    def _create(self, serial: Any) -> _Actor | None:
        definition = self._decode(serial)
        return None if definition is None else _Actor(definition, self.world.destroyed)


def install(monkeypatch, *, slots: int = 12, bank_max: int | None = 24, with_bank: bool = True) -> World:
    """Points the game-call hooks at the fakes. `bank_max=None` means a bank whose size is unknown (no GetMaxSize)."""
    journal_calls: list[tuple] = []
    world = World(manager=None, bank_obj=None, journal_calls=journal_calls)  # type: ignore[arg-type]
    world.destroyed = []
    bank_obj = Bank(world, max_size=bank_max or 24) if with_bank else None
    if bank_obj is not None and bank_max is None:
        bank_obj.GetMaxSize = None  # type: ignore[assignment]
        bank_obj.MaxSlots = None
    manager = Manager(world, slots=slots, bank=bank_obj)
    world.manager, world.bank = manager, bank_obj

    monkeypatch.setattr(gear_inventory, "_manager", lambda: manager)
    monkeypatch.setattr(codes, "_cdo", lambda name: _FakeCDO(world, name))
    monkeypatch.setattr(codes, "_make_struct", _make_struct)
    monkeypatch.setattr(
        journal, "record",
        lambda kind, target, before, after, undo=None: journal_calls.append((kind, target, before, after, undo)),
    )
    return world


class _StructOptions:
    drop_buffer = False  # make_struct loses the Buffer, so serial_struct must fall back to element-wise writes


STRUCT_OPTIONS = _StructOptions()


def _make_struct(name: str, **fields: Any) -> Any:
    if name == "InventorySerialNumber":
        buffer = [0] * 40 if STRUCT_OPTIONS.drop_buffer else list(fields.get("Buffer") or [0] * 40)
        return SimpleNamespace(
            EncryptedLength=fields.get("EncryptedLength", 0), RunningCounter=fields.get("RunningCounter", 0),
            State=fields.get("State", 0), Buffer=buffer,
        )
    return Defn(**fields)
