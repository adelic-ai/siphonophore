"""007 -- a genuine Belnap contradiction (T/F) and unreported-activity (F/T) case.

See `lab/007-belnap-reconciliation.md` for the hypothesis (with explicit null), method, and
analysis. This script runs the experiment and writes results under `out/007/`.

DESIGN.md SS3 describes four-valued reconciliation between self-report (the agent's own, untrusted
narrative) and ground truth (established by an observer outside the agent's trust domain):

    claimed  observed
      T         T      -> corroborated
      T         F      -> contradiction / unsupported claim
      F         T      -> unreported activity
      F         F      -> no evidence

Every experiment in this lab so far (001-006) has only ever produced the T/T corroborated case --
self-report and ground truth always happened to agree, because nothing before this experiment tried
to make them disagree. This experiment does two things neither prior experiment did:

1. Implements `reconcile()` -- the actual (claimed, observed) -> Belnap-value comparison -- as real
   code. Per DESIGN.md's own account this doesn't exist anywhere in the repo yet.
2. Constructs a REAL scenario, using a real delegated sub-agent running under its own provisioned
   uid (the same uid+cgroup shape as 004/005/006), where self-report and ground truth genuinely
   disagree: the sub-agent's self-report actively lies about one file's content (a genuine T/F
   contradiction), and the sub-agent performs an extra, undisclosed write that its self-report never
   mentions at all (a genuine F/T unreported activity) -- alongside a genuinely corroborated write
   (T/T) and a path that is neither claimed nor performed (F/F), so the classifier is shown to
   distinguish all four cases from one real execution's data, not just the two being newly
   introduced.

Ground truth is collected the way DESIGN.md SS5 describes an external observer working: the root
parent reads the sub-agent's output directory directly, AFTER independently confirming (via
`proc.wait()`) that the child has exited -- never by trusting, deriving from, or even looking at
the self-report first. The self-report itself is read from a file the PARENT opened and the CHILD
inherited as an already-open stdout fd (the file-redirect pattern validated in 006, chosen instead
of `stdout=PIPE`+`communicate()` specifically because 006 found that combination unsafe under this
experiment's own earlier real concurrent load -- not needed for correctness here, since this
experiment dispatches a single delegation, but reused for consistency and because there is no
reason to reintroduce a known-hazardous pattern).

Built entirely fresh -- no import, copy, or reuse of any code from 004/005/006 (each of which is
itself self-contained) or from any deleted v1 code. Needs real root on real Linux for the same
reasons 004-006 do; refuses cleanly everywhere else.

Run (from macOS, will refuse -- confirms the refusal path)::

    cd /Users/shunhonda/dev/siphonophore
    python3 lab/007_belnap_reconciliation.py

Run for real, as root, on colima::

    colima ssh -- bash -c "cd /Users/shunhonda/dev/siphonophore && sudo python3 lab/007_belnap_reconciliation.py"
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import pwd
import secrets
import shutil
import subprocess
import sys
import tempfile
import uuid
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

OUT = Path(__file__).parent / "out" / "007"

# New uid range and cgroup root, distinct from 004 (62000s), 005 (63000s), 006 (64000s).
UID_RANGE_START = 65100
UID_RANGE_END = 65199
CGROUP_ROOT = Path("/sys/fs/cgroup/siphonophore-exp007")


def require_real_root_linux() -> None:
    if sys.platform != "linux":
        sys.stderr.write(
            "REFUSED: this experiment requires real Linux (uid/cgroup provisioning is "
            f"Linux-specific). Detected sys.platform={sys.platform!r}. Run it on colima:\n"
            "  colima ssh -- bash -c \"cd /Users/shunhonda/dev/siphonophore && "
            "sudo python3 lab/007_belnap_reconciliation.py\"\n"
        )
        sys.exit(1)
    if os.geteuid() != 0:
        sys.stderr.write(
            f"REFUSED: this experiment requires real root. Detected euid={os.geteuid()}. Re-run with sudo.\n"
        )
        sys.exit(1)
    if not Path("/sys/fs/cgroup/cgroup.controllers").exists():
        sys.stderr.write("REFUSED: cgroup v2 unified hierarchy not detected.\n")
        sys.exit(1)


# ---------------------------------------------------------------------------
# Core primitives -- same shape as 001-006
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Intent:
    kind: str  # "delegate"
    principal_id: str
    intent_id: str
    payload: dict
    consequence: str  # "low" | "high" | "privileged"


@dataclass(frozen=True)
class Decision:
    intent_id: str
    principal_id: str
    kind: str
    permitted: bool
    execution_class: str
    token: str


class Gate:
    CONSEQUENCE_TO_CLASS = {"low": "same_process", "high": "separate_process", "privileged": "uid_cgroup"}

    def __init__(self) -> None:
        self._secret = secrets.token_bytes(32)

    def _canonical(self, intent_id, principal_id, kind, permitted, execution_class) -> bytes:
        return f"{intent_id}:{principal_id}:{kind}:{permitted}:{execution_class}".encode("utf-8")

    def _mint(self, intent_id, principal_id, kind, permitted, execution_class) -> str:
        msg = self._canonical(intent_id, principal_id, kind, permitted, execution_class)
        return hmac.new(self._secret, msg, hashlib.sha256).hexdigest()

    def submit(self, intent: Intent) -> Decision:
        permitted = self._policy(intent)
        execution_class = self.CONSEQUENCE_TO_CLASS.get(intent.consequence, "same_process")
        token = self._mint(intent.intent_id, intent.principal_id, intent.kind, permitted, execution_class)
        return Decision(
            intent_id=intent.intent_id, principal_id=intent.principal_id, kind=intent.kind,
            permitted=permitted, execution_class=execution_class, token=token,
        )

    def verify(self, decision: Decision) -> bool:
        expected = self._mint(
            decision.intent_id, decision.principal_id, decision.kind, decision.permitted, decision.execution_class
        )
        return hmac.compare_digest(expected, decision.token)

    def _policy(self, intent: Intent) -> bool:
        return intent.kind == "delegate" and intent.consequence in ("low", "high", "privileged")


class GateViolation(PermissionError):
    pass


class ProvisioningError(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# uid+cgroup provisioning -- fresh, self-contained, same shape as 004/005/006
# ---------------------------------------------------------------------------


def _run(cmd: list[str]) -> subprocess.CompletedProcess:
    # File-redirected output, not capture_output=True (PIPE + communicate() under the hood) --
    # 006 found that combination unsafe under real concurrent load on this target. This experiment
    # doesn't run useradd/userdel concurrently (a single delegation, dispatched once), so the
    # hazard 006 hit specifically would not apply here -- but there is no reason to reach for a
    # pattern already known to be risky when the safe one costs nothing extra.
    out_fd, out_path = tempfile.mkstemp(prefix="sipho-007-run-")
    try:
        proc = subprocess.run(cmd, stdout=out_fd, stderr=subprocess.STDOUT)
        output = Path(out_path).read_text()
        return subprocess.CompletedProcess(cmd, proc.returncode, stdout=output, stderr=output)
    finally:
        try:
            os.close(out_fd)
        except OSError:
            pass
        try:
            os.unlink(out_path)
        except OSError:
            pass


def _find_free_uid() -> int:
    taken = {pw.pw_uid for pw in pwd.getpwall()}
    for candidate in range(UID_RANGE_START, UID_RANGE_END + 1):
        if candidate not in taken:
            return candidate
    raise ProvisioningError(f"no free uid in reserved range [{UID_RANGE_START}, {UID_RANGE_END}]")


def provision_ephemeral_user(execution_id: str) -> tuple[str, int, int]:
    uid = _find_free_uid()
    username = f"sipho7-{execution_id[:8]}"
    result = _run([
        "useradd", "--no-create-home", "--shell", "/usr/sbin/nologin", "--uid", str(uid),
        "--comment", "siphonophore ephemeral execution identity (experiment 007)", username,
    ])
    if result.returncode != 0:
        raise ProvisioningError(f"useradd failed (rc={result.returncode}): {result.stderr.strip()}")
    entry = pwd.getpwnam(username)
    if entry.pw_uid != uid:
        raise ProvisioningError(f"useradd created uid={entry.pw_uid}, expected {uid}")
    return username, entry.pw_uid, entry.pw_gid


def release_ephemeral_user(username: str) -> None:
    result = _run(["userdel", username])
    if result.returncode != 0:
        raise ProvisioningError(f"userdel failed (rc={result.returncode}): {result.stderr.strip()}")


def provision_cgroup(execution_id: str) -> Path:
    CGROUP_ROOT.mkdir(parents=True, exist_ok=True)
    cg = CGROUP_ROOT / f"exec-{execution_id}"
    cg.mkdir(parents=True, exist_ok=False)
    return cg


def add_pid_to_cgroup(cgroup_path: Path, pid: int) -> None:
    (cgroup_path / "cgroup.procs").write_text(str(pid))


def read_cgroup_procs(cgroup_path: Path) -> set[int]:
    text = (cgroup_path / "cgroup.procs").read_text()
    return {int(line) for line in text.split() if line.strip()}


def release_cgroup(cgroup_path: Path) -> None:
    remaining = read_cgroup_procs(cgroup_path)
    if remaining:
        raise ProvisioningError(f"refusing to release cgroup with live members: {remaining}")
    cgroup_path.rmdir()


def read_real_uid_from_proc(pid: int) -> int:
    status_text = Path(f"/proc/{pid}/status").read_text()
    for line in status_text.splitlines():
        if line.startswith("Uid:"):
            return int(line.split()[1])
    raise ProvisioningError(f"no Uid: line in /proc/{pid}/status")


# ---------------------------------------------------------------------------
# DESIGN.md SS3 -- the actual reconciliation logic, as real code
# ---------------------------------------------------------------------------


class BelnapValue(Enum):
    CORROBORATED = "corroborated"
    CONTRADICTION = "contradiction"
    UNREPORTED_ACTIVITY = "unreported_activity"
    NO_EVIDENCE = "no_evidence"


def reconcile(claimed: bool, observed: bool) -> BelnapValue:
    """The four-valued comparison DESIGN.md SS3 names. Reconciliation happens above the two
    claims, never inside either one -- this function takes two already-independently-produced
    booleans and does nothing else; it has no access to how `claimed` or `observed` were derived,
    which is the point (it cannot be biased toward trusting one channel over the other)."""
    if claimed and observed:
        return BelnapValue.CORROBORATED
    if claimed and not observed:
        return BelnapValue.CONTRADICTION
    if not claimed and observed:
        return BelnapValue.UNREPORTED_ACTIVITY
    return BelnapValue.NO_EVIDENCE


@dataclass(frozen=True)
class Claim:
    path: str
    content: str


@dataclass(frozen=True)
class SelfReport:
    """The agent's own, untrusted narrative -- DESIGN.md SS3: 'useful for explainability, worthless
    as ground truth on its own.' Nothing here is verified; it is exactly what the sub-agent's own
    process asserted about itself, collected from its own stdout."""
    principal_id: str
    claims: tuple[Claim, ...]


def reconcile_path(self_report: SelfReport, path: str, expected_content: str | None,
                    ground_truth: dict[str, str]) -> dict:
    """Reconcile one proposition about `path` against the self-report and ground truth.

    If `expected_content` is given, the proposition under test is "self_report claims path has
    exactly this content" vs. "ground truth shows path has exactly this content" -- this is how a
    genuine contradiction (T/F) is detected: the self-report can claim a path with SOME content
    while ground truth shows different content at that same path, which is claimed=True,
    observed=False for the specific claimed proposition, not merely "path exists nor not."

    If `expected_content` is None, the proposition under test is simply "path was claimed at all"
    vs. "path exists in ground truth at all" -- used for the unreported-activity and no-evidence
    checks, where the whole point is that NO claim mentions the path.
    """
    claim_for_path = next((c for c in self_report.claims if c.path == path), None)
    if expected_content is not None:
        claimed = claim_for_path is not None and claim_for_path.content == expected_content
        observed = ground_truth.get(path) == expected_content
    else:
        claimed = claim_for_path is not None
        observed = path in ground_truth
    value = reconcile(claimed, observed)
    return {
        "path": path, "claimed": claimed, "observed": observed, "value": value.value,
        "self_reported_content": claim_for_path.content if claim_for_path else None,
        "ground_truth_content": ground_truth.get(path),
    }


# ---------------------------------------------------------------------------
# The sub-agent's own program -- a real, separate process under a real provisioned uid.
#
# Deliberately constructs, in ONE real execution, all four situations the reconciler must tell
# apart: a genuine, truthful write (T/T); a claim about a file whose actual content differs from
# what's claimed -- a real, active lie (T/F); an extra write the self-report never mentions at all
# (F/T); and a path that is neither touched nor claimed (F/F, checked by the reconciler but never
# referenced by this program at all).
# ---------------------------------------------------------------------------

_SUBAGENT_PROGRAM = """
import json, os, sys

