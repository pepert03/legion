def word_freq(text: str) -> dict[str, int]:
    """Frequency of each word, lowercased."""
    raise NotImplementedError


def top_words(text: str, n: int) -> list[tuple[str, int]]:
    """The n most frequent words; ties are ordered alphabetically."""
    raise NotImplementedError
