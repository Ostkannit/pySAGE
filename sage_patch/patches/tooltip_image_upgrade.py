"""Make `TooltipUpgrade` apply portrait and button-image overrides at runtime.

Targets the ROTWK SAGE-engine `game.dat` build ``2.01.2614.37001``.

`TooltipUpgrade` already updates two per-object UI strings (DisplayName/Description) when its
upgrade trigger is active. This patch extends that same apply path to also copy
`SelectPortrait`/`ButtonImage` from the module's `ModuleData` onto the live object, so a mod can
switch those images per object without transforming into a new template.

Implementation shape:
- hook `TooltipUpgrade`'s apply helper at ``0x008BBBDE`` with a `jmp rel32`;
- run a cave body that performs the two new writes and replays the stock two writes;
- jump back to the stock tail at ``0x008BBBFE``.

The engine-side INI parser for `TooltipUpgrade` also needs to know the two new field names.
That registration lives in the small helper at ``0x008BBB5D``; this patch redirects it to a
matching cave that appends `SelectPortrait` and `ButtonImage` to the accepted-name list.

The two helper routines this path already uses are reused as-is:
- `sub_0x006796DA` for DisplayName-like handling (`ReferenceDisplayName` sentinel support),
- `sub_0x00674FAE` for plain AsciiString copy-or-clear.
"""

from __future__ import annotations

import struct

from ..asm import Asm
from ..patcher import Patch
from ..utils import allocate_section, apply_byte_patch, find_section, va_to_offset

__all__ = ["TooltipImageUpgradePatch"]

SECTION_NAME = ".ttipimg"

_CHARACTERISTICS = 0x20 | 0x40 | 0x20000000 | 0x40000000

_FIELD_HOOK_VA = 0x008BBB5D
_FIELD_HOOK_OLD = bytes.fromhex("6a08e87c6b01008b4c240850e869fdb6ff8b4c24046a006898f8c600e859fdb6ffc3")

_HANDLER_VA = 0x008BBBCC
_HANDLER_REF_VA = 0x00C6F848

_HOOK_VA = 0x008BBBDE
_HOOK_OLD = bytes.fromhex("8b46f40538010000")

_RETURN_VA = 0x008BBBFE

_SET_DISPLAY_NAME_VA = 0x006796DA
_SET_ASCII_FIELD_VA = 0x00674FAE

_FIELD_SELECT_PORTRAIT_VA = 0x00C25B54
_FIELD_BUTTON_IMAGE_VA = 0x00BFC68C

_OFFSET_SELECT_PORTRAIT = 0x130
_OFFSET_BUTTON_IMAGE = 0x134
_OFFSET_DISPLAY_NAME = 0x138
_OFFSET_DESCRIPTION = 0x13C


def _offset(data: bytes | bytearray, va: int) -> int:
    off = va_to_offset(data, va)
    if off is None:
        raise ValueError(f"VA 0x{va:08x} is not mapped - not the expected build")
    return off


def _hook_bytes(code_va: int) -> bytes:
    jump = b"\xE9" + struct.pack("<i", code_va - (_HOOK_VA + 5))
    return jump + b"\x90" * (len(_HOOK_OLD) - len(jump))


def _field_hook_bytes(code_va: int) -> bytes:
    jump = b"\xE9" + struct.pack("<i", code_va - (_FIELD_HOOK_VA + 5))
    return jump + b"\x90" * (len(_FIELD_HOOK_OLD) - len(jump))


def build_field_registration(base_va: int) -> bytes:
    a = Asm(base_va)

    a.emit(0x6A, 0x08)  # push 8
    a.call_absolute(0x008D26E0)
    a.emit(0x8B, 0x4C, 0x24, 0x08)  # mov ecx, [esp+8]
    a.emit(0x50)  # push eax
    a.call_absolute(0x0042B8D7)

    a.emit(0x8B, 0x4C, 0x24, 0x04)  # mov ecx, [esp+4]
    a.emit(0x6A, 0x00)  # push 0
    a.emit(0x68, struct.pack("<I", 0x00C6F898))  # push DisplayName
    a.call_absolute(0x0042B8D7)

    a.emit(0x8B, 0x4C, 0x24, 0x04)  # mov ecx, [esp+4]
    a.emit(0x6A, 0x00)  # push 0
    a.emit(0x68, struct.pack("<I", _FIELD_SELECT_PORTRAIT_VA))  # push SelectPortrait
    a.call_absolute(0x0042B8D7)

    a.emit(0x8B, 0x4C, 0x24, 0x04)  # mov ecx, [esp+4]
    a.emit(0x6A, 0x00)  # push 0
    a.emit(0x68, struct.pack("<I", _FIELD_BUTTON_IMAGE_VA))  # push ButtonImage
    a.call_absolute(0x0042B8D7)

    a.emit(0xC3)  # retn
    return a.finish()


