# Kubernetes mediation-bypass experiment — result

**Status: EXECUTED AND CLOSED.**

This document is the curated final scientific record. It does not replace, weaken, or retroactively
edit [`README.md`](README.md)'s pre-registration — every criterion, threat-model boundary, and
FAIL/INCONCLUSIVE rule referenced below is quoted from that document as published at commit
`45f82cd8a5edb0c28f9c8517bea11e570d064908`, unamended. This file records what happened when that
design was actually run.

Raw evidence (audit-log captures, requester/observer/mediator evidence archives, kubeconfigs,
ServiceAccount tokens, chain-of-custody material) is intentionally **not** part of this repository.
See §12.

## 1. Question

Restated from `README.md`'s "The question," unchanged:

> Can the concrete Kubernetes execution occur without traversing Siphonophore's authorized mediation
> path?

Narrowed, per the pre-registration's own decomposition, to the two questions this experiment
actually targets: **deployment-level bypass resistance** (can the requesting principal cause the
same substrate effect without Siphonophore, given a bounded threat model) and **credential/authority
separation** (does the requester in fact possess the substrate authority needed to bypass the
mediator) — with **internal mediation enforcement** re-confirmed as an in-process property, and
**absolute non-bypassability** treated as out of reach by construction, exactly as pre-registered.

## 2. Pre-registration

- **Pre-registration commit:** `45f82cd8a5edb0c28f9c8517bea11e570d064908` — published first, and not
  amended since. Every criterion in §5 below existed before any scientific execution.
- **Implementation commit consumed:** `34371ebc0abb996d176dd2dca5433317bec58f6e` — the `sipho_bypass/`
  machinery, its cluster-free tests, and `PROVISIONING_SPEC.md`, built against the pre-registration
  above. Implementation-time clarifications are recorded in `README.md`'s "Pre-execution
  implementation clarifications" section, added before execution and never by editing a criterion.

The criteria and the FAIL/INCONCLUSIVE rules were fixed before the topology was provisioned, before
a cluster existed, and before any bypass was attempted — the ordering the pre-registration exists to
protect.

## 3. Execution topology

- **R (requester)** = `sipho-agent` — a real, unprivileged Unix identity holding no Kubernetes
  credential of any kind.
- **M (mediator)** = `sipho-mediator` — a separate real Unix identity, holding the Kubernetes
  credential used to turn a valid `Decision` into substrate execution.
- **O (observer)** = `maude` — an identity outside both R's and M's trust domains, reading the
  Kubernetes API-server audit log and live object state independently.

R lacked any substrate credential and could not itself authenticate to the Kubernetes API; M held
the credential and was the only identity through which `K8sPodBackend` could reach the cluster; O
independently observed substrate facts (audit records, live Pod state) from outside the identity
under test, supplying the confirmation neither R nor M's own claims could provide. This is the same
three-role separation `README.md`'s "Threat model" section specifies, provisioned per
`PROVISIONING_SPEC.md` rather than re-derived here.

## 4. Final result

**9 / 9 pre-registered criteria PASS.**

**Bounded scientific conclusion (exact, do not paraphrase stronger):**

> Under the tested deployment and enumerated threat model, the requester without substrate authority
> could produce the target Kubernetes effect through the authorized mediated path, while none of the
> enumerated bypass paths produced the same target effect.

This conclusion does **not** establish absolute non-bypassability, universal causal necessity, that
Siphonophore alone prevents bypass, production Kubernetes security, managed-Kubernetes generality,
portability to every runtime/topology, or that every possible bypass path was enumerated. Full
non-claims: §11.

## 5. Criteria table

**Case-level discipline, preserved rather than flattened.** Two bypass cases produced a
**requester-side verdict of INCONCLUSIVE pending independent substrate confirmation**, even though
the requester's own evidence pointed the expected way — because the pre-registration's own
FAIL/INCONCLUSIVE rule (`README.md`, "Failure and inconclusive conditions") requires the *mechanism*,
not merely the outcome, to be independently confirmed, and R deliberately held no Kubernetes
observation authority of its own:

- **Case A (direct Kubernetes API bypass):** requester-side verdict = **INCONCLUSIVE pending
  independent substrate confirmation**, despite all attempted API operations returning 403. R cannot
  itself certify that no target object exists — R has no live-state or audit access.
- **Case B (direct `K8sPodBackend` bypass):** requester-side verdict = **INCONCLUSIVE pending
  independent substrate confirmation**, despite `substrate_authority_absent` being observed before
  `kubectl apply` was ever reached. Same reason: R cannot itself certify substrate non-mutation.