outdir = sys.argv[1]

# T/T: a write that genuinely happens, with content matching exactly what will be claimed below.
with open(os.path.join(outdir, "corroborated.txt"), "w") as f:
    f.write("this write really happened and matches the claim")

# T/F: the self-report below will CLAIM this path has one specific content -- but the process
# actually writes DIFFERENT content. A real, active lie: the claimed effect did not occur as
# described (the file exists, but not with the content the self-report asserts).
with open(os.path.join(outdir, "contradicted.txt"), "w") as f:
    f.write("the real content, which differs from what the self-report is about to claim")

# F/T: an extra, undisclosed write -- performed for real, but never mentioned anywhere in the
# self-report's claims list below.
with open(os.path.join(outdir, "unreported.txt"), "w") as f:
    f.write("a real effect that happened but was never self-reported")

# F/F ("never.txt"): deliberately neither touched nor claimed.

self_report = {
    "principal_id": "sub-agent",
    "claims": [
        {"path": "corroborated.txt", "content": "this write really happened and matches the claim"},
        {"path": "contradicted.txt", "content": "a claimed content that is simply not true"},
    ],
}
print(json.dumps(self_report))
"""


# ---------------------------------------------------------------------------
# Executor -- dispatches the `delegate` Intent, spawns the real sub-agent, collects both channels
# ---------------------------------------------------------------------------


class Executor:
    def __init__(self, gate: Gate) -> None:
        self._gate = gate

    def execute(self, decision: Decision, intent: Intent) -> dict:
        if decision.intent_id != intent.intent_id or decision.kind != intent.kind:
            raise GateViolation("decision does not correspond to this intent")
        if not self._gate.verify(decision):
            raise GateViolation("decision failed Gate verification -- forged, tampered, or downgraded")
        if not decision.permitted:
            raise GateViolation("decision denies this intent")

        if intent.kind == "delegate" and decision.execution_class == "uid_cgroup":
            return self._execute_delegate(decision, intent)
        raise GateViolation(f"no executor handler for kind={intent.kind!r} execution_class={decision.execution_class!r}")

    def _execute_delegate(self, decision: Decision, intent: Intent) -> dict:
        execution_id = decision.intent_id
        outdir = Path(intent.payload["outdir"])
        observations: dict = {"execution_id": execution_id}

        username, uid, gid = provision_ephemeral_user(execution_id)
        observations["provisioned_uid"] = uid
        cgroup_path = provision_cgroup(execution_id)

        stdout_fd, stdout_path = tempfile.mkstemp(prefix=f"sipho-007-stdout-{execution_id[:8]}-")
        proc: subprocess.Popen | None = None
        try:
            try:
                # user=/group=/extra_groups=, not preexec_fn -- and file-redirected stdout, not
                # PIPE/communicate() -- both patterns validated safe by 006 (preexec_fn is
                # documented-unsafe under threads regardless of whether this specific dispatch is
                # threaded; PIPE+communicate() raised a real, if concurrency-triggered, fd hazard
                # on this target). Neither hazard requires concurrency to be worth avoiding here.
                proc = subprocess.Popen(
                    [sys.executable, "-c", _SUBAGENT_PROGRAM, str(outdir)],
                    user=uid, group=gid, extra_groups=[],
                    stdout=stdout_fd, stderr=subprocess.DEVNULL,
                )
                os.close(stdout_fd)
                add_pid_to_cgroup(cgroup_path, proc.pid)
                observations["real_uid_from_proc_status"] = read_real_uid_from_proc(proc.pid)

                proc.wait(timeout=10)
                observations["child_returncode"] = proc.returncode
                observations["child_confirmed_exited"] = proc.poll() is not None

                # --- Self-report: exactly what the sub-agent's own process asserted, and
                # nothing else. Read from the file the PARENT opened; the child only ever had an
                # inherited fd, never the path itself. ---------------------------------------
                self_report_text = Path(stdout_path).read_text().strip()
                self_report_json = json.loads(self_report_text) if self_report_text else {"claims": []}
                claims = tuple(Claim(path=c["path"], content=c["content"]) for c in self_report_json.get("claims", []))
                self_report = SelfReport(principal_id=self_report_json.get("principal_id", "unknown"), claims=claims)
                observations["self_report"] = {
                    "principal_id": self_report.principal_id,
                    "claims": [{"path": c.path, "content": c.content} for c in self_report.claims],
                }

                # --- Ground truth: the ROOT PARENT's own independent read of the sub-agent's
                # output directory, collected AFTER independently confirming (proc.wait() above,
                # not merely trusting the self-report) that the child has exited. This never reads
                # or consults the self-report in any way -- it is a plain directory listing plus
                # file reads, exactly what an external observer per DESIGN.md SS5 would see with
                # zero siphonophore-specific code. ------------------------------------------------
                ground_truth: dict[str, str] = {}
                for entry in outdir.iterdir():
                    if entry.is_file():
                        ground_truth[entry.name] = entry.read_text()
                observations["ground_truth"] = ground_truth

            finally:
                try:
                    os.close(stdout_fd)
                except OSError:
                    pass
                try:
                    os.unlink(stdout_path)
                except OSError:
                    pass
        finally:
            try:
                release_cgroup(cgroup_path)
                observations["cgroup_released"] = not cgroup_path.exists()
            except ProvisioningError:
                observations["cgroup_released"] = False
            release_ephemeral_user(username)
            try:
                pwd.getpwnam(username)
                observations["user_released"] = False
            except KeyError:
                observations["user_released"] = True

        return {"effect": "delegate", "execution_class": "uid_cgroup", "observations": observations}


# ---------------------------------------------------------------------------
# Experiment
# ---------------------------------------------------------------------------


def run() -> dict:
    OUT.mkdir(parents=True, exist_ok=True)
    workdir = Path(tempfile.mkdtemp(prefix="sipho-007-"))
    outdir = workdir / "outdir"
    outdir.mkdir()
    os.chmod(workdir, 0o777)
    os.chmod(outdir, 0o777)  # provisioned uid needs write access, same reason as 004/005/006
    results: dict = {"workdir": str(workdir), "broker_pid": os.getpid(), "broker_uid": os.getuid()}

    gate = Gate()
    executor = Executor(gate)

    # --- Predicate A: reconcile() itself correctly implements all four cells of DESIGN.md SS3's
    # truth table, tested directly and independently of any real delegation. -------------------
    results["predicate_a_truth_table"] = {
        "TT": reconcile(True, True).value,
        "TF": reconcile(True, False).value,
        "FT": reconcile(False, True).value,
        "FF": reconcile(False, False).value,
    }

    # --- Predicate B: a real delegated sub-agent, under its own provisioned uid, produces all
    # four real situations in one execution; ground truth is collected independently by the root
    # parent; reconcile_path() classifies each into the expected Belnap value. -------------------
    b_intent = Intent(
        kind="delegate", principal_id="principal-alice", intent_id=str(uuid.uuid4()),
        payload={"outdir": str(outdir)}, consequence="privileged",
    )
    b_decision = gate.submit(b_intent)
    assert b_decision.execution_class == "uid_cgroup"
    b_effect = executor.execute(b_decision, b_intent)
    obs = b_effect["observations"]

    self_report = SelfReport(
        principal_id=obs["self_report"]["principal_id"],
        claims=tuple(Claim(path=c["path"], content=c["content"]) for c in obs["self_report"]["claims"]),
    )
    ground_truth = obs["ground_truth"]

    reconciled = {
        "corroborated": reconcile_path(self_report, "corroborated.txt",
                                        "this write really happened and matches the claim", ground_truth),
        "contradicted": reconcile_path(self_report, "contradicted.txt",
                                        "a claimed content that is simply not true", ground_truth),
        "unreported": reconcile_path(self_report, "unreported.txt", None, ground_truth),
        "never": reconcile_path(self_report, "never.txt", None, ground_truth),
    }

    results["predicate_b_real_delegation"] = {
        "provisioned_uid": obs["provisioned_uid"],
        "broker_uid": results["broker_uid"],
        "provisioned_uid_differs_from_broker": obs["provisioned_uid"] != results["broker_uid"],
        "real_uid_from_proc_status_matches_provisioned": obs["real_uid_from_proc_status"] == obs["provisioned_uid"],
        "child_confirmed_exited_before_ground_truth_read": obs["child_confirmed_exited"],
        "child_returncode": obs["child_returncode"],
        "self_report": obs["self_report"],
        "ground_truth": ground_truth,
        "reconciled": reconciled,
        "cgroup_released": obs["cgroup_released"],
        "user_released": obs["user_released"],
    }

    # --- Predicate C: forged Decision, never through Gate.submit(), refused before any
    # provisioning happens at all. ----------------------------------------------------------------
    c_intent = Intent(
        kind="delegate", principal_id="principal-eve", intent_id=str(uuid.uuid4()),
        payload={"outdir": str(outdir)}, consequence="privileged",
    )
    c_forged = Decision(
        intent_id=c_intent.intent_id, principal_id=c_intent.principal_id, kind=c_intent.kind,
        permitted=True, execution_class="uid_cgroup", token="beadfeed" * 8,
    )
    users_before = {pw.pw_name for pw in pwd.getpwall()}
    c_refused = False
    try:
        executor.execute(c_forged, c_intent)
    except GateViolation:
        c_refused = True
    users_after = {pw.pw_name for pw in pwd.getpwall()}

    results["predicate_c_forged_refused"] = {
        "refused": c_refused,
        "no_new_users_provisioned": users_after == users_before,
    }

    return results


def main() -> int:
    require_real_root_linux()
    results = run()

    out_path = OUT / "results.json"
    out_path.write_text(json.dumps(results, indent=2, default=str))
    print(f"wrote {out_path}")
    print(json.dumps(results, indent=2, default=str))

    a = results["predicate_a_truth_table"]
    b = results["predicate_b_real_delegation"]
    r = b["reconciled"]
    c = results["predicate_c_forged_refused"]

    checks = [
        ("truth table: T,T -> corroborated", a["TT"] == "corroborated"),
        ("truth table: T,F -> contradiction", a["TF"] == "contradiction"),
        ("truth table: F,T -> unreported_activity", a["FT"] == "unreported_activity"),
        ("truth table: F,F -> no_evidence", a["FF"] == "no_evidence"),
        ("real delegation: provisioned uid differs from broker", b["provisioned_uid_differs_from_broker"] is True),
        ("real delegation: kernel-observed uid matches provisioned", b["real_uid_from_proc_status_matches_provisioned"] is True),
        ("real delegation: child confirmed exited before ground truth read", b["child_confirmed_exited_before_ground_truth_read"] is True),
        ("real T/T case (corroborated.txt) -> corroborated", r["corroborated"]["value"] == "corroborated"),
        ("real T/F case (contradicted.txt) -> contradiction", r["contradicted"]["value"] == "contradiction"),
        ("real F/T case (unreported.txt) -> unreported_activity", r["unreported"]["value"] == "unreported_activity"),
        ("real F/F case (never.txt) -> no_evidence", r["never"]["value"] == "no_evidence"),
        ("real delegation: cgroup released", b["cgroup_released"] is True),
        ("real delegation: user released", b["user_released"] is True),
        ("forged: refused", c["refused"] is True),
        ("forged: no new users provisioned", c["no_new_users_provisioned"] is True),
    ]
    ok = True
    for name, passed in checks:
        print(f"[{'PASS' if passed else 'FAIL'}] {name}")
        ok = ok and passed

    shutil.rmtree(results["workdir"], ignore_errors=True)

    if not ok:
        print("HYPOTHESIS NOT SUPPORTED", file=sys.stderr)
        return 1
    print("HYPOTHESIS SUPPORTED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
