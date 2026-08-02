"""
Tests for scripts/map_futgg_to_futbin.py's pure matching logic
(normalize_name/normalize_rarity/build_matches) - no database, no I/O.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.map_futgg_to_futbin import (
    FutbinCard,
    FutggCard,
    build_matches,
    normalize_name,
    normalize_rarity,
)


def test_normalize_name_strips_diacritics_case_and_punctuation():
    assert normalize_name("Joshua King") == normalize_name("  joshua-KING  ")
    assert normalize_name("Kylian Mbappé") == normalize_name("Kylian Mbappe")


def test_normalize_rarity_collapses_common_promo_vocab():
    assert normalize_rarity("TOTS") == normalize_rarity("Team of the Season")
    assert normalize_rarity("OTW") == normalize_rarity("Ones to Watch")
    assert normalize_rarity("Normal") == normalize_rarity("Common")


def test_unique_key_with_agreeing_rarity_is_high_confidence():
    futbin = [FutbinCard(1, "Joshua King", 89, "FUTTIES", "ST", "Al Khaleej")]
    futgg = [FutggCard(84071502, "Joshua King", 89, "FUTTIES", "ST", "Al Khaleej")]

    matches, unmatched, skipped = build_matches(futbin, futgg)

    assert len(matches) == 1
    m = matches[0]
    assert m.futgg_source_card_id == 84071502
    assert m.futbin_card_id == 1
    assert m.match_method == "name_rating_rarity"
    assert m.match_confidence == 0.95
    assert m.reviewed is True
    assert unmatched == 0
    assert skipped == 0


def test_unique_key_with_no_rarity_signal_is_lower_confidence_unreviewed():
    futbin = [FutbinCard(1, "Random Player", 75, None, "CM", "Some Club")]
    futgg = [FutggCard(999, "Random Player", 75, "Rare", "CM", "Some Club")]

    matches, _, _ = build_matches(futbin, futgg)

    assert len(matches) == 1
    assert matches[0].match_method == "name_rating_only"
    assert matches[0].reviewed is False


def test_no_candidate_is_unmatched_not_guessed():
    futbin = [FutbinCard(1, "Someone Else", 80, "Rare", "ST", "Club A")]
    futgg = [FutggCard(2, "Nobody Matching", 80, "Rare", "ST", "Club A")]

    matches, unmatched, skipped = build_matches(futbin, futgg)

    assert matches == []
    assert unmatched == 1
    assert skipped == 0


def test_ambiguous_duplicate_name_rating_disambiguated_by_rarity():
    futbin = [
        FutbinCard(1, "Duplicate Name", 82, "Rare", "ST", "Club A"),
        FutbinCard(2, "Duplicate Name", 82, "TOTW", "ST", "Club A"),
    ]
    futgg = [FutggCard(3, "Duplicate Name", 82, "Team of the Week", "ST", "Club A")]

    matches, unmatched, skipped = build_matches(futbin, futgg)

    assert len(matches) == 1
    assert matches[0].futbin_card_id == 2
    assert matches[0].match_method == "name_rating_rarity_disambiguated"
    assert matches[0].reviewed is True


def test_ambiguous_duplicate_never_resolved_is_skipped_not_guessed():
    """Two FUTBIN cards share name+rating AND rarity/position/club all
    tie - this must be left unmapped, never an arbitrary pick, per the
    "never fuzzy-match as a primary key" rule."""
    futbin = [
        FutbinCard(1, "Twin Card", 70, "Rare", "CM", "Club A"),
        FutbinCard(2, "Twin Card", 70, "Rare", "CM", "Club A"),
    ]
    futgg = [FutggCard(3, "Twin Card", 70, "Rare", "CM", "Club A")]

    matches, unmatched, skipped = build_matches(futbin, futgg)

    assert matches == []
    assert skipped == 1
    assert unmatched == 0


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
