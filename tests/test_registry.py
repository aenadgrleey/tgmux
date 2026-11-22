import pytest

from tgmux.registry import Registry, UnknownIntegrationError


def test_integrations_are_registered_once():
    registry = Registry()
    registry.add_integration("barabot")
    assert registry.has_integration("barabot")
    assert not registry.has_integration("nobody")
    with pytest.raises(ValueError):
        registry.add_integration("barabot")
    with pytest.raises(ValueError):
        registry.add_integration("Bad Name")


def test_first_claim_wins():
    registry = Registry()
    registry.add_integration("a")
    registry.add_integration("b")
    assert registry.claim(-1005, "a", "join") == "a"
    assert registry.claim(-1005, "b", "join") == "a"
    assert registry.owner(-1005) == "a"


def test_release_and_assign():
    registry = Registry()
    registry.add_integration("a")
    registry.add_integration("b")
    registry.claim(1, "a", "dm")
    assert not registry.release(1, "b")
    assert registry.assign(1, "b") == "a"
    assert registry.owned_by("b") == [1]
    assert registry.release(1)
    assert registry.owner(1) is None
    with pytest.raises(UnknownIntegrationError):
        registry.assign(1, "ghost")


def test_removing_integration_frees_chats(tmp_path):
    registry = Registry(tmp_path / "db.sqlite")
    registry.add_integration("a")
    registry.claim(1, "a", "dm")
    registry.remove_integration("a")
    assert registry.owner(1) is None
