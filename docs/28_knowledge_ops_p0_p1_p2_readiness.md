# M5-K Knowledge Operations P0–P2 Readiness

> [!IMPORTANT]
> **Historical snapshot — superseded for current capability status.** This
> document records the state at completion of the initial P0–P2 readiness
> slice. It is retained as historical delivery evidence and is not the
> authoritative source for current capability status. Statements below that
> source-specific connectors, real parsers, `EvidenceCandidate`, and Core
> Symptom Catalog v2 were not started or deferred apply only to that historical
> slice. Subsequent P1a connector/parser contracts for DailyMed, EMA,
> MedlinePlus, PubMed, and PMC metadata, together with `EvidenceCandidate` and
> Core Symptom Catalog v2, have been implemented and merged into mainline.
>
> Use the current capability matrix in
> [`docs/32_knowledge_capability_review_guide.md`](32_knowledge_capability_review_guide.md)
> as the authority. See
> [`docs/30_knowledge_ops_p1_source_connectors.md`](30_knowledge_ops_p1_source_connectors.md)
> for current P1a connector facts,
> [`docs/31_knowledge_v2_alias_consumer_readiness.md`](31_knowledge_v2_alias_consumer_readiness.md)
> for current alias readiness, and
> [`docs/knowledge_v2_mainline_integration_validation_2026-08-14.md`](knowledge_v2_mainline_integration_validation_2026-08-14.md)
> for mainline integration evidence. P1b live validation remains
> `not_attempted`, and operational live acquisition remains disabled.
> Implemented connector contracts do not establish real acquisition, rights
> approval, formal release, or production readiness. No formal reviewer,
> rights decision, or `KnowledgeRelease` exists, and v2 alias consumer/UI
> integration remains unimplemented. The boundaries
> `knowledge_effect=informational_only` and `runtime_authority=none` remain in
> force.

## 1. Status and product boundary

This slice extends the existing M5-K Knowledge Evidence foundation. It does not
replace the v1 registry and does not create a parallel clinical knowledge
authority.

The stable v1 read API and all v1 manifests remain unchanged. New capabilities
live under `continucare.knowledge.ops` and `continucare.knowledge.manifests_v2`.
They cover source governance, staged acquisition, review mechanics, and release
readiness only.

The following invariants are model-level literals, manifest assertions, and
regression tests:

- `knowledge_effect=informational_only`;
- `runtime_authority=none`;
- launch jurisdiction/language are China mainland and `zh-CN`;
- allowed uses are internal knowledge operations, acquisition-basis
  explanation, and informational display;
- diagnosis, treatment recommendation, emergency triage, automated clinical
  decisions, runtime state transitions, and patient-specific web searches are
  prohibited;
- no patient data is an accepted acquisition input;
- live networking is disabled throughout contract v2.0;
- machine activity cannot approve a clinical Claim, Binding, patient content,
  or KnowledgeRelease;
- synthetic approvals never count toward production readiness;
- no clinical rule reference can be included in a release candidate or release.

No production reviewer, license decision, clinical approval, Binding approval,
patient-content approval, or KnowledgeRelease is asserted in this slice.

The implementation status is intentionally narrower than the phase labels:

- P0 governance foundation: **complete**;
- P1 offline acquisition foundation: **complete**;
- P1 source-specific connectors, real parsers, and live API validation: **not
  started**;
- P2 review, Review Packet, review-event, and release-readiness mechanisms:
  **complete**;
- production reviewer identity integration: **incomplete**;
- formal P2 clinical approval and KnowledgeRelease: **incomplete**;
- post-P2 clinical/runtime work: **not started**.

## 2. Incremental architecture

The dependency direction is one-way:

```text
v1 Knowledge registry (unchanged, curated read surface)

v2 governance manifests
  -> SourcePolicy / validation profile / review gate / blocked release intent
  -> offline acquisition connector
  -> append-only Candidate / Snapshot / ChangeSet / Gap ledger
  -> Review Packet / ReviewEvent chains
  -> governed v2 Source promotion
  -> KnowledgeRelease readiness assessment
```

Nothing in `pathways`, `clinical_rules`, the Layer 3/4 state machine, patient
services, or the application UI imports the v2 package.