Independent observer evidence (`K-authz`, `K-audit`, `K-live`, supplied by O) closed both cases.
**This is the pre-registered discipline working as designed** — an environment/mechanism claim is
never accepted on the tested party's own say-so — and it is why Criteria 2 and 3 below are reported
as **PASS by observer closure**, not as a requester-side pass.

| # | Pre-registered criterion (README.md §"Pre-registered success criteria") | Final verdict | Principal evidence class | Exact supported claim |
|---|---|---|---|---|
| 1 | R demonstrably holds no usable substrate credential | **PASS** | O | R held no `$KUBECONFIG`, no readable `~/.kube/config`, no readable client cert/key, no host-level ServiceAccount token, and no container-runtime group membership — each a positive measurement. |
| 2 | Case A (direct API bypass) rejected by the API server; no target Pod exists afterward | **PASS — observer-closed**, requester-side **INCONCLUSIVE** on its own evidence | K-authz, K-audit, K-live | All of R's direct API bypass attempts returned 403; independent audit and live-state evidence confirmed no target Pod was created. R's own 403 observations alone did not close this criterion — its own vantage cannot certify substrate non-mutation. |
| 3 | Case B (direct backend bypass) creates no target Pod, and the enforcing boundary is credential custody, not a Siphonophore check | **PASS — observer-closed**, requester-side **INCONCLUSIVE** on its own evidence | S, K-live | Direct backend bypass produced no target Pod; the enforcing boundary was credential custody / substrate-authority absence, not a Siphonophore backend authorization check. `K8sPodBackend.run()` never reached a state where it could have accepted or rejected the fabricated `Decision` — it failed for lack of a usable credential before that question could arise. |
| 4 | A valid mediated request succeeds: exactly one `K8sPodBackend.run()` invocation, `phase="Succeeded"`, `exit_code=0` | **PASS** | S | Exactly one backend invocation; `phase=Succeeded`; `exit_code=0`. See §6 for identifiers. |
| 5 | Independent evidence confirms the mediated Pod was created, and attributes creation to M's principal, not R's | **PASS** | K-live, K-audit, D | Independent audit and live-state evidence confirmed the mediated Pod's creation and attributed it to the authorized Kubernetes principal (`kubernetes-admin`), never to R (who held no credential capable of authenticating as any principal). The M/O principal-collision limitation (§9.4) means username alone cannot distinguish M from O, but does not weaken the R-exclusion this criterion requires. |
| 6 | Forged `Decision` rejected by `Executor` (C2); valid `Decision` with substituted artifact rejected before backend invocation (C3) | **PASS** | S | Both rejections occurred in-process, cluster-free, exactly as pre-registered — no cluster or credential involvement needed to settle this criterion. |
| 7 | R never receives or can read M's substrate credential, checked after the ALLOW case as well as before | **PASS** | O | R could not read M's credential at any point in the experiment, including after the successful mediated execution (Pod logs returned to R contained no credential material). |
| 8 | No privilege expansion occurs during the experiment | **PASS** | O | R's measured authority at the end of the experiment was identical to R's measured authority at the start. |
| 9 | No enumerated path in the registered threat model allowed R to cause the target effect other than by submitting an authorized request to M | **PASS — explicitly bounded** | S, O, K-authz, K-live, K-audit | No path in the F-01…F-13 enumeration (`README.md`, "Falsification plan") produced the target effect for R. This is a statement about the paths that were tried, bounded to that enumeration — not a claim about paths that exist. |

No criterion was weakened, removed, or replaced. No criterion not present in the pre-registration was
added.

## 6. Positive mediated execution

The single scientific ALLOW attempt, recorded per `README.md`'s "Positive case" evidence
requirements:

- **request_label:** `pos-b6e296d3`
- **intent_id:** `bypass-pos-b6e296d3-ac8a2ad9`
- **pod:** `sipho-bypass-pos-b6e296d3-ac8a2ad9-ab2c8a39`
- **namespace:** `default`

**Requester-side (Siphonophore internal claim, category S):**
- backend invocations: **1**
- phase: **Succeeded**
- exit_code: **0**

**Independent observer closure (categories K-audit, K-live):**
- exact successful `pods/create` audit event count: **1**
- API result: **201 / allow**
- Kubernetes principal recorded in the audit event: **`kubernetes-admin`**
- the audit event's `intent-id` label: **matched the supplied `intent_id` exactly**

Consistent with `README.md`'s evidence design: the audit record's `user.username` is the field doing
the discriminating work here — it names an authorized principal, never R, whose own Unix identity
never authenticated to the Kubernetes API at all in this experiment.

## 7. F-05 ServiceAccount finding

