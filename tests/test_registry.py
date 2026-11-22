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


def test_pending_joins_are_unique_per_chat_and_link():
    registry = Registry()
    registry.add_integration("a")
    registry.add_integration("b")
    first = registry.add_pending("a", chat_id=None, invite_hash="h", title="T")
    assert registry.add_pending("b", chat_id=None, invite_hash="h", title="T").integration == "a"
    registry.add_pending("b", chat_id=-1007, invite_hash=None, title="X")
    registry.add_pending("b", chat_id=None, invite_hash="other", title="Y")  # several NULL chat_ids are fine
    assert len(registry.pendings()) == 3
    assert [p.id for p in registry.pending_matches(-1009, "t")] == [first.id]
    assert registry.pending_matches(-1009, None) == []
    registry.remove_integration("a")
    assert [p.integration for p in registry.pendings()] == ["b", "b"]