def build_code(base_va: int) -> bytes:
    """Cave body for the TooltipUpgrade apply hook.

    Entered from `_HOOK_VA`, with:
    - `esi` still the same object as in `sub_8BBBCC`;
    - `edi` the target object returned by `sub_70E013`.
    """

    a = Asm(base_va)

    # SelectPortrait  : module+0x130 -> target helper's managed field
    a.emit(0x8B, 0x46, 0xF4)  # mov eax, [esi-0x0c]
    a.emit(0x05, struct.pack("<I", _OFFSET_SELECT_PORTRAIT))  # add eax, 0x130
    a.emit(0x50)  # push eax
    a.emit(0x8B, 0xCF)  # mov ecx, edi
    a.call_absolute(_SET_ASCII_FIELD_VA)

    # ButtonImage     : module+0x134 -> target helper's managed field
    a.emit(0x8B, 0x46, 0xF4)  # mov eax, [esi-0x0c]
    a.emit(0x05, struct.pack("<I", _OFFSET_BUTTON_IMAGE))  # add eax, 0x134
    a.emit(0x50)  # push eax
    a.emit(0x8B, 0xCF)  # mov ecx, edi
    a.call_absolute(_SET_ASCII_FIELD_VA)

    # Stock behavior: DisplayName / Description
    a.emit(0x8B, 0x46, 0xF4)  # mov eax, [esi-0x0c]
    a.emit(0x05, struct.pack("<I", _OFFSET_DISPLAY_NAME))  # add eax, 0x138
    a.emit(0x50)  # push eax
    a.emit(0x8B, 0xCF)  # mov ecx, edi
    a.call_absolute(_SET_DISPLAY_NAME_VA)

    a.emit(0x8B, 0x46, 0xF4)  # mov eax, [esi-0x0c]
    a.emit(0x05, struct.pack("<I", _OFFSET_DESCRIPTION))  # add eax, 0x13c
    a.emit(0x50)  # push eax
    a.emit(0x8B, 0xCF)  # mov ecx, edi
    a.call_absolute(_SET_ASCII_FIELD_VA)

    a.jmp_absolute(_RETURN_VA)
    return a.finish()


class TooltipImageUpgradePatch(Patch):
    """Extend TooltipUpgrade runtime apply to also copy SelectPortrait/ButtonImage."""

    name = "tooltip-image-upgrade"
    description = (
        "Make TooltipUpgrade apply SelectPortrait and ButtonImage on the upgraded object "
        "without object transformation"
    )

    def apply(self, data: bytearray) -> None:
        field_va = allocate_section(data, ".ttipreg", build_field_registration, _CHARACTERISTICS)
        self._check_dispatch(data)
        code_va = allocate_section(data, SECTION_NAME, build_code, _CHARACTERISTICS)

        apply_byte_patch(
            data,
            _offset(data, _FIELD_HOOK_VA),
            _FIELD_HOOK_OLD,
            _field_hook_bytes(field_va),
            "TooltipUpgrade field registration -> tooltip-image-upgrade cave",
        )

        apply_byte_patch(
            data,
            _offset(data, _HOOK_VA),
            _HOOK_OLD,
            _hook_bytes(code_va),
            "TooltipUpgrade apply helper -> tooltip-image-upgrade cave",
        )

    @staticmethod
    def _check_dispatch(data: bytes | bytearray) -> None:
        target = struct.unpack_from("<I", data, _offset(data, _HANDLER_REF_VA))[0]
        if target != _HANDLER_VA:
            raise ValueError(
                f"TooltipUpgrade handler ref @0x{_HANDLER_REF_VA:08x} points to "
                f"0x{target:08x}, not 0x{_HANDLER_VA:08x}"
            )

    def verify(self, data: bytes | bytearray) -> list[str]:
        field_located = find_section(data, ".ttipreg")
        if field_located is None:
            return ["no .ttipreg section: the file does not carry this patch"]
        field_section_va, _field_off, field_vsize = field_located

        located = find_section(data, SECTION_NAME)
        if located is None:
            return [f"no {SECTION_NAME} section: the file does not carry this patch"]
        section_va, _off, vsize = located

        problems: list[str] = []
        try:
            self._check_dispatch(data)
        except ValueError as exc:
            return [str(exc)]

        hook_off = _offset(data, _HOOK_VA)
        if data[hook_off] != 0xE9:
            problems.append(
                f"TooltipUpgrade hook @0x{_HOOK_VA:08x} is not a jmp: "
                f"{bytes(data[hook_off:hook_off + len(_HOOK_OLD)]).hex()}"
            )
            return problems

        target = _HOOK_VA + 5 + struct.unpack_from("<i", data, hook_off + 1)[0]
        if not section_va <= target < section_va + vsize:
            problems.append(
                f"TooltipUpgrade hook jumps to 0x{target:08x}, outside {SECTION_NAME}"
            )
            return problems

        want = build_code(target)
        code_off = _offset(data, target)
        got = bytes(data[code_off : code_off + len(want)])
        if got != want:
            problems.append("the cave body does not match tooltip-image-upgrade")

        field_off = _offset(data, _FIELD_HOOK_VA)
        if data[field_off] != 0xE9:
            problems.append(
                f"TooltipUpgrade field hook @0x{_FIELD_HOOK_VA:08x} is not a jmp: "
                f"{bytes(data[field_off:field_off + len(_FIELD_HOOK_OLD)]).hex()}"
            )
            return problems

        field_target = _FIELD_HOOK_VA + 5 + struct.unpack_from("<i", data, field_off + 1)[0]
        if not field_section_va <= field_target < field_section_va + field_vsize:
            problems.append(
                f"TooltipUpgrade field hook jumps to 0x{field_target:08x}, outside .ttipreg"
            )
            return problems

        want_field = build_field_registration(field_target)
        field_code_off = _offset(data, field_target)
        got_field = bytes(data[field_code_off : field_code_off + len(want_field)])
        if got_field != want_field:
            problems.append("the field-registration cave does not match tooltip-image-upgrade")
        return problems