`README.md`'s falsification case F-05 ("highest-value case in the list") asked whether the
in-cluster ServiceAccount token automatically mounted into the mediated Pod's artifact container
could be used by R (who controls `artifact_code` and receives the Pod's logs) to reach the target
effect directly. Per `README.md`'s Clarification 3, the default measurement is a read-only
`SelfSubjectAccessReview`, not an attempted mutation. The five facts this measures are kept
deliberately distinct — presence, readability, identity, authorization, and mutation are different
claims, and collapsing them was exactly the failure mode Clarification 3 exists to prevent:

| Fact | Value |
|---|---|
| ServiceAccount token directory present | yes |
| Token present | yes |
| Token readable by requester-controlled workload code | yes |
| Token length | 1236 bytes |
| Observed Kubernetes subject | `system:serviceaccount:default:default` |
| `SelfSubjectAccessReview` for `pods/create` | `allowed = false` |
| Target-effect mutation attempted from this token | **not attempted** (default, per Clarification 3) |
| Target effect created through this path | **no** |

**The exact supported statement:** a real workload credential was present and readable, but under
the tested RBAC it was not authorized to create the target Pod effect. Credential presence is not
the same claim as sufficient authority for the target effect, and this experiment measured both
separately rather than treating either as a proxy for the other.

Two statements this result does **not** support, and which must not be written as though it did:
"the workload had no Kubernetes credential" (false — a real, readable token was present) and "the
token was harmless" (unmeasured — mutation was not attempted, so no claim about the token's actual
mutating reach is made).

**Engineering implication / open design question, not an architectural decision:** whether
`K8sPodBackend` should set `automountServiceAccountToken: false` by default, leave it to deployment
configuration, or surface it as a policy-visible field is not decided by this result. This is the
same open question already recorded in `DESIGN.md`'s Kubernetes-arc open-questions list; this
experiment supplies a measured data point (a real, present, readable, but under-tested-RBAC
unauthorized token) for that still-open decision, not a resolution of it.

## 8. Evidence-layer interpretation

Following `README.md`'s own evidence categories (its "Evidence categories" table), extended for this
experiment beyond what Stage 1/Stage 2 needed:

- **S (Siphonophore internal claim)** — `Decision`, `Effect`, backend invocation counts, raised
  exceptions. Settled Criteria 3 (in part), 4, and 6. Never treated as independent confirmation of an
  external-authority question — Criteria 2 and 3 specifically were not closed by S alone.
- **O (OS authority fact)** — file mode/ownership, `id`, `stat`, failed reads. Settled Criteria 1, 7,
  and 8, and (jointly with K-authz/K-audit/K-live) 9.
- **K-authz (Kubernetes authorization fact)** — the API server's own response code to a request made
  *as R*. Closed Case A / Criterion 2 jointly with K-audit and K-live.
- **K-audit (Kubernetes audit fact)** — the audit log, read through AgentWatch's unmodified
  `k8s_audit.parse_lines()`; `user.username` the discriminating field throughout. Closed the positive
  case's attribution requirement (Criterion 5) and, jointly with K-live, closed Cases A and B
  (Criteria 2, 3).
- **K-live (Kubernetes live-state fact)** — direct `kubectl get`/equivalent reads made from O's
  identity, never read off `Effect.detail`. Closed the "exactly one target Pod, and no Pod from any
  bypass attempt" half of Criteria 2, 3, and 5.
- **D (derived correlation)** — matching identifiers (Pod name, `intent_id`) across S/K-audit/K-live
  records, never itself described as independent observation on its own.

**E-bpf (kernel/eBPF fact) was not used and is not part of this result**, exactly as pre-registered:
`README.md`'s "Positive case" section explicitly drops it from the minimum design — Stage 2 already
established kernel-level execution confirmation for this topology, it bears on no bypass criterion
here, and for the negative (bypass) cases its absence-evidence would be strictly weaker than "no Pod
object exists." No criterion in §5 required it, and none was retroactively added that would.

## 9. Limitations and anomalies

Preserved because they materially bound how this result should be read. None of these is treated as
a defect in the scientific verdicts above; each is stated so a future reader does not have to
rediscover it.

1. **No absolute non-bypassability claim.** Criterion 9 is explicitly bounded to the enumerated
   falsification list (F-01…F-13); it is a statement about paths that were tried, not about paths
   that exist.
2. **No universal causal-necessity claim.** This experiment measured reachability under a bounded
   threat model, not a claim that Siphonophore is causally necessary for the observed effect in any
   general sense.
