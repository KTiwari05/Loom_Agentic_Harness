import sys

from utils import slugify, titleize

assert slugify("Hello World!") == "hello-world", slugify("Hello World!")
assert slugify("  A_B  c ") == "a-b-c", slugify("  A_B  c ")
assert titleize("ab cd") == "Ab Cd"  # unchanged
sys.exit(0)
