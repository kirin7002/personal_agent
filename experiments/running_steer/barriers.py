"""Cross-process timing barriers. Only this experiment's table is mutated."""
import asyncio
import time


WINDOWS = ("during_model", "before_control", "before_finish")


def setup(store):
    with store.connect() as db:
        db.execute("""CREATE TABLE IF NOT EXISTS running_steer_barriers (
            task_id TEXT PRIMARY KEY, window TEXT NOT NULL,
            reached REAL, released REAL, provider_started REAL, provider_returned REAL
        )""")


def register(store, tid, window):
    if window not in WINDOWS:
        raise ValueError("Unknown validation window")
    with store.connect() as db:
        db.execute("INSERT INTO running_steer_barriers(task_id,window) VALUES(?,?)", (tid, window))


def state(store, tid):
    with store.connect() as db:
        row = db.execute("SELECT * FROM running_steer_barriers WHERE task_id=?", (tid,)).fetchone()
    return dict(row) if row else None


def mark(store, tid, column):
    if column not in {"reached", "released", "provider_started", "provider_returned"}:
        raise ValueError("Unknown timing field")
    with store.connect() as db:
        db.execute(f"UPDATE running_steer_barriers SET {column}=COALESCE({column},?) WHERE task_id=?", (time.time(), tid))


async def wait_reached(store, tid, timeout=90):
    async with asyncio.timeout(timeout):
        while not state(store, tid)["reached"]:
            task = store.task(tid)
            if task["status"] != "running":
                raise RuntimeError("Task ended before validation barrier")
            await asyncio.sleep(.02)


async def before_tool(store, name, arguments, timeout=40):
    tid = arguments.get("task_id", "")
    row = state(store, tid)
    expected = {"before_control": "call_tools", "before_finish": "finish_task"}
    if not row or row["reached"] or expected.get(row["window"]) != name:
        return
    mark(store, tid, "reached")
    async with asyncio.timeout(timeout):
        while not state(store, tid)["released"]:
            await asyncio.sleep(.02)


def wait_release(store, tid, timeout=40):
    deadline = time.monotonic() + timeout
    while not state(store, tid)["released"]:
        if time.monotonic() >= deadline:
            raise TimeoutError("Validation barrier was not released")
        time.sleep(.02)
