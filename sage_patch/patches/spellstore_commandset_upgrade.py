"""Add an alternate SpellStore command set gated by a PlayerTemplate upgrade.

Targets the ROTWK SAGE-engine `game.dat` build ``2.01.2614.37001``.

The stock SpellStore resolver already chooses between the normal and multiplayer
`PlayerTemplate` command-set fields. This patch adds two more INI fields, stored in a small
side table keyed by the template name, and swaps to the alternate field only when the owning
player has the requested upgrade.
"""

from __future__ import annotations

import argparse
import struct

from ..addresses import (
    PLAYER_PLAYER_TEMPLATE,
    PLAYER_TEMPLATE_BLOCK_KEY,
    PLAYER_TEMPLATE_BLOCK_KEY_BYTES,
    PLAYER_TEMPLATE_BLOCK_KEY_RESUME,
    PLAYER_TEMPLATE_FIELD_TABLE_REFS,
    PLAYER_TEMPLATE_FIELD_TABLE_REF_OPCODES,
    PLAYER_TEMPLATE_FIND_BY_KEY,
    PLAYER_TEMPLATE_NAME_KEY,
)
from ..asm import JE, JNE, JZ, Asm
from ..patcher import Patch
from ..utils import allocate_section, apply_byte_patch, find_section, va_to_offset
from .name_tables import read_cstring

__all__ = ["SpellStoreCommandSetUpgradePatch"]

SECTION_NAME = ".sscu"

# CNT_CODE | MEM_EXECUTE | MEM_READ | MEM_WRITE. The cave stores runtime rows and g_key.
_CHARACTERISTICS = 0x20 | 0x20000000 | 0x40000000 | 0x80000000

_ASCIISTRING_PARSE_FN = 4386398
_UPGRADE_TEMPLATE_PARSE_FN = 7581577

_SPELLSTORE_RESOLVER_VA = 0x0071F96E
_SPELLSTORE_RESOLVER_BYTES = bytes.fromhex("568bcfe82cf6ffff")
_SPELLSTORE_RESUME_VA = 0x0071F976
_FIND_COMMAND_SET_VA = 0x0071EFA2
_STRING_ISEMPTY_VA = 0x00401E64

_PLAYER_TEMPLATE_FIELD_TABLE = 0x00BF81A8
_PLAYER_TEMPLATE_FIELD_TABLE_REFS = PLAYER_TEMPLATE_FIELD_TABLE_REFS
_PLAYER_TEMPLATE_FIELD_TABLE_REF_OPCODES = PLAYER_TEMPLATE_FIELD_TABLE_REF_OPCODES

_PLAYER_UPGRADES_IN_PROGRESS = 0x0BC
_PLAYER_UPGRADES_COMPLETED = 0x14C
_PLAYER_TEMPLATE_COMMANDSET = 0x138
_PLAYER_TEMPLATE_COMMANDSET_MP = 0x13C
_PLAYER_TEMPLATE_SIDE = 0x18

_KEY_OFF = 0x00
_ROWS_OFF = 0x10
ROWS = 64
ROW_STRIDE = 12
_ROWS_MASK = ROWS - 1
_ROW_COMMANDSET_OFF = 0x04
_ROW_UPGRADE_OFF = 0x08

_FINGERPRINT = {
    "PurchaseScienceCommandSet": _PLAYER_TEMPLATE_COMMANDSET,
    "PurchaseScienceCommandSetMP": _PLAYER_TEMPLATE_COMMANDSET_MP,
    "SpecialPowerShortcutCommandSet": 0x140,
    "SpellBook": 0x1B4,
    "SpellBookMp": 0x1B8,
}

_FIELDS = (
    ("PurchaseScienceCommandSetMP2", 0),
    ("PurchaseScienceNeededUpgrade", 1),
)


def _u32(value: int) -> bytes:
    return struct.pack("<I", value)


def _read_field_table(data: bytes | bytearray, base_va: int) -> tuple[tuple[int, int, int, int], ...]:
    off = va_to_offset(data, base_va)
    if off is None:
        raise ValueError(f"the field table at 0x{base_va:08x} is not mapped")
    entries: list[tuple[int, int, int, int]] = []
    for index in range(1024):
        entry = struct.unpack_from("<IIII", data, off + index * 16)
        if entry[0] == 0 and entry[1] == 0:
            return tuple(entries)
        entries.append(entry)
    raise ValueError(f"the field table at 0x{base_va:08x} is not terminated")


