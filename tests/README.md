# OWL tests

## Running

```bash
pip install pytest pytest-xdist
pytest tests/ -n auto                 # everything (what CI runs)
pytest tests/owl -n auto              # the real detection loop only
git config core.hooksPath .githooks   # once per clone: fast tier before every push
```

| Tier | What | Where it runs |
|------|------|---------------|
| fast | `tests/unit tests/owl tests/mqtt tests/contract`, minus `slow` | `.githooks/pre-push` |
| full | everything except `benchmark` | GitHub Actions on every push and PR (Python 3.11 and 3.13), blocking |
| slow | marked `@pytest.mark.slow` (ultralytics/torch imports) | CI only |
| benchmark | `pytest -m benchmark`: timings, machine-specific | by hand |
| e2e | Playwright scripts in `tests/e2e/`: `python tests/e2e/<file>.py` | by hand |
| manual | demos and on-Pi smoke scripts in `tests/manual/` | by hand, on hardware |

A red CI run means no field deploy. Do not add an `--ignore` to make it green:
fix the test or the code.

## Layout

| Folder | Covers | Fakes |
|--------|--------|-------|
| `owl/` | A **real `Owl`** and real frames through `hoot()`: the deployment matrix (controller type x dashboard x recording), faults and `[System] on_error`, spray timing | camera, broker (unreachable port), switch-panel process (thread) |
| `unit/` | One module at a time: algorithms, config, GPS, relays, storage, image recorder, tracking | whatever the module talks to |
| `mqtt/` | `OWLMQTTPublisher` command handlers and state, `DashMQTTSubscriber`, cloud feed | a `MagicMock` Owl (`mock_owl`) and MQTT client |
| `controllers/standalone/` | The single-OWL dashboard (Flask routes, settings, painter) | MQTT subscriber |
| `controllers/networked/` | The in-cab controller: routes, collect screen, fleet, downloads, actuation | `CentralController` |
| `controllers/setup/` | First-boot setup API and trust chain | nmcli, systemctl |
| `contract/` | Producer vs consumer: every command a dashboard sends has an OWL handler | nothing, reads source |
| `agent/` | The AI agent and its tools | LLM provider |
| `integration/` | First-boot app over real HTTP (also the owl-app Playwright backend: keep `serve_setup_app.py` here) | nmcli |

## Where a new test goes

- **Anything that depends on the mode the OWL runs in** (controller type,
  dashboard on/off, recording, algorithm, GPS): `tests/owl/`, on a real Owl.
  A `MagicMock` Owl invents any attribute you ask for, and a hand-built
  namespace only has what you put in it, so neither can notice an attribute
  that is missing in one mode. That is how v3.17 shipped an image-saving
  failure on every unit without a switch panel.
- `owl_factory` (tests/owl/conftest.py) **fails the test if the OWL logs an
  ERROR**. Mark a test that expects one with `@pytest.mark.allow_errors`.
- A new MQTT command: a handler test in `mqtt/`; `contract/` then checks the
  sender side automatically.
- Source-text (`ast`/string) checks are fine for structure (for example "no
  first-boot code in the detection loop"), not for behaviour.

Shared fixtures: `tests/conftest.py` (`mock_owl`, `mqtt_publisher`,
`standalone_test_client`, `networked_test_client`, the power-command guard)
and `tests/owl/conftest.py` + `tests/owl/harness.py` (real Owl).