## 3. P0 governance foundation

P0 adds:

- `ClinicalContextScope`, with explicit jurisdiction, language, condition,
  product, care-setting, population, and intended-use dimensions;
- a rule that clinical scope cannot use `GLOBAL` as an implicit jurisdiction;
- an exhaustive `SafetyBoundary` manifest;
- eight Source Policies covering conservative operational postures for NMPA,
  ICD-11, LOINC, MedDRA, HPO, PRO-CTCAE, CTCAE, and PubMed metadata;
- five synthetic validation profiles: medication follow-up, chronic
  cardiopulmonary, oncology PRO, acute high-risk symptoms, and rare-disease
  terminology;
- eight manual governance gates for Source promotion, content persistence,
  mapping, translation, Claim, Binding, patient content, and release;
- a hash-pinned v2 bundle loader that loads no partial state;
- append-only manifest history with an explicit current head per manifest ID;
- a UI-independent incremental read model;
- a filesystem append-only ledger with exact versions, predecessor SHA-256,
  canonical JSON, exclusive locking, atomic no-overwrite creation, and full
  chain verification.

The broad-coverage strategy is therefore L1-first. L2–L6 are created only for
an explicit demand/scope. A global Source can be registered, but a future
clinical Claim must still use a concrete jurisdiction scope such as `CN`.

## 4. Source Policy semantics

A Source Policy is an operational guard, not a license approval. It records:

- exact allowed HTTPS origins and subdomain posture;
- allowed query keys and content types;
- a response byte ceiling;
- current license posture;
- an operation-by-operation decision;
- exact terms URI when known;
- the fixed `live_network_enabled=false` boundary.

Missing operations are denied. Only link metadata registration and metadata
discovery may be automatically allowed. Full text, quotation, translation,
adaptation, redistribution, mapping, commercial use, training, and vector
indexing are either human-review-required or denied.

The manifest statements are deliberately conservative. `needs_verification`,
`registration_required`, and `license_required` are blockers, not informal
permission claims. No built-in policy currently claims verified-open or
verified-restricted reuse rights.

## 5. P1 acquisition staging

P1 implements the following offline flow:

1. accept a de-identified `AcquisitionRequest` with controlled topic codes,
   normalized query terms, exact validation profile, and exact Source Policies;
2. discover fixture metadata through an `OfflineFixtureConnector`;
3. validate every URL against its Source Policy before connector access;
4. create an append-only `SourceCandidate`;
5. fetch only a hash-pinned synthetic fixture, then independently revalidate
   connector/resource identity, URL, content type, byte limit, digest, UTF-8
   inspectability, and direct-identifier guard;
6. place exact bytes in a content-addressed synthetic quarantine;
7. create an append-only `SourceSnapshot` containing content and metadata
   digests;
8. compare it with the previous exact snapshot and create a `ChangeSet`;
9. create explicit rights-review and curator-review `KnowledgeGap` records,
   plus a content-change gap when applicable;
10. close the `AcquisitionRun` as completed or failed with a stable, sanitized
    failure code.

Repeated acquisition does not overwrite a Candidate, Snapshot, ChangeSet, Gap,
or Run. An unchanged result is itself a new historical observation. Changed
content creates a new digest and `content_changed` ChangeSet; history remains
intact.

Partial staging after a connector failure is retained rather than rolled back.
The terminal Run and connector-failure Gap make that state explicit and do not
persist raw exception messages or credentials.

## 6. Connector, SSRF, and privacy boundary

The connector interface separates discovery from fetch. The only enabled
implementation is `OfflineFixtureConnector`. `GuardedHttpConnector` is present
only as an inert, default-disabled boundary with no built-in network client.
There are no DailyMed, EMA, MedlinePlus, or PubMed/PMC source-specific
connectors in this slice.

`GuardedHttpConnector` has no built-in network client. It remains inert unless
both the connector and Source Policy enable networking and a reviewed transport
is injected. Contract v2.0 makes Source Policy network enablement impossible,
so real HTTP cannot occur in this slice.

The future transport boundary requires:

- HTTPS only;
- exact allowlisted hosts;
- no credentials, fragments, unsafe ports, IP-literal hosts, traversal, or
  unapproved query keys;
