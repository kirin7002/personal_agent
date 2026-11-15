# Production running-task revision validation

Run from the repository root:

```sh
.venv/bin/python -m pytest -q tests/test_running_steer_validation.py
.venv/bin/python scripts/validate_running_steer.py --qwen-live \
  --output test-results/running-steer-validation-production.json
```

The live command requires loopback socket and provider network access. It starts
an application and Hermes gateway on fresh loopback ports, with a new database
and private Hermes home under `.runtime/running-steer-*`. Model configuration is
copied unchanged. Credentials and gateway logs stay in private runtime files;
the JSON report contains only bounded outcome telemetry. Existing configuration
files and running services are not edited or restarted. Installed Hermes Python
dependencies are reused, with no installation or upgrade.

The harness uses the actual `TaskManager`, `CapabilityTools`, production MCP
schemas and production completion implementation. It never imports ProbeManager,
ProbeTools or probe revision/receipt guards. The completion source is loaded by
filename because the gateway owns the `hermes_plugins` Python namespace.

Timing instrumentation writes only an experiment table and audit events:

- `during_model`: the correction must occur between provider invocation and
  provider return; holding a returned response does not count as in-flight proof.
- `before_control`: hold immediately before dispatch to `CapabilityTools.call`.
- `before_finish`: retain the first 23-degree receipt and hold the old finish call.

Each window runs through the production HTTP revise route and through the actual
VoiceBridge tool callback. The six cases use a real Hermes/model connection but
capture the VoiceBridge network sinks. They do **not** constitute live Qwen tests.

`--qwen-live` adds a seventh case using `VoiceBridge.run` and its real upstream
Qwen connection. A queue supplies text input (23 degrees, then “改成21度”); Qwen
chooses the task tools and returns audio. This covers real Qwen tool selection,
Hermes steering and spoken-notification acknowledgement. It does not test a
microphone, speech recognition, or physical speaker playback. All vehicle results
remain simulated previews, as in the production application.

The deterministic tests cover six timing/entry combinations, stale revisions,
stale values, changed zones, superseded receipts, same-run monitoring, HTTP
projection, callback deduplication, ambiguous pauses, session isolation, change
CAS, terminal rejection, production completion binding, queued notice invalidation
and suppression of late audio/transcript deltas.

## Verified run

`test-results/running-steer-validation-production.json`: seven of seven live
cases passed. Each retained one task/run and one dispatch, revision 1, a final
all-zone 21-degree preview, no stale accepted controls after correction, one
completion notification and one production confirmation skip. Both provider
windows recorded correction inside the provider invocation. Both finish windows
retained one old receipt before correction and excluded it from final output.
The actual Qwen case returned 34 audio chunks and acknowledged the current result.
The isolated ports for that run were 61882 (Hermes) and 61883 (application).

The earlier `test-results/running-steer-validation.json` is a failed development
run, retained as evidence: the wrapper initially encountered the gateway's Python
namespace collision, and the longer Qwen correction required clarification and a
second dispatch. It must not be counted as acceptance success.
