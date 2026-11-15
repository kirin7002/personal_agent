import httpx

from app.config import Settings
from app.hermes import HermesClient


async def test_admission_throttle_retries_same_request_identity():
    requests = []
    def handler(request):
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(429, headers={"Retry-After": "0.2"}, json={"error": "concurrency_limit"})
        return httpx.Response(202, json={"run_id": "run_throttled"})
    transport = httpx.AsyncClient(base_url="http://hermes", transport=httpx.MockTransport(handler))
    client = HermesClient(Settings(hermes_key="test"), transport)
    task = {"id": "task-test", "revision": 0, "goal": "demo", "history": [], "calls": []}
    assert await client.start(task, "generic protocol") == "run_throttled"
    assert requests[0].content == requests[1].content
    assert requests[0].headers["Idempotency-Key"] == requests[1].headers["Idempotency-Key"]
    await client.close()
