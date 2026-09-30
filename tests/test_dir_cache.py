from backend import dir_cache


async def test_get_returns_none_on_a_miss():
    assert await dir_cache.get("alice@pam", "vol", "/") is None


async def test_set_then_get_round_trips():
    listing = [{"text": "etc", "leaf": False}]
    await dir_cache.set("alice@pam", "vol", "/", listing)
    assert await dir_cache.get("alice@pam", "vol", "/") == listing


async def test_get_is_scoped_per_user():
    await dir_cache.set("alice@pam", "vol", "/", [{"text": "alice-saw-this"}])
    assert await dir_cache.get("mallory@pam", "vol", "/") is None


async def test_get_is_scoped_per_volume_and_path():
    await dir_cache.set("alice@pam", "vol-a", "/", [{"text": "a"}])
    assert await dir_cache.get("alice@pam", "vol-b", "/") is None
    assert await dir_cache.get("alice@pam", "vol-a", "/other") is None


async def test_set_overwrites_an_existing_entry():
    await dir_cache.set("alice@pam", "vol", "/", [{"text": "old"}])
    await dir_cache.set("alice@pam", "vol", "/", [{"text": "new"}])
    assert await dir_cache.get("alice@pam", "vol", "/") == [{"text": "new"}]


async def test_clear_empties_the_cache():
    await dir_cache.set("alice@pam", "vol", "/", [{"text": "etc"}])
    dir_cache.clear()
    assert await dir_cache.get("alice@pam", "vol", "/") is None


async def test_evict_missing_drops_a_volume_no_longer_present():
    await dir_cache.set("alice@pam", "pruned-vol", "/", [{"text": "gone"}])
    await dir_cache.set("alice@pam", "still-here", "/", [{"text": "kept"}])
    await dir_cache.evict_missing("alice@pam", frozenset({"still-here"}))
    assert await dir_cache.get("alice@pam", "pruned-vol", "/") is None
    assert await dir_cache.get("alice@pam", "still-here", "/") == [{"text": "kept"}]


async def test_evict_missing_with_no_existing_volumes_drops_everything_for_that_user():
    await dir_cache.set("alice@pam", "vol-a", "/", [{"text": "a"}])
    await dir_cache.set("alice@pam", "vol-b", "/other", [{"text": "b"}])
    await dir_cache.evict_missing("alice@pam", frozenset())
    assert await dir_cache.get("alice@pam", "vol-a", "/") is None
    assert await dir_cache.get("alice@pam", "vol-b", "/other") is None


async def test_evict_missing_never_touches_another_users_rows():
    """A revoked/pruned volume for one user must not evict a different
    user's own still-valid cache of the exact same volume - each user's
    own live archive list is what they're reconciled against."""
    await dir_cache.set("alice@pam", "vol", "/", [{"text": "alice's"}])
    await dir_cache.set("mallory@pam", "vol", "/", [{"text": "mallory's"}])
    await dir_cache.evict_missing("alice@pam", frozenset())
    assert await dir_cache.get("alice@pam", "vol", "/") is None
    assert await dir_cache.get("mallory@pam", "vol", "/") == [{"text": "mallory's"}]
