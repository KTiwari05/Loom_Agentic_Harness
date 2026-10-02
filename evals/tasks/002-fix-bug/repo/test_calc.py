import sys

from calc import add, multiply

assert add(2, 3) == 5, f"add(2, 3) should be 5, got {add(2, 3)}"
assert add(-1, 1) == 0, f"add(-1, 1) should be 0, got {add(-1, 1)}"
assert multiply(3, 4) == 12
print("all tests passed")
sys.exit(0)
