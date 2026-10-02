import re
import sys
from pathlib import Path

readme = Path("README.md")
assert readme.is_file(), "README.md was not created"
text = readme.read_text(encoding="utf-8", errors="replace")
low = text.lower()

assert text.lstrip().startswith("#"), "README must start with a markdown title"
assert re.search(r"^#{1,3}\s+installation", low, re.M), "missing an Installation heading"
assert re.search(r"^#{1,3}\s+usage", low, re.M), "missing a Usage heading"
assert len(text.split()) >= 40, "README is too thin"
sys.exit(0)
