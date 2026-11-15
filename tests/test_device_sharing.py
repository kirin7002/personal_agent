"""Credential/pairing tests use real SQLite and no external services."""
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from app.device_sharing import DeviceSharing, PairingRateLimited
from app.store import Store


class Clock:
    now = 1000.0

    def __call__(self):
        return self.now


@pytest.fixture
def env(tmp_path):
    store, clock = Store(tmp_path / "sharing.db"), Clock()
    sharing = DeviceSharing(store, clock=clock)
    sid, phone_sid = store.create_session(), store.create_session()
    desktop = sharing.issue(sid, "Desktop", "desktop", trusted=True)
    phone = sharing.issue(phone_sid, "Phone", "phone")
    return store, sharing, clock, sid, phone_sid, desktop, phone


def race(*actions):
    barrier = Barrier(len(actions))

    def run(action):
        barrier.wait(timeout=5)
        try:
            return action()
        except (ValueError, KeyError, PermissionError) as exc:
            return exc

    with ThreadPoolExecutor(max_workers=len(actions)) as pool:
        futures = [pool.submit(run, action) for action in actions]
        return [f.result(timeout=15) for f in futures]


def test_defaults_auth_public_and_no_plaintext_secrets(env):
    store, sharing, _, sid, phone_sid, desktop, phone = env
    assert desktop["device"]["trusted"] and not desktop["device"]["paired"]
    assert not phone["device"]["trusted"] and not phone["device"]["paired"]
    private = sharing.authenticate(desktop["token"])
    assert private["sid"] == private["session_id"] == sid
    assert sharing.public(private) == desktop["device"]
    assert sharing.authenticate(phone["token"])["sid"] == phone_sid
    code = sharing.create_code(desktop["device"]["id"])
    with store.connect() as db:
        dump = "\n".join(db.iterdump())
        hashes = [r[0] for r in db.execute("SELECT token_hash FROM shared_devices")]
    assert desktop["token"] not in dump and phone["token"] not in dump
    assert code["code"] not in dump and code["code"].replace("-", "") not in dump
    assert all(len(value) == 64 for value in hashes)
    for public in sharing.devices(sid) + sharing.devices(phone_sid):
        assert not {"token", "token_hash", "session_id", "sid"} & public.keys()
    with pytest.raises(PermissionError):
        sharing.create_code(phone["device"]["id"])
    for token in (None, "", "wrong", "a" * 43, desktop["token"] + "x"):
        with pytest.raises(PermissionError):
            sharing.authenticate(token)


def test_pairing_rebinds_same_device_without_moving_old_history(env):
    store, sharing, _, sid, phone_sid, desktop, phone = env
    old, _ = store.create_task(phone_sid, "old private phone history", "old")
    existing, _ = store.create_task(sid, "desktop history", "desktop")
    invite = sharing.create_code(desktop["device"]["id"])
    joined = sharing.redeem(phone["device"]["id"], invite["code"].lower())
    assert joined["id"] == phone["device"]["id"] and joined["sid"] == sid
    assert joined["trusted"] and joined["paired"] and joined["role"] == "member"
    assert joined["binding_version"] == phone["device"]["binding_version"] + 1
    assert sharing.authenticate(phone["token"]) == joined
    assert sharing.devices(phone_sid) == []
    assert store.task(old["id"])["session_id"] == phone_sid
    assert store.tasks(sid) == [existing]
    with pytest.raises(KeyError):
        store.task(old["id"], sid)
    # A paired member can share back to a new desktop: both directions work.
    second = sharing.issue(store.create_session(), "Second desktop", "desktop")
    back_code = sharing.create_code(joined["id"])
    assert sharing.redeem(second["device"]["id"], back_code["code"])["sid"] == sid
    with pytest.raises(ValueError):
        sharing.redeem(second["device"]["id"], invite["code"])


def test_expiry_exact_boundary_and_new_invite_invalidates_previous(env):
    _, sharing, clock, _, _, desktop, phone = env
    first = sharing.create_code(desktop["device"]["id"])
    second = sharing.create_code(desktop["device"]["id"])
    assert second["ttl"] == 300 and second["expires_at"] == clock.now + 300
    with pytest.raises(ValueError):
        sharing.redeem(phone["device"]["id"], first["code"])
    clock.now = second["expires_at"]
    with pytest.raises(ValueError):
        sharing.redeem(phone["device"]["id"], second["code"])