def _resolve_table(data: bytes | bytearray) -> int:
    bases = []
    for ref_va, opcode in zip(_PLAYER_TEMPLATE_FIELD_TABLE_REFS, _PLAYER_TEMPLATE_FIELD_TABLE_REF_OPCODES, strict=True):
        off = va_to_offset(data, ref_va)
        if off is None:
            raise ValueError(f"0x{ref_va:08x} is not mapped - not the expected build")
        if data[off] != opcode:
            raise ValueError(
                f"the PlayerTemplate table reference at 0x{ref_va:08x} is opcode 0x{data[off]:02x}, "
                f"expected 0x{opcode:02x} - not the expected build"
            )
        bases.append(struct.unpack_from("<I", data, off + 1)[0])
    if len(set(bases)) != 1:
        disagreement = ", ".join(f"0x{ref_va:08x}->0x{base:08x}" for ref_va, base in zip(_PLAYER_TEMPLATE_FIELD_TABLE_REFS, bases, strict=True))
        raise ValueError(f"the PlayerTemplate table refs disagree: {disagreement}")
    return bases[0]


def _table_bytes(table_va: int, entries: tuple[tuple[int, int, int, int], ...], parse_fn: int) -> tuple[bytes, bytes]:
    table_size = (len(entries) + len(_FIELDS) + 1) * 16
    strings = bytearray()
    name_vas: list[int] = []
    for name, _user_data in _FIELDS:
        name_vas.append(table_va + table_size + len(strings))
        strings += name.encode("ascii") + b"\x00"
    strings += b"\x00" * (-len(strings) % 4)

    table = bytearray()
    for entry in entries:
        table += struct.pack("<IIII", *entry)
    for (name, user_data), name_va in zip(_FIELDS, name_vas, strict=True):
        table += struct.pack("<IIII", name_va, parse_fn, user_data, 0)
    table += struct.pack("<IIII", 0, 0, 0, 0)
    assert len(table) == table_size
    return bytes(table), bytes(strings)


def _table_span(entries: tuple[tuple[int, int, int, int], ...]) -> int:
    table_size = (len(entries) + len(_FIELDS) + 1) * 16
    strings = sum(len(name) + 1 for name, _user_data in _FIELDS)
    return table_size + strings + (-strings % 4)


def _emit_probe(a: Asm, tag: str, rows_va: int, *, claim: bool) -> None:
    a.emit(0x53, 0x51, 0x52)  # push ebx / push ecx / push edx
    a.emit(0x85, 0xC0)  # test eax, eax
    a.jcc(JE, f"{tag}_none")
    a.emit(0x8B, 0xD8)  # mov ebx, eax               ; the key
    a.emit(0x25, _u32(_ROWS_MASK))  # and eax, ROWS-1
    a.emit(0xB9, _u32(ROWS))  # mov ecx, ROWS              ; probe budget
    a.label(f"{tag}_probe")
    a.emit(0x8B, 0xD0)  # mov edx, eax
    a.emit(0x6B, 0xD2, ROW_STRIDE)  # imul edx, edx, ROW_STRIDE
    a.emit(0x81, 0xC2, _u32(rows_va))  # add edx, rows
    a.emit(0x83, 0x3A, 0x00)  # cmp dword ptr [edx], 0
    a.jcc(JE, f"{tag}_empty")
    a.emit(0x39, 0x1A)  # cmp dword ptr [edx], ebx
    a.jcc(JE, f"{tag}_hit")
    a.emit(0x40)  # inc eax
    a.emit(0x25, _u32(_ROWS_MASK))  # and eax, ROWS-1
    a.emit(0x49)  # dec ecx
    a.jcc(JNE, f"{tag}_probe")
    a.label(f"{tag}_none")
    a.emit(0x31, 0xC0)  # xor eax, eax
    a.jmp(f"{tag}_out")
    a.label(f"{tag}_empty")
    if claim:
        a.emit(0x89, 0x1A)  # mov [edx], ebx
        a.emit(0x83, 0x62, 0x04, 0x00)  # and dword ptr [edx+4], 0
        a.emit(0x83, 0x62, 0x08, 0x00)  # and dword ptr [edx+8], 0
    else:
        a.emit(0x31, 0xC0)  # xor eax, eax
        a.jmp(f"{tag}_out")
    a.label(f"{tag}_hit")
    a.emit(0x8B, 0xC2)  # mov eax, edx
    a.label(f"{tag}_out")
    a.emit(0x5A, 0x59, 0x5B)  # pop edx / pop ecx / pop ebx
    a.emit(0xC3)  # ret


