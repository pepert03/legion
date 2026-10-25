def summarize(text: str) -> str:
    """Summary of a text in three lines:

    Words: <number of words>
    Reading: <minutes at 200 words per minute, rounded up> min
    Frequent: <the 3 most frequent words in lowercase, each with its count in parentheses,
               separated by commas; ties in alphabetical order; "-" if there are none>

    A word is any sequence of letters, digits or underscores (\\w+).
    """
    raise NotImplementedError
