# Knowledge Operations P1 Source Connectors and Evidence Readiness

## Status and authority boundary

This P1 implementation is an offline-first, research/competition readiness
surface. It is not a diagnostic, treatment, emergency-triage, or automated
clinical-decision system. Every new record fixes:

- `knowledge_effect=informational_only`;
- `runtime_authority=none`;
- `contains_patient_data=false`;
- `release_ready=false` for draft/release-readiness objects.

Operational `SourcePolicy.live_network_enabled` remains `false`. The existing
`AcquisitionService` remains synthetic/offline-only. The optional live smoke
validator reads the pinned governance bundle to verify exact policy alignment,
but has no write path to the ledger, Claim promotion, manifests, product
database, pathway, UI, or runtime.

## P1a data flow

```text
typed official identifier
  -> exact endpoint request builder
  -> fake transport + bounded parser (default P1a)
  -> metadata-only structured record
  -> synthetic SourceCandidate / SourceSnapshot
  -> EVIDENCE_CANDIDATE (evc-*; digest + locator, no source text)
  -> machine_draft_claim_v2 (dcl-*; draft, synthetic, non-releasable)
  -> existing ReviewPacketBuilder
  -> existing ReviewEventService + ReviewerVerifier attestation
  -> production_eligible=false / release_ready=false
```

There is no EvidenceCandidate-to-v1-registry path. EvidenceCandidate and
MachineDraftClaim use exact `record_type`, `payload_type`, collection, ID
namespace, and ledger-version checks. The append boundary rejects a synthetic
successor that attempts to become non-synthetic.

## P1a privacy repair audit

Commit `c3dd453` changed only
`continucare/knowledge/ops/security.py` and the new
`tests/knowledge_ops/test_privacy_guard_technical_ids.py`. It added
`_TECHNICAL_HASH_ID_PATTERNS`, which skipped recursive sensitive-text scanning
for ungrouped `event-<20 hex>` and `attest|fixture-<32 hex>` values under the
corresponding ID keys. It also retained the earlier broad rule that trusted any
lower-case 64-hex value whenever the field name ended in `_sha256`. Both rules
were too broad: a digest-derived ID can coincidentally contain an 11-digit CN
mobile pattern, while an attacker-controlled 64-hex string can deliberately
contain one. Field name and shape alone do not prove cryptographic derivation.

The production importers of `security.py` are exactly:

- `continucare/knowledge/ops/__init__.py`;
- `acquisition.py`, `connectors.py`, `evidence.py`, `promotion.py`,
  `release.py`, and `review.py` under `continucare/knowledge/ops/`.

There are no production importers outside Knowledge Ops.

Before repair, both `aab5545` and `c3dd453` were scanned by reading
`git ls-tree -rz --name-only` output as NUL-delimited paths, loading each tracked
JSON blob directly from the selected commit, and recursively recording exact
`^[0-9a-f]{64}$` strings and `(?<!\d)1[3-9]\d{9}(?!\d)` matches. This avoids Git quoting
or non-ASCII/space-containing path omissions.

| Audited commit | Exact hex64 hits | CN-mobile-shape hits | Structured locations |
|---|---:|---:|---|
| `aab5545` | 28 | 0 | v1 bundle pins `$.files[0..4].manifest_sha256` (5); v2 bundle pins `$.files[0..4].manifest_sha256` (5); FHIR/evaluation schema digests (3); Layer 4 `$.details[0..4].outline_digest` and `.local_render_digest` (10); offline fixture `$.resources[0..4].content_sha256` (5) |
| `c3dd453` | 39 | 0 | all 28 base hits, plus v2.2 `$.files[0..5].manifest_sha256` (6) and `source_policies_v2_2.json` `$.policies[0..4].rights_evidence[0].document_sha256` (5) |

The categories were manifest pins, evaluation-output digests, fixture-content
digests, and captured rights-document digests. No non-digest field contained a
phone-shaped hit, every committed digest had an identified producer or
verification boundary, and no historical manifest, fixture, golden, or digest
was modified.

