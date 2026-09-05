"""Installed-code integrity: the mediator must not import code R can modify.

Pre-registration mapping: the CRITICAL TRUST RULE that the future mediator must not execute or
import security-relevant code from a directory writable by R -- covering both the entry point and
the Siphonophore package it imports.

This is the falsification case nobody writes down and everybody assumes away. If
`/opt/sipho-mediation-bypass/lib/siphonophore_core/execution_k8s.py` were writable by R, then R
would not need to bypass mediation at all: it would rewrite the mediator. The property under test
would be vacuous and the experiment would report a PASS.

Everything here is a pure function over stat results or an explicit path walk, so all of it is unit
-testable with temporary fixtures and none of it requires provisioning.
"""
from __future__ import annotations

import hashlib
import os
import stat as stat_module
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

MANIFEST_SUFFIXES = (".py", ".json", ".sh")
_CHUNK = 1 << 16


def file_sha256(path: str | Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(_CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build_manifest(root: str | Path, suffixes: Iterable[str] = MANIFEST_SUFFIXES) -> dict[str, str]:
    """`{relative_path: sha256}` for every file under `root` with a listed suffix, sorted for
    reproducibility. `__pycache__` is excluded: it is derived, it is rewritten on import, and
    including it would make the manifest non-deterministic."""
    root_path = Path(root).resolve()
    suffix_set = tuple(suffixes)
    manifest: dict[str, str] = {}
    for path in sorted(root_path.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        if "__pycache__" in path.parts:
            continue
        if suffix_set and path.suffix not in suffix_set:
            continue
        manifest[str(path.relative_to(root_path))] = file_sha256(path)
    return manifest


@dataclass(frozen=True)
class ManifestDiff:
    missing: tuple[str, ...] = ()
    unexpected: tuple[str, ...] = ()
    modified: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return not (self.missing or self.unexpected or self.modified)

    def to_dict(self) -> dict[str, Any]:
        return {"missing": list(self.missing), "unexpected": list(self.unexpected),
                "modified": list(self.modified), "ok": self.ok}


def verify_manifest(root: str | Path, expected: dict[str, str], suffixes: Iterable[str] = MANIFEST_SUFFIXES) -> ManifestDiff:
    actual = build_manifest(root, suffixes)
    missing = tuple(sorted(set(expected) - set(actual)))
    unexpected = tuple(sorted(set(actual) - set(expected)))
    modified = tuple(sorted(k for k in set(expected) & set(actual) if expected[k] != actual[k]))
    return ManifestDiff(missing=missing, unexpected=unexpected, modified=modified)


def mode_is_writable_by(*, mode: int, st_uid: int, st_gid: int, uid: int, gids: Iterable[int]) -> bool:
    """Pure DAC predicate: could the principal `(uid, gids)` write this inode?

    Deliberately ignores root's ability to write anything: uid 0 is excluded from the threat model
    (README.md, "What R may NOT assume"), and treating root as a writer would make every check on a
    normal system report a failure and train the experiment to ignore it.

    Deliberately ignores POSIX ACLs and immutable/append-only attributes -- neither is readable from
    `os.stat`. `verify_not_writable_by` records that limitation in its report rather than implying
    a completeness it does not have.
    """
    gid_set = set(gids)
    if st_uid == uid:
        return bool(mode & stat_module.S_IWUSR)
    if st_gid in gid_set:
        return bool(mode & stat_module.S_IWGRP)
    return bool(mode & stat_module.S_IWOTH)


@dataclass(frozen=True)
class PathIntegrityReport:
    path: str
    exists: bool
    is_symlink: bool = False
    owner_uid: int | None = None
    owner_gid: int | None = None
    mode_octal: str | None = None
    writable_by_subject: bool | None = None
    writable_ancestors: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()
    limitations: tuple[str, ...] = (
        "POSIX ACLs are not inspected (not visible via os.stat)",
        "file immutability/append-only attributes are not inspected",
        "root (uid 0) is excluded from the writer model by threat-model assumption",
    )

    @property
    def safe_for_privileged_import(self) -> bool:
        return bool(
            self.exists
            and not self.is_symlink
            and self.writable_by_subject is False
            and not self.writable_ancestors
            and not self.errors
        )

    def to_dict(self) -> dict[str, Any]:
        data = {k: v for k, v in self.__dict__.items()}
        data["writable_ancestors"] = list(self.writable_ancestors)
        data["errors"] = list(self.errors)
        data["limitations"] = list(self.limitations)
        data["safe_for_privileged_import"] = self.safe_for_privileged_import
        return data


def verify_not_writable_by(path: str | Path, *, uid: int, gids: Iterable[int]) -> PathIntegrityReport:
    """Check `path` AND every ancestor directory. An ancestor check is not pedantry: a writable
    parent directory lets R rename or replace the child regardless of the child's own mode, which is
    the classic way a "root-owned, 0644" file stops being trustworthy."""
    target = Path(path)
    gid_list = list(gids)
    errors: list[str] = []
    try:
        is_link = target.is_symlink()
        st = target.stat()
    except FileNotFoundError:
        return PathIntegrityReport(path=str(target), exists=False)
    except OSError as exc:
        return PathIntegrityReport(path=str(target), exists=False, errors=(f"stat failed: errno={exc.errno}",))

    writable = mode_is_writable_by(mode=st.st_mode, st_uid=st.st_uid, st_gid=st.st_gid, uid=uid, gids=gid_list)

    writable_ancestors: list[str] = []
    for ancestor in target.resolve().parents:
        try:
            ast_ = ancestor.stat()
        except OSError as exc:
            errors.append(f"stat of ancestor {ancestor} failed: errno={exc.errno}")
            continue
        if mode_is_writable_by(mode=ast_.st_mode, st_uid=ast_.st_uid, st_gid=ast_.st_gid, uid=uid, gids=gid_list):
            writable_ancestors.append(str(ancestor))

    return PathIntegrityReport(
        path=str(target),
        exists=True,
        is_symlink=is_link,
        owner_uid=st.st_uid,
        owner_gid=st.st_gid,
        mode_octal=oct(stat_module.S_IMODE(st.st_mode)),
        writable_by_subject=writable,
        writable_ancestors=tuple(writable_ancestors),
        errors=tuple(errors),
    )


@dataclass(frozen=True)
class ImportPathReport:
    entries: tuple[str, ...]
    writable_entries: tuple[str, ...]
    has_empty_entry: bool
    pythonpath_set: bool
    flags: dict[str, Any] = field(default_factory=dict)

    @property
    def clean(self) -> bool:
        return not self.writable_entries and not self.has_empty_entry and not self.pythonpath_set

    def to_dict(self) -> dict[str, Any]:
        return {"entries": list(self.entries), "writable_entries": list(self.writable_entries),
                "has_empty_entry": self.has_empty_entry, "pythonpath_set": self.pythonpath_set,
                "flags": self.flags, "clean": self.clean}


def import_path_report(sys_path: list[str], *, uid: int, gids: Iterable[int], environ: dict[str, str] | None = None,
                       flags: dict[str, Any] | None = None) -> ImportPathReport:
    """Is any entry on the running interpreter's `sys.path` writable by R?

    An empty entry (`''`) means the current working directory, which under `sudo -u M` is R's own
    cwd -- a direct R-controlled import source. `-I` (isolated mode) removes it and also ignores
    `PYTHONPATH`; PROVISIONING_SPEC.md requires the launcher to pass `-I` for exactly this reason,
    and this function is how that requirement is verified rather than assumed."""
    gid_list = list(gids)
    env = os.environ if environ is None else environ
    writable: list[str] = []
    has_empty = False
    for entry in sys_path:
        if entry == "":
            has_empty = True
            continue
        try:
            st = os.stat(entry)
        except OSError:
            continue
        if stat_module.S_ISDIR(st.st_mode) and mode_is_writable_by(
            mode=st.st_mode, st_uid=st.st_uid, st_gid=st.st_gid, uid=uid, gids=gid_list
        ):
            writable.append(entry)
    return ImportPathReport(
        entries=tuple(sys_path),
        writable_entries=tuple(writable),
        has_empty_entry=has_empty,
        pythonpath_set="PYTHONPATH" in env,
        flags=dict(flags or {}),
    )