def _emit_lookup(a: Asm, rows_va: int) -> None:
    a.label("lookup")
    _emit_probe(a, "lu", rows_va, claim=False)


def _emit_insert(a: Asm, rows_va: int) -> None:
    a.label("insert")
    _emit_probe(a, "in", rows_va, claim=True)


def _emit_block(a: Asm, key_va: int) -> None:
    a.label("block")
    a.emit(0x8B, 0xF8)  # mov edi, eax
    a.emit(0xA3, _u32(key_va))  # mov [g_key], eax
    a.emit(0x57)  # push edi
    a.call_absolute(PLAYER_TEMPLATE_FIND_BY_KEY)
    a.emit(0x50)  # push eax ; keep the lookup result for the displaced code
    a.emit(0xA1, _u32(key_va))  # mov eax, [g_key]
    a.call("insert")
    a.emit(0x85, 0xC0)  # test eax, eax
    a.jcc(JE, "block_restore")
    a.emit(0x83, 0x60, 0x04, 0x00)  # and dword ptr [eax+4], 0
    a.emit(0x83, 0x60, 0x08, 0x00)  # and dword ptr [eax+8], 0
    a.label("block_restore")
    a.emit(0x58)  # pop eax ; restore the lookup result expected by the original code
    a.jmp_absolute(PLAYER_TEMPLATE_BLOCK_KEY_RESUME)


def _emit_parse(a: Asm, key_va: int) -> None:
    a.label("parse")
    a.emit(0x55)  # push ebp
    a.emit(0x89, 0xE5)  # mov ebp, esp
    a.emit(0x83, 0xEC, 0x04)  # sub esp, 4                ; scratch store if the row is missing
    a.emit(0x53)  # push ebx
    a.emit(0x56)  # push esi
    a.emit(0x31, 0xDB)  # xor ebx, ebx              ; the row, 0 until claimed
    a.emit(0xA1, _u32(key_va))  # mov eax, [g_key]
    a.emit(0x85, 0xC0)  # test eax, eax
    a.jcc(JE, "parse_dispatch")
    a.call("lookup")
    a.emit(0x8B, 0xD8)  # mov ebx, eax
    a.label("parse_dispatch")
    a.emit(0x83, 0x7D, 0x14, 0x00)  # cmp dword ptr [ebp+0x14], 0
    a.jcc(JNE, "parse_upgrade")

    a.emit(0x85, 0xDB)  # test ebx, ebx
    a.jcc(JE, "parse_commandset_scratch")
    a.emit(0x8D, 0x43, _ROW_COMMANDSET_OFF)  # lea eax, [ebx+4]
    a.jmp("parse_commandset_call")
    a.label("parse_commandset_scratch")
    a.emit(0x8D, 0x45, 0xFC)  # lea eax, [ebp-4]
    a.label("parse_commandset_call")
    a.emit(0x6A, 0x00)  # push 0
    a.emit(0x50)  # push eax
    a.emit(0xFF, 0x75, 0x0C)  # push [ebp+0xc]
    a.emit(0xFF, 0x75, 0x08)  # push [ebp+8]
    a.call_absolute(_ASCIISTRING_PARSE_FN)
    a.emit(0x83, 0xC4, 0x10)  # add esp, 16
    a.jmp("parse_out")

    a.label("parse_upgrade")
    a.emit(0x85, 0xDB)  # test ebx, ebx
    a.jcc(JE, "parse_upgrade_scratch")
    a.emit(0x8D, 0x43, _ROW_UPGRADE_OFF)  # lea eax, [ebx+8]
    a.jmp("parse_upgrade_call")
    a.label("parse_upgrade_scratch")
    a.emit(0x8D, 0x45, 0xFC)  # lea eax, [ebp-4]
    a.label("parse_upgrade_call")
    a.emit(0x6A, 0x00)  # push 0
    a.emit(0x50)  # push eax
    a.emit(0xFF, 0x75, 0x0C)  # push [ebp+0xc]
    a.emit(0xFF, 0x75, 0x08)  # push [ebp+8]
    a.call_absolute(_UPGRADE_TEMPLATE_PARSE_FN)
    a.emit(0x83, 0xC4, 0x10)  # add esp, 16

    a.label("parse_out")
    a.emit(0x5E)  # pop esi
    a.emit(0x5B)  # pop ebx
    a.emit(0x89, 0xEC)  # mov esp, ebp
    a.emit(0x5D)  # pop ebp
    a.emit(0xC3)  # ret