### Exact path plus verified-context digest recognition

`AUDITED_SHA256_FIELDS` is now an evidence inventory only; membership and a
`_sha256` suffix grant no scanner trust. `assert_no_sensitive_data` defaults to
zero digest trust. A digest is skipped only when a caller selects a code-defined
`DigestTrustProfile`, the value occurs at an exact root-relative schema path,
the value is exactly 64 lower-case hexadecimal characters, and the caller has
already established the profile's context. That context includes the exact
collection and `payload_type`, strict-model validation, append-only ledger
digest replay, byte recomputation, loaded-bundle equality, or an explicit
verifier contract as applicable.

Profiles contain no mapping-key wildcard. Sequence-index wildcards occur only
inside strict typed tuples such as manifest, artifact, checklist-evidence, or
LedgerRef lists. Unknown fields such as `foo_sha256`, the same field at a wrong
or nested `metadata` path, wrong length/alphabet, and mixed case all undergo
normal PII scanning. Open `metadata`, rationale, notes, conditions,
limitations, and legacy/untyped payloads never inherit digest trust.

| Exact field(s) | Production or recomputation boundary |
|---|---|
| `entry_sha256`, `supersedes_entry_sha256` | `AppendOnlyLedger.append` hashes canonical entry bytes; `_history_unlocked` recomputes entries and predecessor links |
| `catalog_sha256` | `OfflineFixtureConnector` hashes the exact catalog bytes before loading |
| `content_sha256` | offline/live connector and quarantine boundaries hash exact content bytes; quarantine reads rehash before return |
| `metadata_sha256` | `AcquisitionService._metadata_digest` hashes canonical SourceCandidate metadata |
| `whole_record_sha256` | EvidenceCandidate copies the verified SourceSnapshot content digest and promotion checks the equality |
| `whole_response_sha256` | connector `response_digest` and the body-free live report hash exact response bytes |
| `document_sha256` | `SourceRightsEvidence` pins the captured official-document bytes; rights posture stays `needs_verification` until independent verification |
| `manifest_sha256` | v1/v2 loaders hash every pinned manifest before model loading |
| `bundle_index_sha256`, `governance_index_sha256` | `KnowledgeOpsBundle.index_sha256()` hashes canonical index content; read/review/release paths compare the pin |
| `safety_boundary_sha256` | `ReviewPacketBuilder` hashes canonical SafetyBoundary content and packet validation recomputes it |
| `subject_entry_sha256` | ReviewPacket copies the exact `LedgerRef.entry_sha256` and validates equality |
| `expected_predecessor_sha256` | ReviewEvent binds the current append-only predecessor; decision replay checks the chain head |
| `provenance_evidence_sha256` | typed declaration only; it is currently untrusted because no evidence producer/verifier binding exists. Every profile deliberately excludes it, and a phone-bearing value is rejected. |
| `verification_evidence_sha256`, `reviewer_verification_evidence_sha256` | `ReviewerVerifier` identity-evidence contract and immutable ReviewEvent snapshot equality checks |
| `reviewer_identity_assertion_sha256` | canonical reviewer-identity assertion producer and event verifier recomputation |
| `event_claim_sha256` | canonical ReviewEvent claim producer and verifier-attestation binding |
| `attestation_sha256` | `ReviewerVerifier` issues and verifies the attestation; the built-in readiness verifier uses keyed HMAC and remains synthetic-only |

A global 64-hex shape exemption and field-name-only exemption are intentionally
forbidden. Hex permits every decimal digit, so a deliberately or
coincidentally phone-shaped substring is possible. Only the exact path plus its
verified context can avoid forcing an append-only digest rewrite.

### `assert_no_sensitive_data` call-point classification

The final `rg -n "assert_no_sensitive_data\\(" continucare/knowledge/ops`
inventory has no unclassified production call. Recursive calls inside
`security.py` are scanner implementation, not separate trust decisions.

