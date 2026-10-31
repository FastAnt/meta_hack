# Pulseboard

Pulseboard is a small team-dashboard service: it collects project
metrics, stores them locally and shows a live status in the terminal.

Demo project for Agent-Aware Terminal: it is a convenient place to
watch an agent move between components while it works.

## Components

- `api` — HTTP-ish layer: turns requests into engine calls and back.
- `core` — engine and models: builds dashboards and computes the metrics summary.
- `storage` — local dashboard storage in a JSON file.
- `ui` — terminal UI: prints the dashboard to the console.
- `tests` — engine tests: metrics summary and the empty dashboard.

## Run

```bash
python -m ui.cli
python -m pytest tests/
```