def _emit_spellstore_hook(a: Asm, key_va: int) -> None:
    a.label("spellstore")
    a.emit(0x56)  # push esi ; preserve the original field for the fallback path
    a.emit(0x8B, 0x44, 0x24, 0x0C)  # mov eax, [esp+0x0c] ; player
    a.emit(0x85, 0xC0)  # test eax, eax
    a.jcc(JZ, "stock")
    a.emit(0x8B, 0x40, PLAYER_PLAYER_TEMPLATE)  # mov eax, [eax+0x34] ; template
    a.emit(0x85, 0xC0)  # test eax, eax
    a.jcc(JZ, "stock")
    a.emit(0x8B, 0x48, PLAYER_TEMPLATE_NAME_KEY)  # mov ecx, [eax+0x10] ; faction key
    a.call("lookup")
    a.emit(0x85, 0xC0)  # test eax, eax
    a.jcc(JZ, "stock")

    a.label("have_row")
    a.emit(0x8B, 0xD0)  # mov edx, eax ; row
    a.emit(0x8B, 0x72, _ROW_COMMANDSET_OFF)  # mov esi, [edx+4] ; MP2 command set
    a.emit(0x85, 0xF6)  # test esi, esi
    a.jcc(JZ, "stock")
    a.emit(0x8B, 0xCE)  # mov ecx, esi
    a.call_absolute(_STRING_ISEMPTY_VA)
    a.emit(0x84, 0xC0)  # test al, al
    a.jcc(JZ, "stock")
    a.emit(0x58)  # pop eax ; discard the preserved original field, the alternate won
    a.emit(0x56)  # push esi
    a.emit(0x8B, 0xCF)  # mov ecx, edi
    a.call_absolute(_FIND_COMMAND_SET_VA)
    a.jmp_absolute(_SPELLSTORE_RESUME_VA)

    a.label("stock")
    a.emit(0x5E)  # pop esi ; restore the original field before finding the set
    a.emit(0x56)  # push esi
    a.emit(0x8B, 0xCF)  # mov ecx, edi
    a.call_absolute(_FIND_COMMAND_SET_VA)
    a.jmp_absolute(_SPELLSTORE_RESUME_VA)