| Module | Class A — open/untrusted, no digest trust | Class B — exact verified model/profile | Class C — impossible digest |
|---|---|---|---|
| `acquisition.py` | SourceCandidate open fields, discovered-resource metadata, and decoded synthetic fixture body | AcquisitionRun, replayed/created SourceSnapshot, ChangeSet, and KnowledgeGap | none |
| `connectors.py` | FixtureResource with `content_sha256` removed, before file access | strict FixtureResource after exact fixture bytes are rehashed | none |
| `evidence.py` | strict SourceCandidate including open metadata | replayed SourceSnapshot, EvidenceCandidate, predecessor MachineDraftClaim, and new MachineDraftClaim | none |
| `promotion.py` | strict SourceCandidate including open metadata | replayed SourceSnapshot/KnowledgeGap, PromotionDecision after every ref resolves, and GovernedSourceV2 | none |
| `review.py` | generated-by/known-limitations, unknown or legacy packet/supplemental evidence, open review text, strict SourceCandidate, and legacy subject payloads | strict typed subject dispatcher; typed review-evidence dispatcher; KnowledgeGap; ReviewPacket after packet-material replay; ReviewEvent only after identity, predecessor, evidence, and attestation verification | none |
| `release.py` | release-candidate open projection (including author provenance), strict SourceCandidate, and legacy artifact payloads | strict artifact dispatcher, KnowledgeGap, verified KnowledgeReleaseCandidate, readiness report with base/all-resolved variants, and KnowledgeRelease | none |

Class C is intentionally empty. Every otherwise digest-free strict object in a
scanner call still contains open strings or metadata and therefore remains in
Class A rather than receiving a bypass. Every Class B selection occurs only
after collection, `payload_type`, strict model, and the relevant digest/ref
integrity checks. SourceSnapshot reads after acquisition use the same exact
schema profile only after append-only replay and strict cross-field validation.

Review evidence now has one centralized dispatcher shared by packet creation,
packet-material replay, ReviewEvent supplemental checklist evidence, and gate
decision replay. It performs, in order: exact `LedgerRef` replay and ledger
digest verification; exact `(collection, payload_type)` dispatch; strict model
validation; record identity and synthetic-lineage checks; nested reference
replay; type-specific lineage/integrity verification; and only then selection
of a code-defined digest profile. The current trusted pair is exactly
`(snapshot, source_snapshot)` with
`ACQUISITION_SOURCE_SNAPSHOT`. A known collection/type used in the wrong pair
is rejected; unknown and legacy pairs receive zero trust and normal PII
scanning.

The deterministic review regression fixes a canonical candidate entry whose
SHA-256 is
`5e06fcf272eadac5481501b2a120de788abbb5f6b6dcd9fb30cf15804119261b`;
it contains the production-boundary CN-mobile shape `15804119261`. The digest
is accepted only when nested in a fully replayable strict SourceSnapshot.
The same value in open metadata remains rejected, and wrong pair, strict-model,
identity, missing-reference, lineage, and partial-write cases all fail closed.

### Machine-generated value inventory

Every machine-generated value of relevant length and numeric alphabet has one
of three dispositions; there is no residual fourth category.

| Class | Values | Treatment and evidence |
|---|---|---|
| A — actual SHA-256 | ledger entry/predecessor, catalog/content/metadata/whole-record/whole-response, document/manifest/index/boundary, review identity/event/attestation, and locator fingerprint digests | Exact audited fields above; corresponding producer/replay/validator establishes integrity. `whole_record_sha256` is the non-reconstructive locator fingerprint. |
| B — internal IDs | acquisition run/candidate/snapshot/ChangeSet/Gap IDs; review packet and review-chain IDs; event and attestation IDs; release-readiness IDs; synthetic verifier fixture attestation IDs | All digest-derived producers use `digest_derived_internal_id`. Event IDs retain 20 digest hex characters; all other listed generators retain 32. Hex is grouped with `-` every at most 8 characters, preserving entropy while preventing a long digit run. |
| C — operational values outside free-text scanning | atomic-write `secrets.token_hex(12)` nonce filenames, ephemeral HMAC key, `TemporaryDirectory` names, and temporary test DB/cache paths | They are never serialized into a scanned knowledge payload. Quarantine `relative_path` is handled as a structural technical path, is canonical-path validated, and is independently bound to a rehashed `content_sha256`; it is not treated as free text. |

