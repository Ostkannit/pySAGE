"""Tests for the spellstore-commandset-upgrade patch.

The regression this patch had was not the binary hook itself - `verify` already covers that -
but the INI surface that the mod loader reads back from the patched binary. These tests cover
that path end to end: the generated `.sagepatch` advertises the two new `PlayerTemplate` fields,
and `load_game` accepts a `PlayerTemplate` block that uses them.
"""

from __future__ import annotations

from pathlib import Path

from sage_ini.engine import dump_engine, load_engine, parse_engine
from sage_ini.loader import load_game
from sage_patch.sagepatch import generate_from_patches


def _surface_engine():
    generated, unknown = generate_from_patches(["spellstore-commandset-upgrade"])
    assert unknown == []
    return generated.engine


class TestSpellStoreSurface:
    def test_the_generated_surface_declares_the_two_new_player_template_fields(self):
        engine = _surface_engine()
        fields = {delta.name: delta for delta in engine.fields}

        assert set(fields) == {
            "PurchaseScienceCommandSetMP2",
            "PurchaseScienceNeededUpgrade",
        }
        assert all(delta.block == "PlayerTemplate" for delta in fields.values())

    def test_the_surface_round_trips_through_the_written_sagepatch(self):
        engine = _surface_engine()
        written = dump_engine(engine)
        loaded = parse_engine(written)

        assert {delta.name for delta in loaded.fields} == {
            "PurchaseScienceCommandSetMP2",
            "PurchaseScienceNeededUpgrade",
        }


class TestPlayerTemplateParsing:
    def test_load_game_accepts_the_new_fields_in_a_player_template_block(self, tmp_path: Path):
        engine = _surface_engine()
        sagepatch = tmp_path / ".sagepatch"
        sagepatch.write_text(dump_engine(engine), encoding="utf-8")

        (tmp_path / "playertemplate.ini").write_text(
            """PlayerTemplate TestFaction
    PurchaseScienceCommandSetMP2 = TestCommandSetMP2
        PurchaseScienceNeededUpgrade = Ref:upgrades
End
""",
            encoding="utf-8",
        )

        loaded = load_game(tmp_path, engine=load_engine(sagepatch))

        assert [d.code for d in loaded.diagnostics.items] == []
