"""The installed mediator entry point. Reads one request on stdin, writes one response on stdout.

Invoked by the fixed-shape launcher specified in PROVISIONING_SPEC.md:

    sudo -n /usr/local/libexec/sipho-mediate        # no arguments, ever

which execs the pinned interpreter in isolated mode:

    exec /opt/sipho-mediation-bypass/venv/bin/python -I -m sipho_bypass.mediator ...

`-I` (isolated) implies `-E` (ignore PYTHONPATH) and `-s` (no user site-packages), and on
Python >= 3.11 also `-P` (do not prepend cwd/script dir to sys.path). Under `sudo -u M` the cwd is
still R's, so without `-I` an `''` entry on `sys.path` would be an R-controlled import source. See
integrity.import_path_report(), which verifies this rather than assuming it.

ARGV DISCIPLINE: any argument at all is refused. The sudoers grant is written to permit no
arguments (the SH-08 `""` convention already used by `siphonophore-spawn` and re-validated by
Stage 2), so an argument arriving here means either the grant is wrong or something else invoked
this binary -- both are fixture failures worth failing loudly on, not conditions to tolerate.

Exit codes are about the MECHANISM, never about the science: 0 means "a well-formed response was
produced" (which includes a normalized rejection of R's request); non-zero means the mediator
itself could not run.
"""
from __future__ import annotations

import json
import sys

from .. import redaction
from . import hardening, service
from .config import DEFAULT_CONFIG_PATH, ConfigError, load_config

EXIT_OK = 0
EXIT_BAD_INVOCATION = 64
EXIT_CONFIG = 65
EXIT_INTERNAL = 70
EXIT_UNSAFE_DEPLOYMENT = 71


def _emit(obj: dict) -> None:
    # Even the mediator's own output goes through the secret scan. If a future change ever routed
    # credential-shaped content into the response, this fails the invocation instead of disclosing
    # it -- falsification case F-10.
    sys.stdout.write(redaction.safe_json_dumps(obj, indent=None, sort_keys=True))
    sys.stdout.write("\n")
    sys.stdout.flush()


def main(argv: list[str] | None = None, stdin_bytes: bytes | None = None,
         config_path: str = DEFAULT_CONFIG_PATH) -> int:
    args = sys.argv[1:] if argv is None else argv
    if args:
        sys.stderr.write("sipho-mediate accepts no arguments\n")
        return EXIT_BAD_INVOCATION

    try:
        config = load_config(config_path)
    except ConfigError as exc:
        # Deliberately terse: the message can contain M-side paths, and stderr reaches R.
        sys.stderr.write("mediator configuration unavailable\n")
        sys.stderr.write(f"detail-class: {type(exc).__name__}\n")
        return EXIT_CONFIG

    if config.enforce_deployment_hardening:
        findings = hardening.deployment_findings(config)
        if findings:
            # A misconfigured fixture must be INCONCLUSIVE, never a quietly weaker run.
            sys.stderr.write("mediator refuses to start: deployment hardening preconditions unmet\n")
            for finding in findings:
                sys.stderr.write(f"  - {finding}\n")
            return EXIT_UNSAFE_DEPLOYMENT
    hardening.harden_environment(config)

    raw = sys.stdin.buffer.read() if stdin_bytes is None else stdin_bytes

    try:
        outcome = service.handle_request(raw, config)
    except Exception:  # noqa: BLE001 -- a traceback on stderr would reach R and could name M paths
        sys.stderr.write("mediator internal error\n")
        return EXIT_INTERNAL

    try:
        _emit(outcome.response.to_dict())
    except redaction.SecretLeakError:
        sys.stderr.write("mediator refused to emit a response that failed its own secret scan\n")
        return EXIT_INTERNAL

    _write_m_side_record(config, outcome)
    return EXIT_OK


def _write_m_side_record(config, outcome) -> None:  # noqa: ANN001
    """Best-effort, M-owned, never returned to R. Holds the full exception text that the normalized
    response deliberately withholds. A failure to write must not fail the request: this is M's
    bookkeeping, not part of the mediation boundary."""
    if not config.evidence_dir:
        return
    try:
        from pathlib import Path

        directory = Path(config.evidence_dir)
        directory.mkdir(parents=True, exist_ok=True)
        intent_id = outcome.response.intent_id or "unlabelled"
        target = directory / f"{intent_id}.json"
        # No redaction pass here, by design: this file is M-owned and R cannot read it, and its
        # entire purpose is to retain the detail the response drops.
        target.write_text(json.dumps(outcome.m_side, indent=2, sort_keys=True, default=str))
        target.chmod(0o600)
    except Exception:  # noqa: BLE001
        return


if __name__ == "__main__":
    raise SystemExit(main())