- a public peer-IP attestation for the initial request and every redirect;
- at most three redirects, with policy validation repeated on every hop;
- allowlisted content type and maximum byte size.

Acquisition requests reject direct-identifier fields and common email, phone,
national-ID, MRN, or labeled-patient patterns. URL or free-text patient context
cannot be used as a search term. Technical hashes and governed identifiers are
distinguished from personal identifiers to avoid treating content digests as
patient data.

## 7. Candidate to Source promotion

Promotion creates a governed v2 Source, not a v1 Source and not a clinical
Claim. It requires the Source-promotion gate's curator and rights decisions.

The promotion service verifies:

- exact current Candidate and Snapshot refs;
- Candidate–Snapshot ownership;
- Source Policy registration posture;
- latest required role decisions;
- exact ReviewEvent evidence refs;
- exact open gaps carried by the approved Review Packet;
- synthetic/production separation.

Synthetic promotion results in:

- `registry_status=synthetic_fixture`;
- `production_eligible=false`;
- `access_mode=quarantined_synthetic_fixture`;
- explicit unresolved Gap refs;
- `runtime_authority=none`.

A production Source cannot be promoted with synthetic evidence or unresolved
gaps. Contract v2.0 additionally blocks production Source promotion because
its pinned intent declares that neither formal reviewers nor formal license
decisions exist. No automatic clinical-Claim or Binding promotion API exists.

## 8. P2 reviewer and Review Packet model

Supported roles are:

- knowledge curator;
- rights officer;
- terminologist;
- clinical reviewer;
- pharmacist.

`ReviewerIdentity` separates three assurance states and pins a stable principal,
authorized roles, exact jurisdictions/scopes, and an authorization validity
interval:

- `synthetic_test`;
- `identity_unverified`;
- `formally_verified`.

The built-in in-memory verifier is ephemeral and readiness/test-only; it rejects
formally verified identities. A production decision provider cannot be
constructed without an injected trusted reviewer verifier. A future production
identity integration must resolve the current identity and authorization and
supply an external verification reference, verification evidence SHA-256,
verifier, timestamp, and verifiable event attestation. Unverified identities
cannot approve. Synthetic identities can act only on synthetic packets and can
never become production-eligible.

A Review Packet pins:

- exact subject LedgerRef and digest;
- exact governance bundle ID/version, canonical index digest, and every
  historical/current manifest digest;
- subject kind and governance gate;
- exact requested roles;
- exact Source operations requiring a rights decision;
- Source Policy when relevant;
- clinical context scope;
- evidence refs and open Gap refs;
- structured author identity/provenance for Claim, Binding, terminology
  mapping, translation, patient content, and KnowledgeRelease gates;
- known limitations;
- the SafetyBoundary digest;
- generator and timestamp;
- synthetic and no-patient-data status.

The builder also derives every currently open Gap whose subject is the exact
review subject (or its owning Candidate for Snapshot, ChangeSet, and Source
reviews). A caller cannot hide a known machine-created Gap by omitting it from
the request. A later Gap-head change invalidates the prior gate decision and
requires a new packet.

An approved ReviewEvent requires a checklist with at least one pass and no
failure. A rights approval must explicitly approve every requested Source
operation; omitted operations remain denied. Clinical, terminology, and
pharmacy approvals must confirm the packet's exact scope.

ReviewEvent decisions are explicit `in_review`, `revision_requested`,
`rejected`, or `approved` states; later events append rather than mutate the
earlier decision.

Events form one append-only ledger chain for each exact subject and role. The
latest head controls the gate. A new packet invalidates approvals tied to an
older packet. Synthetic event heads may exercise the mechanism but always have
`counts_toward_release=false`. A multi-role gate also requires distinct reviewer
principals, so two accounts belonging to the same person cannot satisfy two
roles.

Every event pins the complete reviewer authorization snapshot, expected prior
event digest, and a verifier-issued attestation over the event claim. Gate
resolution re-resolves each reviewer, compares the current trusted identity to
the event snapshot, verifies authorization both at decision time and resolution
time, and validates the attestation. Missing, inactive, expired, mismatched, or
unverifiable identities fail closed. `counts_toward_release` remains an audit
record only; production eligibility is recomputed from current trusted identity
state and verified attestations.

