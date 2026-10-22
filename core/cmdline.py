"""Split a command line into arguments the way the operating system does.

shlex.split() is a POSIX splitter: it treats a backslash as an escape
character, so "python C:\\dev\\app.py" turns into "C:devapp.py" and the
command runs against a path that does not exist. On Windows the rules are
the ones CommandLineToArgvW uses:

  * whitespace separates arguments
  * a double quote groups text into one argument
  * a backslash is literal unless it stands in front of a quote
    - 2n backslashes then a quote  -> n backslashes and the quote delimits
    - 2n+1 backslashes then a quote -> n backslashes and a literal quote
"""
import os
import shlex
from typing import List

__all__ = ["split_command"]


def _split_windows(command: str) -> List[str]:
    """Split a command line using the Windows rules."""
    args: List[str] = []
    current: List[str] = []
    in_quotes = False
    index = 0
    length = len(command)

    while index < length:
        char = command[index]

        if char == "\\":
            start = index
            while index < length and command[index] == "\\":
                index += 1
            backslashes = index - start
            if index < length and command[index] == '"':
                current.append("\\" * (backslashes // 2))
                if backslashes % 2:
                    # The quote is escaped, so it is part of the argument.
                    current.append('"')
                else:
                    in_quotes = not in_quotes
                index += 1
            else:
                current.append("\\" * backslashes)
            continue

        if char == '"':
            in_quotes = not in_quotes
            index += 1
            continue

        if char in " \t" and not in_quotes:
            if current:
                args.append("".join(current))
                current = []
            index += 1
            continue

        current.append(char)
        index += 1

    if current:
        args.append("".join(current))
    return args


def split_command(command: str) -> List[str]:
    """Split a command line into a list of arguments."""
    if not command or not command.strip():
        return []
    if os.name == "nt":
        return _split_windows(command)
    return shlex.split(command)
