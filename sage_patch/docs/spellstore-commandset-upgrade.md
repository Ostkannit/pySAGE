# `spellstore-commandset-upgrade` - add an upgrade-gated SpellStore variant

Targets the ROTWK SAGE-engine `game.dat` build ``2.01.2614.37001``.

## What it does

The stock SpellStore path already resolves a command set from the local player's `PlayerTemplate`
inside `sub_71F933`:

* `PlayerList::getLocalPlayer` feeds the local `Player*` into the SpellStore path.
* `sub_71F933` reads `Player+0x34` for the `PlayerTemplate*`.
* It chooses between the existing fields at `PlayerTemplate+0x138` and `+0x13C`.
* It then passes the chosen `AsciiString` to `ControlBar::findCommandSet`.

This patch adds two new `PlayerTemplate` fields, stored in a side table keyed by the template
name:

* `PurchaseScienceCommandSetMP2` - the alternate SpellStore command set.
* `PurchaseScienceNeededUpgrade` - the upgrade required to switch to MP2.

At runtime, the resolver keeps the stock path and only swaps to MP2 when the owning player has
the requested upgrade. If the upgrade is missing, or MP2 is empty, it falls back to the stock
command set.

## Hook

The hook lands at `0x0071F96E`, right before the stock `push esi` / `mov ecx, edi` / `call
sub_71EFA2` sequence. The cave preserves the original field on the stack, checks the row for the
current `PlayerTemplate`, tests the requested upgrade bit, and falls back to the stock lookup if
the alternate field is empty.

## Example

```sh
sage-patch apply spellstore-commandset-upgrade --in game.dat.backup --out game.dat

sage-patch verify spellstore-commandset-upgrade game.dat
```

## Tests

The repo now includes [`tests/test_spellstore_commandset_upgrade.py`](../tests/test_spellstore_commandset_upgrade.py), which checks:

* the patch is exported in the CLI registry;
* the patch advertises the new `PlayerTemplate` fields;
* `apply()` installs a verifiable cave on a copy of `game.dat.backup`.

## Caveats

This is client-side UI logic. It does not change the simulation, but a mod still needs the same
patched binary on every client if it wants the UI behavior to match everywhere.