Static `fixture_set_id` values are governed SafeIds, not machine-generated
nonces. The only digest-derived `fixture-*` identifier is synthetic verifier
test evidence and uses the same grouping helper.

The numeric-pattern metadata fixes the current lower bound at 11 unseparated
digits for a CN mobile number and 17 for a CN national ID; the international
phone pattern requires a leading `+`. The grouping maximum is 8, strictly less
than `min_sensitive_unseparated_digit_run=11`. A deterministic 10,000-seed
test runs each actual generator family (event, attestation, fixture,
packet/review chain, acquisition, and release), proves stable output, unchanged
effective digest length, maximum digit run below the derived threshold, and a
successful full sensitive-data scan. Any future shorter numeric PII pattern
changes the shared metadata and causes the ID safety tests to fail.

## Official metadata connectors

| Source | Exact contract | Retained fields | Explicit exclusions |
|---|---|---|---|
| DailyMed | `GET /dailymed/services/v2/spls/{set_id}/history.json` | SetID, SPL version, publication date, title metadata, locator | label body; automatic assertion of the latest FDA conclusion |
| EMA | documented website medicines JSON data file | product number, medicine name, active substance/status/revision metadata, official locator | PMS/SPOR registration APIs; undocumented XHR/backends; HTML scraping; ePI pilot; document body |
| MedlinePlus | dated official health-topics XML feed | topic ID/title/language/update/canonical link | full summary, patient-facing body, third-party body, translation/adaptation |
| PubMed | E-utilities ESummary JSON for a typed PMID | bibliographic title/date/source and record locator | abstract, article body, clinical conclusion |
| PMC OA | legacy OA service for a typed PMCID | per-article licence label/retraction/locator metadata | article/full-text package; automatic open-licence conclusion |

PubMed and PMC are separate policies and rights scopes. NCBI requests contain
no API key or personal email. Both endpoints use `rate_limit_key=ncbi` and a
thread-safe process-default limiter with monotonic time, so different connector
and transport instances share a minimum 400 ms request-start interval. Clock,
sleeper, and limiter are injectable; offline tests use fake time, perform no
real wait, and reset the process singleton before and after each relevant test.
Non-NCBI policies default to independent source buckets. The legacy PMC OA
service is documented as scheduled to stop on or after 2026-08-24; HTTP 404 or
410 is `contract_changed` with no retry and no scraping fallback.

## Controlled input boundary

Live builders accept only strict models:

- lower-case UUID-form DailyMed SetID;
- fixed registered EMA dataset ID;
- fixed MedlinePlus feed kind plus a typed date;
- numeric PMID or upper-case `PMC` PMCID.

They do not accept `query: str`, AcquisitionRequest free text, patient
sentences, URLs, or arbitrary search terms. Path and query values are checked
again at the transport boundary. Percent encoding, nested encoding, Unicode
confusables, control characters, case variants outside the ID grammar,
unexpected keys, duplicate keys, and non-allowlisted paths fail before DNS and
before fake request capture.

## DNS/socket/TLS binding and response limits

The optional live transport:

1. requires both global egress and exact case-sensitive Knowledge flag value
   `"true"`, plus an opaque `KnowledgeEgressPermit`;
2. accepts HTTPS on port 443 only, with exact host/path/query and no userinfo,
   fragment, proxy, cookie, redirect, or caller-supplied URL;
3. resolves all DNS answers and rejects the whole request if any answer is not
   globally routable;
4. connects only to a validated IP, performs TLS with the original hostname as
   SNI using `ssl.create_default_context()`, certificate verification,
   hostname checking, and TLS 1.2 minimum without fixing a maximum TLS version;
5. verifies `getpeername()` against the validated DNS set before sending the
   HTTP request, while preserving the original official `Host` header;
