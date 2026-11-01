"""Word statistics for plain text: the words, how many, the most common ones, and the reading time."""
import re
from collections import Counter

WORD = re.compile(r"[a-z0-9']+")


def words(text: str) -> list[str]:
    return WORD.findall(text.lower())


def word_count(text: str) -> int:
    return len(words(text))


def top_words(text: str, n: int = 3) -> list[tuple[str, int]]:
    """The n most common words with their counts, most common first; ties in alphabetical order."""
    counts = Counter(words(text))
    return sorted(counts.items(), key=lambda kv: (kv[1], kv[0]))[:n]


def reading_minutes(text: str, words_per_minute: int = 200) -> int:
    """Whole minutes to read the text, at least 1 for any text with words in it."""
    n = word_count(text)
    return 0 if n == 0 else max(1, round(n / words_per_minute))
