"""Persistent device credentials and one-use conversation pairing.

Only trusted server code may call issue with an existing conversation ID. All
other device IDs/session IDs must come from authenticate, not request bodies.
Reauthenticate long-lived connections after revocation/rebinding. Public device
records deliberately omit session IDs and credential hashes. No history moves.

Any bound owner/member may invite another device. The first active device in a
conversation is labelled owner; subsequent/joining devices are members. These
labels do not grant access to any other conversation. revoke is session scoped;
the parent determines any additional owner-only UI policy.
"""
import hashlib
import math
import secrets
import time
import uuid


PAIRING_TTL = 300
PAIRING_ATTEMPTS = 5
CODE_ISSUES = 5
_ALPHABET = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"


class PairingRateLimited(ValueError):
    def __init__(self, retry_after):
        super().__init__("Too many pairing attempts; try again later")
        self.retry_after = max(1, math.ceil(retry_after))


def _text(value, label, maximum=128):
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ValueError(f"{label} must be a nonempty string of at most {maximum} characters")
    return value.strip()


def _hash(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class DeviceSharing:
    def __init__(self, store, *, clock=None):
        self.store = store
        self.clock = clock or time.time
        with store.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS shared_devices(
                    id TEXT PRIMARY KEY, session_id TEXT NOT NULL,
                    name TEXT NOT NULL, kind TEXT NOT NULL, role TEXT NOT NULL,
                    token_hash TEXT NOT NULL UNIQUE, revoked_at REAL,
                    trusted INTEGER NOT NULL DEFAULT 0, paired INTEGER NOT NULL DEFAULT 0,
                    binding_version INTEGER NOT NULL DEFAULT 1,
                    created REAL NOT NULL, updated REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS shared_devices_session ON shared_devices(session_id);
                CREATE TABLE IF NOT EXISTS device_pairing_codes(
                    code_hash TEXT PRIMARY KEY, issuer_device_id TEXT NOT NULL,
                    session_id TEXT NOT NULL, binding_version INTEGER NOT NULL,
                    created REAL NOT NULL, expires_at REAL NOT NULL,
                    consumed_at REAL, redeemed_by TEXT
                );
                CREATE INDEX IF NOT EXISTS device_pairing_issuer ON device_pairing_codes(issuer_device_id);
                CREATE TABLE IF NOT EXISTS device_pairing_limits(
                    device_id TEXT NOT NULL, action TEXT NOT NULL,
                    window_start REAL NOT NULL, attempts INTEGER NOT NULL,
                    PRIMARY KEY(device_id,action)
                );
            """)
            columns = {row[1] for row in db.execute("PRAGMA table_info(shared_devices)")}
            for field in ("trusted", "paired"):
                if field not in columns:
                    db.execute(f"ALTER TABLE shared_devices ADD COLUMN {field} INTEGER NOT NULL DEFAULT 0")

    def _now(self):
        now = self.clock()
        if type(now) not in (int, float) or not math.isfinite(now) or now < 0:
            raise ValueError("clock must return a finite nonnegative epoch")
        return float(now)

    @staticmethod
    def _public(row):
        return {"id": row["id"], "name": row["name"], "kind": row["kind"], "role": row["role"],
                "revoked": row["revoked_at"] is not None, "revoked_at": row["revoked_at"],
                "trusted": bool(row["trusted"]), "paired": bool(row["paired"]),
                "binding_version": row["binding_version"], "created": row["created"], "updated": row["updated"]}

    @classmethod
    def _private(cls, row):
        return {**cls._public(row), "sid": row["session_id"], "session_id": row["session_id"]}

    def public(self, device):
        """Strip private routing fields from an authenticated/redeemed device."""
        return self._public(device)

    @staticmethod
    def _device(db, device_id):
        row = db.execute("SELECT * FROM shared_devices WHERE id=? AND revoked_at IS NULL", (device_id,)).fetchone()
        if row is None:
            raise PermissionError("Device is unavailable")
        return row

    def issue(self, sid, name, kind, *, trusted=False):
        if type(trusted) is not bool:
            raise ValueError("trusted must be a server-selected boolean")
        name, kind = _text(name, "name"), _text(kind, "kind", 32)
        token = secrets.token_urlsafe(32)
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if not db.execute("SELECT 1 FROM sessions WHERE id=?", (sid,)).fetchone():
                raise KeyError("Conversation not found")
            now, device_id = self._now(), uuid.uuid4().hex
            role = "member" if db.execute("SELECT 1 FROM shared_devices WHERE session_id=? AND revoked_at IS NULL", (sid,)).fetchone() else "owner"
            db.execute("""INSERT INTO shared_devices
                (id,session_id,name,kind,role,token_hash,trusted,created,updated) VALUES(?,?,?,?,?,?,?,?,?)""",
                       (device_id, sid, name, kind, role, _hash(token), int(trusted), now, now))
            return {"device": self._public(self._device(db, device_id)), "token": token}

    def authenticate(self, token):
        if not isinstance(token, str) or not 32 <= len(token) <= 256:
            raise PermissionError("Invalid device credential")
        with self.store.connect() as db:
            row = db.execute("SELECT * FROM shared_devices WHERE token_hash=? AND revoked_at IS NULL", (_hash(token),)).fetchone()
            if row is None:
                raise PermissionError("Invalid device credential")
            return self._private(row)

    def _limit(self, db, device_id, action, maximum, now):
        row = db.execute("SELECT * FROM device_pairing_limits WHERE device_id=? AND action=?", (device_id, action)).fetchone()
        if row and now < row["window_start"] + PAIRING_TTL:
            if row["attempts"] >= maximum:
                return PairingRateLimited(row["window_start"] + PAIRING_TTL - now)
            db.execute("UPDATE device_pairing_limits SET attempts=attempts+1 WHERE device_id=? AND action=?", (device_id, action))
        else:
            db.execute("""INSERT INTO device_pairing_limits VALUES(?,?,?,1)
                ON CONFLICT(device_id,action) DO UPDATE SET window_start=excluded.window_start,attempts=1""",
                       (device_id, action, now))
        return None

    def create_code(self, device_id):
        result = None
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            device, now = self._device(db, device_id), self._now()
            if not device["trusted"]:
                raise PermissionError("Pair this device with a trusted device first")
            error = self._limit(db, device_id, "issue", CODE_ISSUES, now)
            if error is None:
                raw = "".join(secrets.choice(_ALPHABET) for _ in range(12))
                code = "-".join(raw[i:i+4] for i in range(0, 12, 4))
                # A new invitation supersedes this device's earlier invitation.
                db.execute("UPDATE device_pairing_codes SET consumed_at=? WHERE issuer_device_id=? AND consumed_at IS NULL", (now, device_id))
                db.execute("INSERT INTO device_pairing_codes VALUES(?,?,?,?,?,?,NULL,NULL)",
                           (_hash(raw), device_id, device["session_id"], device["binding_version"], now, now + PAIRING_TTL))
                result = {"code": code, "expires_at": now + PAIRING_TTL, "ttl": PAIRING_TTL}
        if error:
            raise error
        return result

    def redeem(self, device_id, code):
        result = None
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            device, now = self._device(db, device_id), self._now()
            error = self._limit(db, device_id, "redeem", PAIRING_ATTEMPTS, now)
            if error is None:
                normalized = "".join(code.upper().split()).replace("-", "") if isinstance(code, str) and len(code) <= 128 else ""
                invitation = db.execute("""SELECT c.*,d.revoked_at AS issuer_revoked,d.session_id AS issuer_sid,
                    d.binding_version AS issuer_version,d.trusted AS issuer_trusted FROM device_pairing_codes c
                    JOIN shared_devices d ON d.id=c.issuer_device_id WHERE c.code_hash=?""", (_hash(normalized),)).fetchone()
                valid = (invitation and invitation["consumed_at"] is None and now < invitation["expires_at"]
                         and invitation["issuer_revoked"] is None
                         and invitation["issuer_trusted"]
                         and invitation["session_id"] == invitation["issuer_sid"]
                         and invitation["binding_version"] == invitation["issuer_version"]
                         and invitation["issuer_device_id"] != device_id)
                if not valid:
                    error = ValueError("Pairing code is invalid or expired")
                else:
                    db.execute("UPDATE device_pairing_codes SET consumed_at=?,redeemed_by=? WHERE code_hash=?", (now, device_id, invitation["code_hash"]))
                    role = device["role"] if device["session_id"] == invitation["session_id"] else "member"
                    db.execute("""UPDATE shared_devices SET session_id=?,role=?,trusted=1,paired=1,
                        binding_version=binding_version+1,updated=? WHERE id=?""", (invitation["session_id"], role, now, device_id))
                    db.execute("UPDATE device_pairing_codes SET consumed_at=? WHERE issuer_device_id=? AND consumed_at IS NULL", (now, device_id))
                    result = self._private(self._device(db, device_id))
        # Failed attempts must commit their counters; raising inside would undo them.
        if error:
            raise error
        return result

    def devices(self, sid):
        with self.store.connect() as db:
            return [self._public(row) for row in db.execute("SELECT * FROM shared_devices WHERE session_id=? ORDER BY created,id", (sid,))]

    def revoke(self, sid, target_id):
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM shared_devices WHERE id=? AND session_id=?", (target_id, sid)).fetchone()
            if row is None:
                raise KeyError("Device not found in conversation")
            if row["revoked_at"] is None:
                now = self._now()
                db.execute("UPDATE shared_devices SET revoked_at=?,updated=?,binding_version=binding_version+1 WHERE id=?", (now, now, target_id))
                db.execute("UPDATE device_pairing_codes SET consumed_at=? WHERE issuer_device_id=? AND consumed_at IS NULL", (now, target_id))
                row = db.execute("SELECT * FROM shared_devices WHERE id=?", (target_id,)).fetchone()
            return self._public(row)