For Claim, Binding, terminology mapping, translation, patient-content, and
KnowledgeRelease gates, the service rejects an author who reviews the same
object. Resolution repeats the identity/principal separation check, so a signed
event appended directly to the ledger cannot bypass it. This is a mechanism,
not evidence that any real author or reviewer has been verified.

Event recording and gate resolution independently revalidate the packet's
current subject, required roles and Source operations, policy/profile scope,
open Gaps, governance pins, and approval payloads. Direct low-level ledger
writes that omit or contradict those constraints are rejected by gate
resolution.

## 9. KnowledgeRelease readiness

The v2 bundle contains a release-intent manifest with:

- no selected artifact;
- no formal reviewer;
- no formal license decision;
- `release_ready=false`;
- `status=readiness_only_blocked`.

A `KnowledgeReleaseCandidate` pins the exact governance bundle, canonical
bundle-index digest, and every manifest digest. The readiness service rejects
manifest substitution before it records the candidate and rechecks the same
evidence during assessment.

Readiness fails closed for any of the following:

- the pinned governance release intent explicitly remains blocked;
- empty release;
- stale or unknown exact refs;
- artifact-kind/collection mismatch;
- synthetic candidate or artifact;
- non-production Source;
- scope/profile, jurisdiction, or language mismatch;
- open Gap, including gaps carried by a governed Source even if the release
  assembler omits them;
- patient-data guard failure;
- missing artifact-level gate approval;
- synthetic or unverified artifact approval;
- missing release-level role approval;
- missing conditional roles from the validation profile;
- synthetic or unverified release approval.

`finalize()` always writes a readiness report first. If blockers exist it raises
`KnowledgeReleaseBlocked` and writes no release. A successful future release is
still informational-only, contains no ClinicalRule ref, and has no runtime
authority. Contract v2.0 cannot reach that success path: its pinned release
intent is a mandatory blocker. Enabling a later release therefore requires an
explicitly reviewed manifest/model version change rather than a runtime flag.

## 10. Synthetic five-domain fixtures

The five offline files contain no medical content. They are labeled synthetic
and exist only to verify acquisition mechanics:

- medication follow-up: no product claim, threshold, or advice;
- chronic cardiopulmonary: no clinical claim or severity rule;
- oncology PRO: no instrument item, option, translation, or score;
- acute high-risk symptoms: no triage or escalation rule;
- rare-disease terminology: no ontology term, mapping, or translation.

Each file and the fixture catalog is SHA-256 fixed. The connector rejects a
catalog mismatch, content mismatch, traversal, symlink, excessive bytes, policy
mismatch, or disallowed content type.

## 11. Current safe operating procedure

Until formal roles and rights decisions exist:

1. load the built-in v2 governance bundle;
2. create only a de-identified request scoped to an existing validation
   profile;
3. run only the offline fixture connector;
4. inspect Candidate, Snapshot, ChangeSet, and Gap history;
5. use synthetic reviewers only for mechanism tests;
6. do not label synthetic Source promotion as production registration;
7. assess release readiness and expect blockers;
8. do not call or expect a successful release finalization.

## 12. Explicitly deferred work

The following remains outside P0–P2 readiness:

- formal reviewer identity-provider integration;
- actual license applications, purchases, or legal decisions;
- source-specific DailyMed, EMA, MedlinePlus, and PubMed/PMC connectors;
- real Source discovery, source parsers, live API validation, crawling, or bulk
  download;
- real medical content import or translation;
- an `EvidenceCandidate` model or `EvidenceCandidate`-to-draft-Claim flow;
- eight new symptom-benchmark candidates or a core symptom catalog v2;
- production Claim or Binding authoring and approval;
- patient-facing content publication;
- runtime clinical rules, activation, or safety case;
- vector/RAG indexing;
- patient-specific acquisition;
- UI integration.

Post-P2 runtime discussion is permitted only after separate evidence that the
exact Claim, Binding, artifact governance, clinical validation, monitoring,
rollback, and product regulatory safety case are complete. Nothing in this
slice satisfies that threshold.