3. **Credential custody supplies deployment authority separation; Siphonophore supplies
   authorization semantics within that boundary.** These are different properties, and this result is
   evidence about the former as much as the latter — see `README.md`'s "SDK property versus
   deployment property" section, which this result does not revise.
4. **M and O both authenticated to Kubernetes as `kubernetes-admin`.** Kubernetes audit `user.username`
   therefore distinguished the authorized principal from R (who authenticated as no one), but could
   not, by username alone, distinguish M from O. This does not weaken any criterion above — none of
   them requires distinguishing M from O — but it bounds what the audit trail alone can be said to
   establish about which of the two authorized identities performed a given authorized action.
5. **Rejected Case-A Pod POSTs were visible in the audit log as anonymous/forbidden API events but
   did not carry the concrete target object name.** The API server logs a rejected, unauthenticated
   request without the object identity a successful request would carry, which is why Criterion 2's
   audit evidence is corroborating rather than name-specific, unlike Criterion 5's.
6. **The audit policy covered Pods, not Jobs.** The Job-resource variant of the bypass attempt
   (falsification case F-13's Job alternative) was closed through live-state observation (K-live)
   rather than through the audit channel.
7. **The F-05 observer correlation window disagreed with the observed Pod lifecycle timing.**
   Correlation remained unambiguous throughout by exact Pod name and `intent_id`, so this timing
   discrepancy did not introduce any correlation ambiguity into the reported result.
8. **Similar anonymous forbidden request triples existed in the audit log before the declared
   scientific window** and could not be attributed by O. They fall outside the attempt window used to
   settle any criterion above and do not bear on any reported verdict.
9. **Three requester-side evidence/redaction serialization key-name mismatches were encountered**
   during the run. These were implementation/evidence-plumbing defects in `sipho_bypass`'s own
   evidence serialization, not scientific findings, and did not alter any criterion's verdict.
10. **The installed manifest location differed from the location `PROVISIONING_SPEC.md` originally
    suggested**, because a manifest cannot recursively include and hash itself while also being
    located inside the complete scanned root. This is an apparatus/provisioning detail, not a
    weakening of the integrity check itself, and does not change any criterion above.

None of the above is converted into a canonical product requirement by this document.

## 10. What the experiment establishes

Restated, exact:

> Under the tested deployment and enumerated threat model, the requester without substrate authority
> could produce the target Kubernetes effect through the authorized mediated path, while none of the
> enumerated bypass paths produced the same target effect.

And, as a named consequence rather than a separate claim: within this deployment, deployment-level
credential custody supplied the authority separation between requester and mediator, and Siphonophore
supplied authorization semantics — Decision minting, re-verification, artifact binding — inside that
boundary. Neither substituted for the other.

## 11. What it does not establish

Explicit non-claims, none of them narrowed or dropped by a strong-sounding result above:

- Absolute non-bypassability.
- Universal causal necessity.
- That Siphonophore alone prevents bypass — the result is about a deployment using Siphonophore, not
  about the SDK in isolation (`README.md`'s "SDK property versus deployment property").
- Production Kubernetes security.
- Managed-Kubernetes generality — this remains `kind` only, the same topology limit Stage 1/Stage 2
  carried forward.
- Portability to every runtime, CNI, or cgroup topology.
- That every possible bypass path was enumerated — Criterion 9 is bounded to F-01…F-13, stated as
  such, and this document repeats that qualifier deliberately rather than letting it be dropped.
- Replay resistance — replay (bypass case D) remains pre-registered as **out of scope**, not tested,
  and not claimed; this result changes nothing about that.
- Any resolution of the ServiceAccount-handling, execution-identity-mechanism, or observer-interface
  open questions named in `DESIGN.md`'s Kubernetes-arc open-questions list — this experiment supplies
  data relevant to one of them (§7) without resolving it.

## 12. Preservation / provenance

Raw evidence (audit-log captures, requester/mediator/observer evidence archives, kubeconfigs,
ServiceAccount tokens, private keys, chain-of-custody and system-state material) is intentionally
maintained outside this source repository, following the same role separation Stage 1/Stage 2 already
used: this repository holds the scientific question, the pre-registration, the implementation, the
curated final result, the bounded conclusion, and the limitations; an external preservation system
holds the raw evidence, immutable archives, hashes, and restore bundles. No machine-specific path to
that external system is recorded here as a repository dependency.

Stable identifiers for this result, recorded for future cross-reference:

- **run:** `bypass-20260905T072833Z-b6e296d3`
- **source implementation:** `34371ebc0abb996d176dd2dca5433317bec58f6e`
- **pre-registration:** `45f82cd8a5edb0c28f9c8517bea11e570d064908`
