# Testing and Debugging

Use the shared quality runner first, then move to device-specific debugging on hardware.

## Quick checks

```sh
prek run --all-files
```

`format-changed` is the only mutating shared operation and it is Python-only. C/C++ formatting is handled by prek's staged `clang-format` hook.

## Full local verification

```sh
python scripts/quality_check.py complete
```

This matches the broad CI quality gate used before `Test Status` can pass on protected `develop`. Install the prerequisites from [Getting Started](./getting-started.md) first, especially `cmake`/`ctest`, `ninja`, `shellcheck-py==0.11.0.1` from `requirements-quality.txt`, `actionlint v1.7.12`, and `gitleaks v8.30.1`.

The firmware/helper-script baseline remains Python 3.8+, but the shared quality runner itself is documented as a Python 3.14 CI-parity quality environment.

## Hook behavior

- Installed prek hooks work in Linux shells and Windows Git Bash.
- Local hooks can be bypassed.
- CI is the required enforcement layer.

## Flash and monitor

```sh
pio run --target upload
pio device monitor
```

Optional enhanced monitor:

```sh
python3 -m pip install pyserial colorama matplotlib
python3 scripts/debugging_monitor.py
```