6. sends `Accept-Encoding: identity` and rejects compressed or transformed
   responses;
7. does not trust `Content-Length`: it rejects an over-cap declaration, reads
   non-chunked bodies to EOF and chunked bodies through bounded framing, applies
   a streaming hard cap, and rejects declaration/actual-size mismatch;
8. accepts only endpoint-allowlisted JSON/XML MIME types and strict UTF-8;
9. applies bounded JSON depth/container/field/scalar checks and DTD/entity-free
   XML depth/element/attribute/text checks.

At most two retries are possible, only for timeout, HTTP 429, and selected
500/502/503/504 responses. Sleeper, clock, and jitter are injectable. HTTP
401/403/404/410, redirects, identity failures, parser failures, and policy
failures are never retried. Both 404 and 410 are normalized to
`contract_changed`. `Retry-After` over 30 seconds is not slept.

## Stable error and disposition model

The connector taxonomy includes feature/policy, DNS/peer/TLS, HTTP, response,
parser, contract, and rights errors. Each maps to one of `retry`, `gap`,
`abort`, or `not_attempted`. A live report normalizes those outcomes to:

- `validated`;
- `access_blocked`;
- `rate_limited`;
- `network_failed`;
- `contract_changed`;
- `rights_unresolved`;
- `not_attempted`.

The report contains only source, official documentation URL, endpoint
origin/path template, timestamp, HTTP status, normalized MIME, byte count,
whole-response digest, parsed record count, stable error, and limitations. It
cannot contain response bodies or write Knowledge state.

## Rights evidence and manifest history

`source_policies_v2.json` and `bundle_index_v2.json` remain byte-identical.
`source_policies_v2_2.json` is an append-only file-version-2 delta that extends
the exact file-version-1 predecessor. `bundle_index_v2_2.json` pins both source
policy versions and selects version 2 as the current head. The loader
materializes the contiguous policy chain; callers that load the old index still
receive the original eight-policy bundle.

Each new policy records only an official documentation URL, UTC retrieval time,
whole-document byte digest, recorder identity, default-deny conclusion, and
known limitations. No terms page body is committed. Since there is no formal
rights officer, every new policy remains `needs_verification`, and high-risk
reuse is denied or review-required. Metadata discovery is the only automatic
online-relevant operation described by policy; operational live acquisition is
still disabled.

## Versioned readiness Gap registry

`readiness_gaps_v1.json` is hash-pinned by the append-only
`bundle_index_v2_3.json` (`bundle_version=3`); its SHA-256 is
`9dea836f1d987b38d0cd76342bc10c053c2969c57d1c0ff8f8e7ff90f8475025`
and its size is 7,632 bytes. The builtin v2 loader selects v3. Existing
`bundle_index_v2.json`, `bundle_index_v2_2.json`, their source-policy files,
and every historical digest remain byte-identical and independently loadable;
old indexes return an empty readiness-Gap set rather than backfilling history.

The frozen registry contains exactly these 12 open records:

| Gap ID | Exact subject | Blocks |
|---|---|---|
| `gap-p1a-dailymed-live-validation-not-attempted` | `source-dailymed@1` | persistent validation, production eligibility, release, P1b live validation |
| `gap-p1a-ema-live-validation-not-attempted` | `source-ema-website-data@1` | persistent validation, production eligibility, release, P1b live validation |
| `gap-p1a-medlineplus-live-validation-not-attempted` | `source-medlineplus@1` | persistent validation, production eligibility, release, P1b live validation |
| `gap-p1a-pubmed-live-validation-not-attempted` | `nlm-pubmed-metadata@2` | persistent validation, production eligibility, release, P1b live validation |
| `gap-p1a-pmc-live-validation-not-attempted` | `source-pmc-open-access@1` | persistent validation, production eligibility, release, P1b live validation |
| `gap-p1a-dailymed-rights-unresolved` | `source-dailymed@1` | reuse beyond metadata/link-only, production eligibility, release |
| `gap-p1a-ema-rights-unresolved` | `source-ema-website-data@1` | reuse beyond metadata/link-only, production eligibility, release |
| `gap-p1a-medlineplus-rights-unresolved` | `source-medlineplus@1` | reuse beyond metadata/link-only, production eligibility, release |
| `gap-p1a-pubmed-rights-unresolved` | `nlm-pubmed-metadata@2` | reuse beyond bibliographic metadata/link-only, production eligibility, release |
| `gap-p1a-pmc-rights-unresolved` | `source-pmc-open-access@1` | reuse beyond per-article licence-locator metadata/link-only, production eligibility, release |
| `gap-p1b-cold-import-socket-proof-pending` | governance gate `cold_import_socket_proof` | production eligibility, release, P1b live validation |
| `gap-core-symptom-catalog-terminology-alias-review-pending` | Core Symptom Catalog `2.0.0`: nausea, vomiting, diarrhea, abdominal-pain, constipation, decreased-appetite, fatigue, dizziness, dyspnea | v2 consumer integration only |

