#!/usr/bin/env python3
"""Unit checks for Pokémon singles rarity filters (no network)."""

from pokemon_singles import include_card, number_sort_key


def assert_include(family, name, rarity, number="", subset=None, expected=True):
    got = include_card(family, name, rarity, number, subset)
    assert got is expected, f"{family=} {name=} {rarity=} {subset=} -> {got} (want {expected})"


def test_xy_era():
    assert_include("xy", "Charizard EX (11)", "Ultra Rare", "11/106")
    assert_include("xy", "Charizard EX (100 Full Art)", "Ultra Rare", "100/106")
    assert_include("xy", "M Charizard EX (Y) (Secret)", "Secret Rare", "107/106")
    assert_include("xy", "Raichu BREAK", "Rare BREAK", "50/162")
    assert_include("xy", "Lysandre (104 Full Art)", "Ultra Rare", "104/106")
    assert_include("xy", "Metapod", "Uncommon", "2/106", expected=False)
    assert_include("xy", "Flashfire Booster Box", None, "", expected=False)
    assert_include("xy", "Chikorita", "Common", "RC1/RC32", subset="radiant-collection")
    assert_include("xy", "Flareon EX (Full Art)", "Ultra Rare", "RC28/RC32", subset="radiant-collection")


def test_sm_era():
    assert_include("sun-and-moon", "Leafeon GX", "Ultra Rare", "13/156")
    assert_include("sun-and-moon", "Leafeon GX (Full Art)", "Ultra Rare", "139/156")
    assert_include("sun-and-moon", "Venusaur & Snivy GX", "Ultra Rare", "1/236")
    assert_include("sun-and-moon", "Jessie & James (Full Art)", "Ultra Rare", "68/68")
    assert_include("sun-and-moon", "Cynthia (Full Art)", "Ultra Rare", "148/156")
    assert_include("sun-and-moon", "Giratina Prism Star", "Prism Rare", "58/156")
    assert_include("sun-and-moon", "Leafeon GX (Secret Rare)", "Secret Rare", "157/156")
    assert_include("sun-and-moon", "Venusaur & Snivy GX (Secret)", "Rainbow Rare", "241/236")
    assert_include("sun-and-moon", "Crushing Hammer (Secret Rare)", "Secret Rare", "166/156")
    assert_include("sun-and-moon", "Charizard GX", "Shiny Holo Rare", "SV49/SV94", subset="shiny-vault")
    assert_include("sun-and-moon", "Scyther", "Shiny Holo Rare", "SV1/SV94", subset="shiny-vault", expected=False)
    assert_include("sun-and-moon", "Shining Mew", "Shiny Holo Rare", "40/73")


def test_swsh_era():
    assert_include("sword-and-shield", "Espeon V", "Ultra Rare", "64/203")
    assert_include("sword-and-shield", "Leafeon V (Full Art)", "Ultra Rare", "166/203")
    assert_include("sword-and-shield", "Leafeon VMAX (Alternate Art Secret)", "Secret Rare", "204/203")
    assert_include("sword-and-shield", "Reshiram", "Amazing Rare", "017/072")
    assert_include("sword-and-shield", "Radiant Charizard", "Radiant Rare", "020/159")
    assert_include("sword-and-shield", "Skyla (Full Art)", "Ultra Rare", "072/072")
    assert_include("sword-and-shield", "Altaria", "Ultra Rare", "GG19/GG70", subset="galarian-gallery")
    assert_include("sword-and-shield", "Boltund V", "Ultra Rare", "TG13/TG30", subset="trainer-gallery")
    assert_include("sword-and-shield", "Charizard VMAX", "Shiny Holo Rare", "SV107/SV122", subset="shiny-vault")
    assert_include("sword-and-shield", "Rowlet", "Shiny Holo Rare", "SV001/SV122", subset="shiny-vault", expected=False)
    assert_include("sword-and-shield", "Hoppip", "Common", "1/203", expected=False)


def test_sv_mega_era():
    assert_include("scarlet-and-violet", "Charizard ex - 006/165", "Double Rare", "006/165")
    assert_include("scarlet-and-violet", "Charizard ex - 199/165", "Special Illustration Rare", "199/165")
    assert_include("scarlet-and-violet", "Squirtle - 170/165", "Illustration Rare", "170/165")
    assert_include("scarlet-and-violet", "Alakazam ex - 188/165", "Ultra Rare", "188/165")
    assert_include("scarlet-and-violet", "Mew ex - 205/165", "Hyper Rare", "205/165")
    assert_include("scarlet-and-violet", "Prime Catcher", "ACE SPEC Rare", "119/131")
    assert_include("scarlet-and-violet", "Oddish", "Shiny Rare", "092/091")
    assert_include("scarlet-and-violet", "Charizard ex - 234/091", "Shiny Ultra Rare", "234/091")
    assert_include("scarlet-and-violet", "Braviary (Master Ball Pattern)", "Uncommon", "078/086", expected=False)
    assert_include("mega-evolution", "Mega Venusaur ex - 003/132", "Double Rare", "003/132")
    assert_include("mega-evolution", "Mega Gardevoir ex - 187/132", "Mega Hyper Rare", "187/132")
    assert_include("mega-evolution", "Mewtwo ex", "Futuristic Rare", "157/128")
    assert_include("scarlet-and-violet", "Zekrom ex - 172/086", "Black White Rare", "172/086")
    assert_include("scarlet-and-violet", "Pineco - 001/091", "Common", "001/091", expected=False)
    assert_include("scarlet-and-violet", "Code Card - 151 Elite Trainer Box", "Code Card", "", expected=False)


def test_number_sort():
    keys = [number_sort_key(value) for value in ("11/106", "100/106", "RC1/RC32", "TG02/TG30")]
    assert keys[0] < keys[1]
    assert keys[1] < keys[2]
    assert keys[2] < keys[3]


if __name__ == "__main__":
    test_xy_era()
    test_sm_era()
    test_swsh_era()
    test_sv_mega_era()
    test_number_sort()
    print("ok")