def _build_code(base_va: int, upgrade_id: int) -> bytes:
    """Flip the chosen field when the player has the requested upgrade, then call stock code."""
    upgrade_word = _PLAYER_UPGRADES_COMPLETED + (upgrade_id // 32) * 4
    upgrade_mask = 1 << (upgrade_id % 32)

    a = Asm(base_va)
    # The hook lands at the stock `push esi`; `esi` already holds the currently chosen field.
    a.emit(0x56)  # push esi ; preserve the original field for the fallback path
    a.emit(0x8B, 0x44, 0x24, 0x0C)  # mov eax, [esp+0x0c] ; player
    a.emit(0x85, 0xC0)  # test eax, eax
    a.jcc(JZ, "stock")
    a.emit(0xF7, 0x80, _u32(upgrade_word), _u32(upgrade_mask))  # test [eax+off], mask
    a.jcc(JZ, "stock")

    a.emit(0x8B, 0x50, PLAYER_PLAYER_TEMPLATE)  # mov edx, [eax+0x34] ; template
    a.emit(0x85, 0xD2)  # test edx, edx
    a.jcc(JZ, "stock")
    a.emit(0x8D, 0x8A, _u32(_PLAYER_TEMPLATE_COMMANDSET))  # lea ecx, [edx+0x138]
    a.emit(0x3B, 0xF1)  # cmp esi, ecx
    a.jcc(JE, "to_mp")
    a.emit(0x8D, 0x72, 0x04)  # lea esi, [edx+0x13c]
    a.jmp("recheck")
    a.label("to_mp")
    a.emit(0x8D, 0x72, 0x00)  # lea esi, [edx+0x138]

    a.label("recheck")
    a.emit(0x8B, 0xCE)  # mov ecx, esi
    a.call_absolute(_STRING_ISEMPTY_VA)
    a.emit(0x84, 0xC0)  # test al, al
    a.jcc(JZ, "stock")
    a.emit(0x58)  # pop eax ; discard the preserved original field, the alternate won
    a.emit(0x31, 0xC0)  # xor eax, eax
    a.jmp_absolute(_SPELLSTORE_RESUME_VA)

    a.label("stock")
    a.emit(0x5E)  # pop esi ; restore the original field before finding the set
    a.emit(0x56)  # push esi
    a.emit(0x8B, 0xCF)  # mov ecx, edi
    a.call_absolute(_FIND_COMMAND_SET_VA)
    a.jmp_absolute(_SPELLSTORE_RESUME_VA)
    return a.finish()


class SpellStoreCommandSetUpgradePatch(Patch):
    name = "spellstore-commandset-upgrade"
    description = (
        "Add PurchaseScienceCommandSetMP2 and PurchaseScienceNeededUpgrade to PlayerTemplate, "
        "then switch the SpellStore to MP2 while the chosen upgrade is present"
    )

    def apply(self, data: bytearray) -> None:
        table_va = self._resolve(data)
        entries = self._check_build(data, table_va)
        section_va = allocate_section(
            data, SECTION_NAME, lambda base: self._build_section(base, entries), _CHARACTERISTICS
        )
        for file_off, old, new, note in self._edits(data, section_va, entries, table_va):
            apply_byte_patch(data, file_off, old, new, note)

    def verify(self, data: bytes | bytearray) -> list[str]:
        problems: list[str] = []
        located = find_section(data, SECTION_NAME)
        if located is None:
            return [f"no {SECTION_NAME} section: the file does not carry this patch"]
        section_va, section_off, _vsize = located
        try:
            table_va = self._resolve(data)
            all_entries = _read_field_table(data, table_va)
        except ValueError as exc:
            return [f"cannot read the PlayerTemplate field table: {exc}"]

        entries: tuple[tuple[int, int, int, int], ...] | None = None
        for index, entry in enumerate(all_entries):
            if read_cstring(data, entry[0]) == _FIELDS[0][0]:
                entries = all_entries[:index]
                break
        if entries is None:
            return [f"the PlayerTemplate table does not name {_FIELDS[0][0]}"]

        by_name = {read_cstring(data, entry[0]): entry for entry in all_entries}
        parse_fn = self._assemble(section_va, entries).label_va("parse")
        for field, want in _FINGERPRINT.items():
            entry = by_name.get(field)
            if entry is None:
                problems.append(f"the PlayerTemplate table does not name {field}")
                continue
            if entry[3] != want:
                problems.append(f"unexpected build: PlayerTemplate.{field} is at {entry[3]:#x}, expected {want:#x}")

        for field, user_data in _FIELDS:
            entry = by_name.get(field)
            if entry is None:
                problems.append(f"the PlayerTemplate table does not name {field}")
                continue
            if entry[1] != parse_fn:
                problems.append(f"{field} does not use the patch's own parse function")
            if entry[2] != user_data:
                problems.append(f"{field} carries userData {entry[2]}, expected {user_data}")
            if entry[3] != 0:
                problems.append(f"{field} has a non-zero template offset ({entry[3]:#x})")

        expected = self._build_section(section_va, entries)
        got = bytes(data[section_off : section_off + len(expected)])
        if got != expected:
            problems.append(f"{SECTION_NAME} cave bytes do not match the expected layout")

        for va, old, target, note in self._edits(data, section_va, entries, table_va, table_ref=False):
            got = bytes(data[va : va + len(target)])
            if got != target:
                problems.append(f"{note} @0x{va:x}: expected {target.hex()}, got {got.hex()}")
        return problems

    def ini_surface(self):
        import importlib

        engine = importlib.import_module("sage_ini.engine")

        return engine.Engine(
            fields=(
                engine.FieldDelta(
                    "PlayerTemplate",
                    field,
                    "String" if user_data == 0 else "Ref:upgrades",
                    "\"\"" if user_data == 0 else 0,
                    patch=self.name,
                )
                for field, user_data in _FIELDS
            )
        )

    @staticmethod
    def _resolve(data: bytes | bytearray) -> int:
        return _resolve_table(data)

    @staticmethod
    def _check_build(data: bytes | bytearray, table_va: int) -> tuple[tuple[int, int, int, int], ...]:
        entries = _read_field_table(data, table_va)
        by_name = {read_cstring(data, name): offset for name, _fn, _ud, offset in entries}
        for field, want in _FINGERPRINT.items():
            got = by_name.get(field)
            if got != want:
                raise ValueError(
                    f"unexpected build: PlayerTemplate.{field} is at "
                    f"{'absent' if got is None else hex(got)}, expected {want:#x}"
                )
        for field, _user_data in _FIELDS:
            if field in by_name:
                raise ValueError(
                    f"the PlayerTemplate table already names {field} - this patch is already "
                    "applied, or another patch has added the same field"
                )
        return entries

    @staticmethod
    def _code_offset(entries: tuple[tuple[int, int, int, int], ...]) -> int:
        return _ROWS_OFF + ROWS * ROW_STRIDE + _table_span(entries)

    def _build_section(self, base_va: int, entries: tuple[tuple[int, int, int, int], ...]) -> bytes:
        code = self._assemble(base_va, entries)
        table, strings = _table_bytes(base_va + _ROWS_OFF + ROWS * ROW_STRIDE, entries, code.label_va("parse"))
        body = bytearray(_ROWS_OFF + ROWS * ROW_STRIDE)
        body += table + strings
        assert len(body) == self._code_offset(entries)
        return bytes(body) + code.finish()

    def _assemble(self, base_va: int, entries: tuple[tuple[int, int, int, int], ...]) -> Asm:
        a = Asm(base_va + self._code_offset(entries))
        _emit_lookup(a, base_va + _ROWS_OFF)
        _emit_insert(a, base_va + _ROWS_OFF)
        _emit_block(a, base_va + _KEY_OFF)
        _emit_parse(a, base_va + _KEY_OFF)
        _emit_spellstore_hook(a, base_va + _KEY_OFF)
        a.finish()
        return a

    def _edits(
        self,
        data: bytes | bytearray,
        section_va: int,
        entries: tuple[tuple[int, int, int, int], ...],
        old_table: int,
        *,
        table_ref: bool = True,
    ) -> list[tuple[int, bytes, bytes, str]]:
        labels = self._assemble(section_va, entries).label_va
        out: list[tuple[int, bytes, bytes, str]] = []

        def at(va: int) -> int:
            off = va_to_offset(data, va)
            if off is None:
                raise ValueError(f"0x{va:08x} is not mapped - not the expected build")
            return off

        if table_ref:
            for ref_va, opcode in zip(_PLAYER_TEMPLATE_FIELD_TABLE_REFS, _PLAYER_TEMPLATE_FIELD_TABLE_REF_OPCODES, strict=True):
                out.append(
                    (
                        at(ref_va),
                        bytes([opcode]) + _u32(old_table),
                        bytes([opcode]) + _u32(section_va + _ROWS_OFF + ROWS * ROW_STRIDE),
                        f"PlayerTemplate field table ref @0x{ref_va:08x}",
                    )
                )

        out.append(
            (
                at(PLAYER_TEMPLATE_BLOCK_KEY),
                PLAYER_TEMPLATE_BLOCK_KEY_BYTES,
                b"\xe9" + struct.pack(
                    "<i", labels("block") - (PLAYER_TEMPLATE_BLOCK_KEY + 5)
                )
                + b"\x90" * (len(PLAYER_TEMPLATE_BLOCK_KEY_BYTES) - 5),
                "PlayerTemplate block key -> SpellStore row key",
            )
        )

        out.append(
            (
                at(_SPELLSTORE_RESOLVER_VA),
                _SPELLSTORE_RESOLVER_BYTES,
                b"\xe9" + struct.pack(
                    "<i", labels("spellstore") - (_SPELLSTORE_RESOLVER_VA + 5)
                )
                + b"\x90" * (len(_SPELLSTORE_RESOLVER_BYTES) - 5),
                "SpellStore resolver -> spellstore-commandset-upgrade cave",
            )
        )
        return out