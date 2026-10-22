"""The Great Green Snake must stay harmless.

This test exists so the easter egg can never grow teeth: it may only return
strings, never touch the filesystem, the network or the environment.
"""
import os

from doctor import easter_ggs


def test_ggs_returns_a_string():
    result = easter_ggs.ggs()
    assert isinstance(result, str)
    assert result
    assert "Snake" in result


def test_ggs_is_pure():
    """The same call always gives the same answer."""
    assert easter_ggs.ggs() == easter_ggs.ggs()


def test_maybe_greet_is_deterministic_for_a_given_seed():
    assert easter_ggs.maybe_greet(seed=0) == easter_ggs.ggs()
    assert easter_ggs.maybe_greet(seed=1) is None


def test_maybe_greet_usually_returns_none():
    results = [easter_ggs.maybe_greet(seed=n) for n in range(1, 11)]
    assert results.count(None) >= 8


def test_ggs_does_not_touch_the_filesystem_or_environment():
    before_files = set(os.listdir("."))
    before_env = dict(os.environ)

    easter_ggs.ggs()
    easter_ggs.maybe_greet(seed=0)

    assert set(os.listdir(".")) == before_files
    assert dict(os.environ) == before_env


def test_module_exposes_nothing_dangerous():
    public = [n for n in dir(easter_ggs) if not n.startswith("_")]
    # "annotations" and "Optional" come from the module's own typing imports.
    assert set(public) <= {"SNAKE", "MESSAGE", "ggs", "maybe_greet", "Optional", "annotations"}
