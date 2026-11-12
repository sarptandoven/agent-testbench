"""plugins.py: one registry for every kind of plug-in. Built-ins and installed packages sit in the same table; a
plug-in that fails to load, or takes a built-in's name, is left out and reported; nothing is loaded until a kind is
used. These tests use a made-up kind in a module of their own, so they never touch the real kinds."""
import sys
import types

import pytest

from agent_testbench import plugins as P


class Greeter:
    def __init__(self, name="", words="hello"):
        self.name, self.words = name, words


def _prepare(obj, name):
    obj = obj() if isinstance(obj, type) else obj
    obj.name = obj.name or name
    return obj


def _contract(obj):
    return [] if isinstance(getattr(obj, "words", None), str) and obj.words else ["it has no words to say"]


@pytest.fixture
def greeters(monkeypatch):
    mod = types.ModuleType("tb_fake_greeters")
    mod.Greeter = Greeter
    mod.loud = Greeter(words="HELLO")
    mod.KIND = P.Kind("greeters", "greeter", "say hello", {"plain": "tb_fake_greeters:Greeter"},
                      prepare=_prepare, contract=_contract)
    monkeypatch.setitem(sys.modules, "tb_fake_greeters", mod)
    monkeypatch.setitem(P.KINDS, "greeters", "tb_fake_greeters")
    eps = []
    monkeypatch.setattr(P, "entry_points", lambda kind_name: eps if kind_name == "greeters" else [])
    yield mod, eps
    P.reset("greeters")


class EP:
    def __init__(self, name, value, load):
        self.name, self.value, self._load = name, value, load

    def load(self):
        return self._load()


def test_built_ins_and_installed_plugins_share_one_table(greeters):
    mod, eps = greeters
    eps.append(EP("loud", "tb_fake_greeters:loud", lambda: mod.loud))
    assert P.names("greeters") == ["plain", "loud"]
    assert P.get("greeters", "plain").words == "hello" and P.get("greeters", "plain").name == "plain"
    assert P.get("greeters", "loud").name == "loud", "a plug-in without a name takes its entry point's"
    assert P.builtin("greeters", "plain") and not P.builtin("greeters", "loud")


def test_a_broken_plugin_or_one_taking_a_builtin_name_is_left_out_and_reported(greeters):
    mod, eps = greeters

    def boom():
        raise ImportError("no module named greetings_extra")
    eps += [EP("plain", "evil:Greeter", lambda: mod.loud), EP("shaky", "shaky:it", boom)]
    assert P.names("greeters") == ["plain"]
    assert P.get("greeters", "plain").words == "hello", "a built-in is never replaced"
    bad = P.broken("greeters")
    assert "tries to replace a greeter that already exists" in bad["plain"]
    assert "did not load: ImportError: no module named greetings_extra" in bad["shaky"]
    with pytest.raises(P.PluginError, match="installed but did not load .* `testbench plugins` shows it"):
        P.get("greeters", "shaky")
    with pytest.raises(P.PluginError, match="no greeter 'nope'; the greeters here are plain"):
        P.get("greeters", "nope")


def test_register_by_hand_and_the_contract(greeters):
    P.register("greeters", "quiet", Greeter(words="hi"))
    assert "quiet" in P.names("greeters")
    P.unregister("greeters", "quiet")
    assert "quiet" not in P.names("greeters")
    assert P.check("greeters", Greeter(words="hey")) == []
    assert P.check("greeters", Greeter(words="")) == ["it has no words to say"]
    assert P.check("greeters", 42)[0].startswith("it cannot be made into a greeter: AttributeError")


def test_unknown_kinds_and_kinds_not_yet_defined_are_named():
    with pytest.raises(P.PluginError, match="no plug-in kind 'widgets'; the kinds are backends, checks"):
        P.kind("widgets")
    P.reset()                                     # forgets loaded kinds; imports nothing to do it


def test_nothing_is_loaded_until_a_kind_is_used():
    import subprocess
    code = ("import sys, agent_testbench.plugins as P; "
            "print(sorted(m for m in P.KINDS.values() if m in sys.modules and m != 'agent_testbench.backends'))")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0 and out.stdout.strip() == "[]", out.stdout + out.stderr
