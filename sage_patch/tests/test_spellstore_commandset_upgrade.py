from __future__ import annotations

import importlib.util
import sys
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _install_stub_package_tree() -> None:
    if "sage_patch" not in sys.modules:
        pkg = types.ModuleType("sage_patch")
        pkg.__path__ = [str(ROOT)]
        sys.modules["sage_patch"] = pkg
    if "sage_patch.patches" not in sys.modules:
        pkg = types.ModuleType("sage_patch.patches")
        pkg.__path__ = [str(ROOT / "patches")]
        sys.modules["sage_patch.patches"] = pkg


def _load_module(module_name: str, relative_path: str):
    _install_stub_package_tree()
    existing = sys.modules.get(module_name)
    if existing is not None:
        return existing
    spec = importlib.util.spec_from_file_location(module_name, ROOT / relative_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {module_name} from {relative_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def _load_spellstore_module():
    patcher_stub = types.ModuleType("sage_patch.patcher")

    class Patch:
        pass

    patcher_stub.Patch = Patch
    sys.modules["sage_patch.patcher"] = patcher_stub

    if "sage_ini" not in sys.modules:
        ini_pkg = types.ModuleType("sage_ini")
        ini_pkg.__path__ = []
        sys.modules["sage_ini"] = ini_pkg
    if "sage_ini.engine" not in sys.modules:
        engine_stub = types.ModuleType("sage_ini.engine")

        class Engine:
            def __init__(self, fields=()):
                self.fields = tuple(fields)

        class FieldDelta:
            def __init__(self, block, field, type_name, default, patch=""):
                self.block = block
                self.field = field
                self.type_name = type_name
                self.default = default
                self.patch = patch

        engine_stub.Engine = Engine
        engine_stub.FieldDelta = FieldDelta
        sys.modules["sage_ini.engine"] = engine_stub

    _load_module("sage_patch.pe", "pe.py")
    _load_module("sage_patch.utils", "utils.py")
    _load_module("sage_patch.asm", "asm.py")
    _load_module("sage_patch.addresses", "addresses.py")
    _load_module("sage_patch.patches.name_tables", "patches/name_tables.py")
    return _load_module(
        "sage_patch.patches.spellstore_commandset_upgrade",
        "patches/spellstore_commandset_upgrade.py",
    )


class SpellStoreCommandSetUpgradePatchTests(unittest.TestCase):
    def test_patch_is_listed_in_registry_sources(self) -> None:
        registry_text = (ROOT / "registry.py").read_text(encoding="utf-8")
        init_text = (ROOT / "patches" / "__init__.py").read_text(encoding="utf-8")
        self.assertIn(
            "SpellStoreCommandSetUpgradePatch.name: SpellStoreCommandSetUpgradePatch",
            registry_text,
        )
        self.assertIn("SpellStoreCommandSetUpgradePatch", init_text)

    def test_resolve_upgrade_and_apply_on_stock_game_dat(self) -> None:
        module = _load_spellstore_module()
        data = bytearray((ROOT / "game.dat.backup").read_bytes())
        patch = module.SpellStoreCommandSetUpgradePatch()

        patch.apply(data)

        located = sys.modules["sage_patch.utils"].find_section(data, module.SECTION_NAME)
        self.assertIsNotNone(located)
        self.assertEqual(patch.verify(data), [])

    def test_ini_surface_exposes_the_new_player_template_fields(self) -> None:
        module = _load_spellstore_module()
        patch = module.SpellStoreCommandSetUpgradePatch()

        surface = patch.ini_surface()

        def _surface_tuple(field):
            return (
                getattr(field, "block", getattr(field, "block_name", None)),
                getattr(field, "field", getattr(field, "name", getattr(field, "key", None))),
                getattr(field, "type_name", getattr(field, "type", getattr(field, "typeName", None))),
                getattr(field, "default", getattr(field, "default_value", None)),
                getattr(field, "patch", getattr(field, "patch_name", None)),
            )

        self.assertEqual(
            [_surface_tuple(field) for field in surface.fields],
            [
                ("PlayerTemplate", "PurchaseScienceCommandSetMP2", "String", '""', patch.name),
                (
                    "PlayerTemplate",
                    "PurchaseScienceNeededUpgrade",
                    "Ref:upgrades",
                    0,
                    patch.name,
                ),
            ],
        )

    def test_code_shape_starts_with_the_expected_hook_prologue(self) -> None:
        module = _load_spellstore_module()
        entries = ()
        code = module.SpellStoreCommandSetUpgradePatch()._assemble(0x0100_0000, entries).finish()
        self.assertTrue(code.startswith(bytes.fromhex("53 51 52")))
        self.assertIn(bytes.fromhex("56 8b 44 24 0c 85 c0"), code)


if __name__ == "__main__":
    unittest.main()