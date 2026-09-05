# Development Workflow

Use the same quality commands locally that CI runs on `develop`.
The firmware/helper-script baseline remains Python 3.8+, but the CI-parity quality environment in these steps is Python 3.14.

## Quick path

1. Clone the repo and install the shared quality tools from [Getting Started](./getting-started.md), including `cmake`/`ctest`, `ninja`, `shellcheck-py==0.11.0.1` from `requirements-quality.txt`, `actionlint v1.7.12`, and `gitleaks v8.30.1`.
2. Migrate old hook configuration, then install prek hooks:
   - `git config --unset core.hooksPath` (`git` may return nonzero if it was not set)
   - `prek install --hook-type pre-commit --hook-type pre-push`
3. Implement a focused change.
4. Run `prek run --all-files` before opening a PR.
5. Run `python scripts/quality_check.py complete` when you want the full local CI-equivalent pass.

## Local enforcement

- The `pre-commit` hook is the quick mutating pass.
- The installed `pre-push` hook chooses the right shared checks for your changed paths.
- Local hooks can be bypassed, so they are a convenience layer, not final enforcement.
- protected develop requires the CI job named `Test Status`, which is the real merge gate.

## Commands

| Goal | Command |
|---|---|
| Install hooks | `prek install --hook-type pre-commit --hook-type pre-push` |
| Quick pre-commit sweep | `prek run --all-files` |
| Full local verification | `python scripts/quality_check.py complete` |

## PR expectations

- Branch from `develop`.
- Keep each PR focused.
- Describe reproduction and verification steps.
- Follow [GOVERNANCE.md](../../GOVERNANCE.md) for review etiquette.