The source Gaps contain only exact `SourcePolicyRef` values. They do not copy
licence posture, live-network posture, operation decisions, or response caps.
The loader dereferences the policies and fails closed if a referenced policy is
missing, a rights Gap contradicts `verified_open`, a live Gap contradicts live
posture, or unresolved rights coexist with operations beyond metadata/link-only.

The persistent governance read model therefore reports all five sources as
`not_attempted` and `metadata_link_only`, with
`production_eligible=false`, `release_ready=false`,
`consumer_integration_ready=false`, `knowledge_effect=informational_only`, and
`runtime_authority=none`. Eleven source/socket Gaps block production and
release; the catalog Gap blocks only v2 consumer integration and does not block
P1a readiness. Source production promotion and KnowledgeRelease readiness use
the same open registry and fail closed.

A `LiveValidationReport` is transient run evidence only. It may truthfully
record `validated` during a future authorized run, but cannot write or resolve
the registry, change persistent readiness, or make a release eligible;
`wrote_knowledge_state=false` and `release_ready=false` are fixed. A `/tmp`
JSON report is never the governance truth source.

Registry v1 can express only `lifecycle=open`; `resolved` and resolution fields
fail model validation. A future resolution must use a new hash-pinned successor
manifest, retain old-manifest loadability, include an evidence reference,
enforce `created_by` principal different from `resolved_by` principal, and bind
resolution to the existing `ReviewerVerifier` and attested `ReviewEvent` path.
Until that successor schema and formal path exist, no boolean edit can resolve
a Gap and no formal approval is claimed.

## Core Symptom Catalog v2

The shared catalog is owned by `continucare-shared-terminology`, not a pathway,
and is not imported by UI/runtime/pathway modules.

| Benchmark | v2 status | Existing/candidate reference |
|---|---|---|
| nausea | reused concept | `nausea` |
| vomiting | reused concept | `vomiting` |
| diarrhea | reused concept | `diarrhea` |
| abdominal-pain | reused concept | `abdominal-pain` |
| constipation | reused concept | `constipation` |
| bloating | alias candidate only | `abdominal-distension` |
| decreased-appetite | reused concept | `decreased-appetite` |
| fatigue | reused concept | `fatigue` |
| dizziness | reused concept | `dizziness` |
| dyspnea | reused concept | `dyspnea` |
| chest-pain | internal candidate | no external code/reference |
| rash | alias candidate only | `skin-eruption` |

All mappings remain inherited-unverified or pending-unverified. No new SNOMED,
ICD-11, LOINC, or MedDRA code is claimed. The schema carries semantic
non-equivalence boundaries and rejects clinical fields such as triage,
severity, risk, diagnosis, treatment, recommendation, or clinical rule.

## Synthetic review fixture

The executable test fixture uses a synthetic machine author and a distinct
synthetic clinical reviewer with different identity and principal IDs. It calls
the existing `ReviewPacketBuilder`, `ReviewEventService`,
`InMemoryReviewerDirectory`, and `ReviewLedgerDecisionProvider`. The event has
an ephemeral verifier attestation and cannot count toward release. Same identity,
same principal through another account, forged formal status, incomplete gate,
and synthetic-lineage laundering all fail closed. No demo review helper or
parallel approval system exists.

