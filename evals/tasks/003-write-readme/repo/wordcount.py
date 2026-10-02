"""wordcount — count words, lines and characters in text files (a tiny wc clone)."""

import sys
from pathlib import Path


def count(path: str) -> tuple[int, int, int]:
    text = Path(path).read_text(encoding="utf-8", errors="replace")
    return len(text.splitlines()), len(text.split()), len(text)


if __name__ == "__main__":
    for arg in sys.argv[1:]:
        lines, words, chars = count(arg)
        print(f"{lines:6} {words:6} {chars:6} {arg}")
