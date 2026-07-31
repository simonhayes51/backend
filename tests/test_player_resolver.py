from app.services.player_resolver import _split_name_and_rating


class TestSplitNameAndRating:
    def test_trailing_rating_is_extracted(self):
        assert _split_name_and_rating("Mbappe 92") == ("Mbappe", 92)

    def test_no_rating_returns_full_query(self):
        assert _split_name_and_rating("Mbappe") == ("Mbappe", None)

    def test_multi_word_name_with_rating(self):
        assert _split_name_and_rating("Kylian Mbappe 92") == ("Kylian Mbappe", 92)

    def test_rating_out_of_range_is_not_treated_as_rating(self):
        # A trailing 2-3 digit number outside 40-99 isn't a real FUT
        # rating (e.g. a jersey number or year) - keep it as part of the name.
        assert _split_name_and_rating("Player 123") == ("Player 123", None)

    def test_single_digit_trailing_number_is_not_a_rating(self):
        assert _split_name_and_rating("Pele 9") == ("Pele 9", None)
