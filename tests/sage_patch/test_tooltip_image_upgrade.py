from __future__ import annotations

import struct

import pytest

from sage_patch.patches.tooltip_image_upgrade import (
    SECTION_NAME,
    TooltipImageUpgradePatch,
    _FIELD_HOOK_OLD,
    _FIELD_HOOK_VA,
    _HANDLER_REF_VA,
    _HANDLER_VA,
    _HOOK_OLD,
    _HOOK_VA,
    build_code,
)
from sage_patch.utils import find_section, va_to_offset
from tests.sage_patch.synthetic import synthetic_image


def _offset(data: bytes | bytearray, va: int) -> int:
    off = va_to_offset(data, va)
    assert off is not None, f"0x{va:08x} is not mapped"
    return off


def _plant_sites(data: bytearray) -> None:
    struct.pack_into("<I", data, _offset(data, _HANDLER_REF_VA), _HANDLER_VA)
    data[_offset(data, _FIELD_HOOK_VA) : _offset(data, _FIELD_HOOK_VA) + len(_FIELD_HOOK_OLD)] = _FIELD_HOOK_OLD
    data[_offset(data, _HOOK_VA) : _offset(data, _HOOK_VA) + len(_HOOK_OLD)] = _HOOK_OLD


@pytest.fixture
def image() -> bytearray:
    data = synthetic_image()
    _plant_sites(data)
    return data


def test_apply_then_verify(image: bytearray) -> None:
    patch = TooltipImageUpgradePatch()
    patch.apply(image)
    assert patch.verify(image) == []


def test_verify_rejects_unpatched_image(image: bytearray) -> None:
    assert TooltipImageUpgradePatch().verify(image) != []


def test_apply_rewrites_hook_to_jmp_into_its_cave(image: bytearray) -> None:
    patch = TooltipImageUpgradePatch()
    patch.apply(image)

    located = find_section(image, SECTION_NAME)
    assert located is not None
    base_va, _off, vsize = located

    hook_off = _offset(image, _HOOK_VA)
    assert image[hook_off] == 0xE9
    target = _HOOK_VA + 5 + struct.unpack_from("<i", image, hook_off + 1)[0]
    assert base_va <= target < base_va + vsize


def test_apply_writes_expected_cave_body(image: bytearray) -> None:
    patch = TooltipImageUpgradePatch()
    patch.apply(image)

    hook_off = _offset(image, _HOOK_VA)
    target = _HOOK_VA + 5 + struct.unpack_from("<i", image, hook_off + 1)[0]
    want = build_code(target)
    got = bytes(image[_offset(image, target) : _offset(image, target) + len(want)])
    assert got == want


def test_apply_twice_raises(image: bytearray) -> None:
    patch = TooltipImageUpgradePatch()
    patch.apply(image)
    with pytest.raises(ValueError):
        patch.apply(image)


def test_wrong_dispatch_target_is_refused(image: bytearray) -> None:
    struct.pack_into("<I", image, _offset(image, _HANDLER_REF_VA), _HANDLER_VA + 0x10)
    with pytest.raises(ValueError, match="TooltipUpgrade handler ref"):
        TooltipImageUpgradePatch().apply(image)
