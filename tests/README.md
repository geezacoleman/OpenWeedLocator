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

The CI runner has no MQTT broker, no `config/CONTROLLER.ini` (gitignored) and
no ultralytics. The root conftest refuses real MQTT connections so a local
mosquitto can't hide a broken test; for the other two, mock the model
(`@patch('utils.greenongreen.YOLO')`) and never read the repo's
`CONTROLLER.ini` from a test.

## Tests never write to anything real

Enforced, not a convention:

- `tests/conftest.py` sets `OWL_HOME` to a temp directory before any
  production import. Everything the OWL writes at run time (`config/`,
  `logs/`, `models/`, `downloads/`, `uploads/`, `agent/widgets|sessions`)
  resolves through `utils/paths.home_dir()`, so it lands there. The temp home
  is seeded with the shipped `config/` and `models/` files only: your own
  `CONTROLLER.ini` and active-config pointer never reach a test.
- `tests/_write_guard.py` is an audit hook on every file open-for-write,
  rename, delete, mkdir, chmod and truncate in the test process. A target
  outside the temp directory is refused and the test fails, naming the file,
  even when the code under test swallows the error.
- `/etc/hosts` and the avahi service file are repointed per test.

Until 3.27.3 a run rewrote `config/active_config.txt` (to a 4-relay fixture
the OWL then booted with), appended a test hostname to `/etc/hosts` when run
as root, and created `logs/`, `downloads/`, `agent/` and `/home/logs`.

Need a private home for one test (to check what lands in `models/`)? Use the
`owl_home` fixture. New production code that writes a file: build the path
with `home_dir(...)`, never from `__file__`. To see what a run would have
written instead of failing: `OWL_TEST_WRITE_GUARD=report pytest ...` then
read `$TMPDIR/owl_test_writes.log`.

Child processes are not seen by the hook; do not shell out to something that
writes outside `tmp_path`.

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