def test_rate_limits_failed_attempts_persist_and_reset(env):
    store, sharing, clock, _, _, desktop, phone = env
    for _ in range(5):
        with pytest.raises(ValueError, match="invalid"):
            sharing.redeem(phone["device"]["id"], "invalid")
    # A new service object must not reset attempt counters.
    reopened = DeviceSharing(store, clock=clock)
    valid = reopened.create_code(desktop["device"]["id"])
    with pytest.raises(PairingRateLimited) as error:
        reopened.redeem(phone["device"]["id"], valid["code"])
    assert error.value.retry_after == 300
    clock.now += 300
    fresh = reopened.create_code(desktop["device"]["id"])
    assert reopened.redeem(phone["device"]["id"], fresh["code"])["trusted"]


def test_code_issuance_rate_limit_and_self_redemption(env):
    _, sharing, clock, _, _, desktop, _ = env
    for _ in range(5):
        code = sharing.create_code(desktop["device"]["id"])
    with pytest.raises(PairingRateLimited):
        sharing.create_code(desktop["device"]["id"])
    with pytest.raises(ValueError):
        sharing.redeem(desktop["device"]["id"], code["code"])
    clock.now += 300
    assert sharing.create_code(desktop["device"]["id"])["expires_at"] == clock.now + 300


def test_revoke_scoped_idempotent_and_invalidates_tokens_and_invites(env):
    _, sharing, _, sid, phone_sid, desktop, phone = env
    code = sharing.create_code(desktop["device"]["id"])
    with pytest.raises(KeyError):
        sharing.revoke(phone_sid, desktop["device"]["id"])
    revoked = sharing.revoke(sid, desktop["device"]["id"])
    assert revoked["revoked"]
    assert sharing.revoke(sid, desktop["device"]["id"]) == revoked
    with pytest.raises(PermissionError):
        sharing.authenticate(desktop["token"])
    with pytest.raises(PermissionError):
        sharing.create_code(desktop["device"]["id"])
    with pytest.raises(ValueError):
        sharing.redeem(phone["device"]["id"], code["code"])
    assert not sharing.authenticate(phone["token"])["trusted"]


def test_concurrent_redemption_only_one_winner(env):
    store, sharing, _, sid, _, desktop, phone = env
    other = sharing.issue(store.create_session(), "Other", "phone")
    code = sharing.create_code(desktop["device"]["id"])["code"]
    results = race(lambda: sharing.redeem(phone["device"]["id"], code),
                   lambda: sharing.redeem(other["device"]["id"], code))
    assert sum(isinstance(r, dict) for r in results) == 1
    assert sum(sharing.authenticate(d["token"])["sid"] == sid for d in (phone, other)) == 1


def test_concurrent_attempt_limit_is_atomic(env):
    store, sharing, _, _, _, _, phone = env
    results = race(*(lambda: sharing.redeem(phone["device"]["id"], "wrong") for _ in range(8)))
    assert sum(isinstance(r, PairingRateLimited) for r in results) == 3
    with store.connect() as db:
        assert db.execute("SELECT attempts FROM device_pairing_limits WHERE device_id=? AND action='redeem'", (phone["device"]["id"],)).fetchone()[0] == 5


def test_revoke_redeemer_race_never_reactivates_device(env):
    _, sharing, _, _, phone_sid, desktop, phone = env
    code = sharing.create_code(desktop["device"]["id"])["code"]
    # Revocation with the original sid may lose to rebind; then the parent must
    # authenticate/reload and revoke under the new binding, never another session.
    results = race(lambda: sharing.redeem(phone["device"]["id"], code),
                   lambda: sharing.revoke(phone_sid, phone["device"]["id"]))
    if isinstance(results[1], dict):
        with pytest.raises(PermissionError):
            sharing.authenticate(phone["token"])
        assert isinstance(results[0], PermissionError)
    else:
        assert isinstance(results[1], KeyError)
        assert results[0]["paired"]


def test_rebinding_invalidates_invites_created_under_previous_binding(env):
    store, sharing, _, _, _, desktop, phone = env
    trusted_phone = sharing.issue(store.create_session(), "Trusted phone", "phone", trusted=True)
    old_code = sharing.create_code(trusted_phone["device"]["id"])["code"]
    code = sharing.create_code(desktop["device"]["id"])["code"]
    sharing.redeem(trusted_phone["device"]["id"], code)
    with pytest.raises(ValueError):
        sharing.redeem(phone["device"]["id"], old_code)


@pytest.mark.parametrize("name,kind", [("", "phone"), ("ok", ""), (None, "phone"), ("x" * 129, "phone")])
def test_invalid_device_metadata(env, name, kind):
    _, sharing, _, sid, _, _, _ = env
    with pytest.raises(ValueError):
        sharing.issue(sid, name, kind)
