from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace

ZERO_SHA = "0" * 40
_VALID_STATUS_PREFIXES = {"A", "C", "D", "M", "R", "T", "U", "X"}


@dataclass(frozen=True)
class NameStatusEntry:
    status: str
    paths: tuple[bytes, ...]


@dataclass(frozen=True)
class PrePushRecord:
    local_ref: str
    local_sha: str
    remote_ref: str
    remote_sha: str
    state: str
    rev_range: str | None
    reason: str | None = None
    fail_closed: bool = False


def parse_name_status_z(data: bytes) -> list[NameStatusEntry]:
    tokens = data.split(b"\0")
    if tokens and tokens[-1] == b"":
        tokens.pop()
    entries = []
    index = 0
    while index < len(tokens):
        status = tokens[index].decode("ascii")
        index += 1
        if not status or status[0] not in _VALID_STATUS_PREFIXES:
            raise ValueError(f"unsupported status: {status!r}")
        path_count = 2 if status[0] in {"R", "C"} else 1
        if index + path_count > len(tokens):
            if status[0] == "R":
                raise ValueError("R status requires two paths")
            if status[0] == "C":
                raise ValueError("C status requires two paths")
            raise ValueError(f"{status} status requires one path")
        paths = tuple(tokens[index : index + path_count])
        index += path_count
        entries.append(NameStatusEntry(status, paths))
    return entries


def is_zero_sha(value: str) -> bool:
    return value == ZERO_SHA


def parse_pre_push_stdin(text: str) -> list[PrePushRecord]:
    records = []
    for line in text.splitlines():
        if not line.strip():
            continue
        parts = line.split()
        if len(parts) != 4:
            raise ValueError(f"invalid pre-push record: {line!r}")
        local_ref, local_sha, remote_ref, remote_sha = parts
        if is_zero_sha(local_sha):
            state = "deleted_ref"
            rev_range = None
        elif is_zero_sha(remote_sha):
            state = "new_branch"
            rev_range = None
        elif local_sha == remote_sha:
            state = "no_updates"
            rev_range = None
        else:
            state = "existing_update"
            rev_range = f"{remote_sha}..{local_sha}"
        records.append(
            PrePushRecord(
                local_ref, local_sha, remote_ref, remote_sha, state, rev_range
            )
        )
    return records


def resolve_pre_push_record(
    record: PrePushRecord,
    merge_base_resolver: Callable[[str, str], str | None],
) -> PrePushRecord:
    if record.state != "new_branch":
        return record
    baseline = merge_base_resolver(record.local_ref, record.local_sha)
    if baseline:
        return replace(record, rev_range=f"{baseline}..{record.local_sha}")
    return replace(record, reason="new branch baseline unresolved", fail_closed=True)


def _git_stdout(
    repo_root: str,
    argv: Sequence[str],
    git_runner: Callable[..., subprocess.CompletedProcess[bytes]] = subprocess.run,
) -> str | None:
    try:
        completed = git_runner(
            list(argv),
            check=True,
            cwd=repo_root,
            shell=False,
            capture_output=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    output = completed.stdout.decode("utf-8", errors="replace").strip()
    return output or None


def make_merge_base_resolver(
    repo_root: str,
    git_runner: Callable[..., subprocess.CompletedProcess[bytes]] = subprocess.run,
) -> Callable[[str, str], str | None]:
    def resolve(local_ref: str, local_sha: str) -> str | None:
        branch_prefix = "refs/heads/"
        if local_ref.startswith(branch_prefix):
            branch_name = local_ref[len(branch_prefix) :]
        else:
            branch_name = local_ref
        upstream = _git_stdout(
            repo_root,
            ["git", "rev-parse", "--abbrev-ref", f"{branch_name}@{{upstream}}"],
            git_runner=git_runner,
        )
        if upstream:
            baseline = _git_stdout(
                repo_root,
                ["git", "merge-base", upstream, local_sha],
                git_runner=git_runner,
            )
            if baseline:
                return baseline
        origin_head = _git_stdout(
            repo_root,
            ["git", "symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD"],
            git_runner=git_runner,
        )
        if not origin_head or origin_head == upstream:
            return None
        return _git_stdout(
            repo_root,
            ["git", "merge-base", origin_head, local_sha],
            git_runner=git_runner,
        )

    return resolve


def _is_partial_zero_ref(value: str) -> bool:
    return len(value) == len(ZERO_SHA) and value.startswith("0") and value != ZERO_SHA


def resolve_managed_pre_push_range(env: Mapping[str, str]) -> str:
    from_ref = env.get("PRE_COMMIT_FROM_REF")
    to_ref = env.get("PRE_COMMIT_TO_REF")
    if from_ref is None and to_ref is None:
        raise ValueError("managed pre-push range unavailable")
    if from_ref is None or to_ref is None:
        raise ValueError(
            "managed pre-push requires both PRE_COMMIT_FROM_REF and PRE_COMMIT_TO_REF"
        )
    if not from_ref.strip() or not to_ref.strip():
        raise ValueError("managed pre-push range must use non-empty refs")
    if any(char.isspace() for char in from_ref) or any(
        char.isspace() for char in to_ref
    ):
        raise ValueError("managed pre-push range refs must not contain whitespace")
    if is_zero_sha(from_ref) or is_zero_sha(to_ref):
        raise ValueError("managed pre-push range unavailable")
    if _is_partial_zero_ref(from_ref) or _is_partial_zero_ref(to_ref):
        raise ValueError("managed pre-push range refs must be full commit SHAs")
    return f"{from_ref}..{to_ref}"


def git_diff_name_status(repo_root: str, rev_range: str) -> list[NameStatusEntry]:
    completed = subprocess.run(
        ["git", "diff", "--name-status", "-z", rev_range],
        check=True,
        cwd=repo_root,
        shell=False,
        capture_output=True,
    )
    return parse_name_status_z(completed.stdout)


def decode_paths(entries: Sequence[NameStatusEntry]) -> list[str]:
    decoded = []
    for entry in entries:
        raw_paths = entry.paths[-1:]
        if entry.status.startswith("R"):
            raw_paths = entry.paths
        for raw_path in raw_paths:
            decoded.append(os.fsdecode(raw_path))
    return decoded


def main(
    argv: Sequence[str] | None = None, stdin_text: str | None = None
) -> tuple[int, str]:
    args = list(sys.argv[1:] if argv is None else argv)
    stdin_payload = sys.stdin.read() if stdin_text is None else stdin_text
    if args == ["pre-push-records"]:
        records = parse_pre_push_stdin(stdin_payload)
        rendered = "\n".join(
            f"{record.state}:{record.rev_range or '-'}" for record in records
        )
        if argv is None:
            print(rendered)
        return 0, rendered
    return 2, "unsupported arguments"


if __name__ == "__main__":
    raise SystemExit(main()[0])
