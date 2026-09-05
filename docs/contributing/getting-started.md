# Getting Started

This is the shortest supported setup for local development and the Python 3.14 CI-parity quality environment.

## Prerequisites

- Python 3.8+ for the historical firmware/helper-script baseline
- Python 3.14 for the CI-parity quality environment
- Go 1.25.5
- PlatformIO Core from the pioarduino `v6.1.19` archive
- `cmake` + `ctest`
- `ninja` (`ninja-build` on Debian/Ubuntu)
- `clang-format` 21+
- `shellcheck-py==0.11.0.1` via `requirements-quality.txt`
- `actionlint v1.7.12`
- `gitleaks v8.30.1`
- USB-C cable for device testing

## Bootstrap

```sh
git clone --recursive https://github.com/crosspoint-reader/crosspoint-reader
cd crosspoint-reader
python -m pip install --upgrade pip
python -m pip install --requirement requirements-quality.txt
python -m pip install --upgrade https://github.com/pioarduino/platformio-core/archive/refs/tags/v6.1.19.zip
```

`requirements-quality.txt` installs the pinned `shellcheck-py==0.11.0.1` executable used by CI.
Use Python 3.14 for this CI-parity quality environment even if your firmware/helper-script work stays on the historical Python 3.8+ baseline.

Install the non-Python quality tools before `python scripts/quality_check.py complete`.

### Linux example

```sh
sudo apt-get update
sudo apt-get install -y cmake ninja-build
# ctest ships with cmake packages on common Linux distros.
```

### actionlint and gitleaks

Install `actionlint v1.7.12` and `gitleaks v8.30.1` from their release archives, then add the binaries to your `PATH`.

### Windows

Use Git Bash on Windows for the repository commands in this guide after installing CMake/CTest, Ninja, `actionlint v1.7.12`, and `gitleaks v8.30.1` with your preferred package manager.

If you cloned without submodules:

```sh
git submodule update --init --recursive
```

## Hook migration

If you previously pointed Git at `.githooks`, remove that first:

```sh
git config --unset core.hooksPath
```

That command may return nonzero when `core.hooksPath` was never set.

Then install the shared prek hooks:

```sh
prek install --hook-type pre-commit --hook-type pre-push
```

## Daily commands

```sh
prek run --all-files
python scripts/quality_check.py complete
```

Use Git Bash on Windows for the same commands.

## What to read next

- [Development Workflow](./development-workflow.md)
- [Testing and Debugging](./testing-debugging.md)
- [Architecture Overview](./architecture.md)