## Promotion import isolation

The promotion boundary is enforced by a test-only Python AST import graph, not
grep and not runtime monkeypatching. It maps project modules including package
`__init__.py` files, parses `import`, `from ... import ...`, relative imports,
and package-export edges, then traverses direct and transitive dependencies.

Forward roots are `app.py`, every `pages/**` module, all pathway entries,
Layer 4 and business-service entries, the actual agents runtime and
care-agent/care-engine service entries, and v1 Knowledge package,
`__main__`, registry, render, models/read DTO, and resolver entries. They must
not reach the unique module defining `EvidenceCandidatePromotionService`,
`continucare.knowledge.ops.promotion`, the Knowledge Ops package export, or
acquisition/connector/evidence/review/release/store/live-validation write
boundaries. Reverse traversal from evidence and Source promotion must not reach
app/pages, pathway, runtime/service, or v1 registry/render entries.

Mutation-sensitivity fixtures prove both a direct import and an indirect chain
through a package `__init__`, a relative import, and two intermediate modules
are detected. None of the protected entry files was modified. This is a static
architectural gate; it is not the deferred process-start cold-import socket
proof.

## Final repair blockers and resolution

Three coordination-window blockers were closed without changing any historical
digest, manifest, fixture, golden file, runtime authority, or clinical content:

1. **Digest trust was not bound to verified context.** Field-name trust could
   either hide an attacker-controlled phone-bearing hex value or reject a real
   machine-derived digest inconsistently at later call sites. The repair makes
   trust exact-path and profile-specific, scans open material before integrity
   verification, keeps `provenance_evidence_sha256` untrusted, and covers every
   actual strict machine path. The deterministic regression payloads are
   `verified-digest-fixture-318` and `verified-acquisition-digest-467`; tests
   recompute their fixed SHA-256 values and confirm the production `cn_phone`
   boundary pattern before positive and negative path assertions.
2. **Production Source-promotion rejection precedence was reversed.** A
   persistent readiness Gap masked a more immediate synthetic-lineage failure.
   Production now rejects synthetic candidate/snapshot/decision first, then
   rejects the persistent readiness Gap for non-synthetic input. Both exact
   error messages are preserved, and both failures prove unchanged ledger row
   count and collection heads.
3. **Typed review evidence was rescanned without replay context.** A valid
   SourceSnapshot could carry a ledger-entry SHA-256 containing an accidental
   phone-shaped digit run inside `candidate_ref.entry_sha256`; generic evidence
   scans then rejected it. All four review evidence call points now use the
   centralized typed dispatcher above. No PII pattern, global field exemption,
   64-hex exemption, payload self-assertion, or legacy trust was added.

Final diff self-review also found and fixed one narrow readiness error-class
edge: a Gap collection head with the wrong `payload_type` is now recorded as an
`invalid_gap_reference` blocker rather than allowing `ValueError` to escape.
The final trust audit then required every nested LedgerRef in strict
SourceSnapshot, ChangeSet, EvidenceCandidate, MachineDraftClaim,
GovernedSourceV2, KnowledgeGap, and release subject/artifact context to replay
successfully before a profile is selected. A phone-bearing but unreplayable ref
now fails on ledger integrity with no write. Both findings were committed as
separate fixes, not folded into earlier history. The typed review-evidence fix
is likewise an append-only successor commit.

## Running validation

All P1a tests run with `CONTINUCARE_KNOWLEDGE_LIVE_VALIDATION=false`, global
external egress disabled, every LLM unconfigured/disabled, external adapters in
mock/disabled mode, a temporary `PYTHONPYCACHEPREFIX`, and a temporary
`CONTINUCARE_DB_PATH`. A scoped test guard rejects `socket.socket`, `socket.create_connection`,
`SSLContext.wrap_socket`, and any SQLite path outside that test's temporary
root. The cold v1 Knowledge import chain has a pre-existing urllib3 local IPv6
capability probe; the isolated subprocess preloads that third-party module,
then proves that incremental Knowledge Ops imports, bundle/read-model loading,
factories, and the default validator report make zero socket and zero SQLite
calls. No v1 import code was changed to conceal this limitation.

