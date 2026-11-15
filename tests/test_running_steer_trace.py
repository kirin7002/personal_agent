"""Export only bounded acceptance evidence, never provider material or prose."""
from experiments.running_steer.plan_one import safe_arguments, safe_numeric_facts, trace


def test_receipt_export_excludes_configuration_and_untrusted_text():
    assert safe_arguments({"zone": "all", "temperature_c": 23, "api_key": "private", "prompt": "private"}) == {
        "zone": "all", "temperature_c": 23}
    assert safe_arguments({"zone": {"secret": "private"}, "action": ["private"]}) == {}
    assert safe_numeric_facts({"driver_temperature_c": 22, "text": "private", "source": {"api_key": "private"},
                               "equipment": {"air_suspension": False}}) == {
        "driver_temperature_c": 22, "air_suspension": False}


def test_event_trace_omits_prompt_headers_and_generated_prose():
    class Store:
        def task_events(self, tid):
            return [{"created": 12, "kind": "agent.step", "data": {
                "name": "call_tools", "task_revision": 1, "duration_ms": 3,
                "message": "private", "detail": "private", "headers": {"Authorization": "private"},
                "observation": {"text": "private"}, "arguments": {"api_key": "private"}}}]
    assert trace(Store(), {"id": "fixture", "created": 10}) == [{
        "at_ms": 2000, "kind": "agent.step", "task_revision": 1, "duration_ms": 3, "tool": "call_tools"}]
