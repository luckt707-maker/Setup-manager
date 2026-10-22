"""A very small hidden surprise.

The GGS - the Great Green Snake - is the mascot of the doctor. It is not part
of any check and it never changes anything: it only returns a string. Keeping
it here is harmless by design, and ``test_easter_ggs.py`` asserts exactly that.
"""
from __future__ import annotations

from typing import Optional

SNAKE = r"""
      _
     (_)
    /  \
   /    \
  |  ~   |
   \    /
    \__/
"""

MESSAGE = (
    "The Great Green Snake says: your build is fine, "
    "it just needed a moment to think about it."
)


def ggs() -> str:
    """Return the GGS greeting. Pure: same answer every time, no side effects."""
    return SNAKE + MESSAGE


def maybe_greet(seed: Optional[int] = None) -> Optional[str]:
    """Return the greeting roughly one run in ten, otherwise ``None``.

    With an explicit ``seed`` the result is deterministic, which is what the
    tests rely on.
    """
    if seed is None:
        # Cheap, no side effects, no RNG state shared with the rest of the app.
        seed = sum(bytearray(str(id(object())))) % 10
    if seed % 10 == 0:
        return ggs()
    return None