Only after all offline gates pass may the live validator be invoked in a
separate process:

```bash
CONTINUCARE_EXTERNAL_EGRESS_ENABLED=true \
CONTINUCARE_KNOWLEDGE_LIVE_VALIDATION=true \
python -m continucare.knowledge.ops.source_connectors.live_validation
```

The command accepts no URLs, queries, output paths, credentials, or patient
data. Live-smoke retries are explicitly disabled, so it makes at most five fixed
metadata requests (one per modeled endpoint), uses an automatically removed
temporary directory, prints one body-free JSON report, and never changes the
parent environment.

No live validation was executed in P1a. The explicit default-off run remains
`request_count=0`, all five records remain `not_attempted`, and
`wrote_knowledge_state=false`. No socket, DNS, HTTP, external API, real patient
data, ledger state, committed manifest, product database, pathway, UI, or
runtime was written during implementation or verification.

Final offline validation used a temporary `PYTHONPYCACHEPREFIX` and temporary
`CONTINUCARE_DB_PATH` for every Python command. Decisive results:

- `pytest -q tests/knowledge_ops/test_privacy_guard_technical_ids.py`:
  `49 passed`;
- `pytest -q tests/knowledge_ops/test_evidence_candidate_promotion.py`:
  `17 passed`;
- `pytest -q tests/knowledge_ops`: `166 passed`;
- `pytest -q tests/test_knowledge_ops_acquisition.py`: `67 passed`;
- `pytest -q tests/test_knowledge_ops_review_release.py`: `49 passed` after
  the final readiness edge test;
- final `pytest -q`: `775 passed, 3 skipped`; the prior repair state was
  `739 passed, 3 skipped, 1 failed`, and all three unchanged skips only request
  the official `FHIR_R4_SCHEMA_ZIP`;
- `python -m compileall -q continucare app.py pages`: exit 0;
- `git diff --check c3dd453e52415eca549c63a6915ec5b6a1edfcbf..HEAD`
  and `git diff --check`: exit 0.

An instrumented default-off probe replaced socket creation, DNS resolution,
HTTP request, TLS wrapping, and `SecureMetadataTransport.execute` with rejecting
counters. Its result was
`calls={api:0,dns:0,http:0,socket:0}, request_count=0, record_count=5,
statuses={not_attempted}`. P1b therefore remains `not_attempted`; no live source
contract has been asserted.

The existing scoped socket guard was deliberately not redefined or promoted to
a root autouse fixture. P1b remains blocked on redefining the invariant from
"socket constructed" to "non-loopback outbound connection attempted", proving
that guard at process start, completing the cold-import socket proof, and then
running validation only in a separately authorized isolation window. EMA's
official JSON documentation evidence does not independently unblock P1b.

## Remaining production blockers

- No formal rights officer decision exists.
- No formal clinical reviewer or pharmacist approval exists.
- Synthetic approvals cannot establish production eligibility.
- EMA and MedlinePlus public datasets may exceed the P1 hard byte cap.
- The legacy PMC OA metadata contract may retire or change.
- Official endpoint schemas and terms can change and therefore require ongoing
  ChangeSet/Gap handling and human review.
- Process-local rate limiting does not coordinate multiple OS processes; any
  future multi-process validator requires an external shared budget before it
  may be enabled.
- The AST gate covers Python static imports in the current project module tree;
  future dynamic import mechanisms require a separately reviewed fail-closed
  rule.
- The process-start non-loopback socket invariant and authorized live source
  validation remain P1b work, not P1a evidence.
- No production Claim, Binding, patient content, KnowledgeRelease, pathway,
  clinical rule, state-machine change, or runtime authority is created here.
