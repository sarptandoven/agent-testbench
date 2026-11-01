from textstats import reading_minutes, top_words, word_count, words


def test_words_are_lowercase_and_keep_apostrophes():
    assert words("It's a Cat, a CAT!") == ["it's", "a", "cat", "a", "cat"]


def test_word_count():
    assert word_count("one two  three\nfour") == 4
    assert word_count("") == 0


def test_top_words_are_the_most_common():
    assert top_words("b a c a b a", 2) == [("a", 3), ("b", 2)]


def test_top_words_breaks_ties_alphabetically():
    assert top_words("pear apple fig", 3) == [("apple", 1), ("fig", 1), ("pear", 1)]


def test_reading_time():
    assert reading_minutes("word " * 1000) == 5
    assert reading_minutes("just a few words") == 1
    assert reading_minutes("") == 0
