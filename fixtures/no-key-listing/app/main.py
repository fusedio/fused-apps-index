"""Counts the words in a note. Stdlib only."""


def main(text: str = "") -> dict:
    return {"words": len(text.split())}
