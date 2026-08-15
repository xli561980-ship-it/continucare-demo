# Knowledge Evidence Foundation

## 1. Purpose and boundary

This M5 slice adds a pathway-agnostic, static Knowledge Evidence registry. It
answers why a governed artifact exists or uses a representation; it does not
authorize, publish, activate, or execute that artifact.

The four information layers remain separate:

1. **Source Document** registers a public document or standard as an exact,
   versioned record.
2. **Knowledge Claim** records a bounded statement, what it supports, what it
   does not support, its scope, and exact citations.
3. **Executable Artifact** remains under its own governance. A knowledge
   binding is informational and never supplies runtime eligibility.
4. **Patient Evidence** remains the patient's QuestionnaireResponse,
   Observation, Timeline, and Summary evidence. No patient identifier, patient
   store, or runtime patient data is accepted by this package.

GLP1-14D is the first migration fixture, not a special case in the contracts.
The generic models contain no GLP-1 product, disease, jurisdiction, canonical,
or pathway constant. A second synthetic pathway exists only in tests to prove
scope isolation and is not a new product manifest.

This slice does not add a database, vector index, RAG, web crawler, medical QA,
external evaluation corpus, alert, threshold, clinical rule, diagnosis,
treatment recommendation, deployment, or M6 integration.

## 2. Existing contracts and convergence boundary

Before this slice, three repository areas held related but non-equivalent data:

| Area | Existing responsibility | Kept separate / convergence rule |
| --- | --- | --- |
| `pathways` | Versioned Pathway manifest, FHIR artifact references, legacy source metadata, and legacy rule list | Remains the owner of pathway publication and artifact discovery. Legacy source IDs have exact aliases into the new source registry. A knowledge loader never treats legacy approval fields as authority. |
| `terminology` | Versioned catalog, questionnaire bindings, concepts, source IDs | Remains the terminology artifact. Catalog ownership is explicitly mapped to an exact Pathway version; catalog source IDs have exact aliases. Knowledge records do not overwrite the catalog. |
| `layer4` | Patient runtime contracts, clinical rule definitions, and rule approvals | Remains the only existing clinical-rule execution gate. Knowledge does not import Layer4 and cannot make a rule executable. Patient `EvidenceReference` is not reused as a knowledge citation. |

The new registry is the authoritative representation for new Knowledge Source,
Claim, Binding, Review Event, and Coverage Gap records. Existing pathway and
terminology source fields are migration inputs only. They should eventually be
generated projections or exact refs, but that larger convergence is outside
this slice.

The current GLP1-14D manifest continues to have `clinical_rules=[]`. This is a
regression assertion about that fixture, not a global restriction on future
pathways and not a property inferred by the knowledge renderer.

## 3. Contract model

### 3.1 Exact immutable references

Every relationship uses an exact compound reference:

- `SourceRef(source_id, record_version)`
- `ClaimRef(claim_id, claim_version)`
- `BindingRef(binding_id, binding_version)`
- `CoverageGapRef(gap_id, gap_version)`
- `PathwayRef(pathway_code, pathway_version)`

Records are append-only. A successor stores only a forward `supersedes` ref to
the immediately preceding version. Reverse links, current heads, and lifecycle
views are derived. Old records are never rewritten to point at a successor.

Review decisions are append-only events. Each event has a unique `event_id`;
a later event uses `supersedes_event_id`. For each exact subject and review
domain the chain must have one root, one head, no branch, and no cycle.

### 3.2 Source records

A `SourceRecord` separates internal record version from an external document
version. It can record document identifiers, authority, typed jurisdiction,
language, publication/effective dates, retrieval precision, URLs, license terms,
and lifecycle without inventing unknown values.

The four migrated DailyMed entries only provide a title, URL, and retrieval
date as separate fields in the repository. Their authority and jurisdiction
metadata are explicitly `not_available_in_repository`, while document version
remains null because the repository does not provide one; the host is not
silently presented as the document's issuing authority. A DailyMed set ID is
mechanically extracted from each existing URL and remains unverified, as does
the URL's classification as canonical. Unknown language is `und`.

Access is discriminated:

- `link_only` always has integrity `not_content_fixed`; it cannot carry stored
  content bytes, a content hash, or an embedded quote.
- `local_artifact` carries a safe relative path, byte size when known, and an
  exact SHA-256. CURRENT loading requires an explicit safe artifact root, a
  byte-for-byte hash match, and the unique trusted head of a license decision
  approving `local_copy`.

The bundle-manifest hash and a third-party document hash are separate controls.
The former fixes registry JSON bytes; it never implies that an external URL is
content-fixed.

### 3.3 Claims, scope, and citations

Clinical and terminology claims have an exact statement, `supports`, mandatory
`does_not_support`, a lifecycle, and one or more exact citations. Locators are a
typed union (page, section, table/figure, terminology concept, answer list, or
URL fragment); placeholder free text is not a locator.

Clinical scope uses an explicit Pathway-version whitelist and typed dimensions
for condition, product, jurisdiction, care setting, age, and population. Empty
collections never mean “all.” A universal scope is restricted to non-clinical
terminology, unit, or interoperability standards. Even a universal claim needs
an explicit binding to each artifact and exact Pathway version.

`WorkflowDesignDecision` is a distinct, non-evidence-based claim kind. This
slice does not fabricate historical owners or decisions: artifacts missing that
governance are represented by versioned coverage gaps instead.

### 3.4 Typed artifact relationships

Bindings resolve typed artifact refs, not a generic string tuple. Slice 1 can
resolve:

- Questionnaire item: exact canonical, version, and recursive `linkId`;
- Observation mapping item: exact Pathway, version, owned mapping file, and
  `linkId`;
- Questionnaire terminology binding: exact catalog/version and `linkId`;
- terminology concept: exact catalog/version and concept ID;
- whole PlanDefinition: exact canonical/version.

The contract also reserves typed versioned refs for rules, red flags, education,
and summaries. Because this slice has no resolver for those types, selecting one
in CURRENT mode fails closed. HISTORICAL mode keeps it visible as unresolved.

Each binding records a deterministic set of independent approval requirements.
Those values describe which artifact governance must act; they are not approval
state. There is deliberately no `approved_for_execution`, `activation_gate`, or
second copy of artifact lifecycle in this package.

The registry does not impose a one-relationship-per-artifact rule. The same
exact artifact may have multiple claims or purposes and may also have an open
gap. Each relationship and gap keeps its own versioned ID and exact artifact
ref; presentation counts distinct targets dynamically rather than assigning
artificial inventory numbers.

### 3.5 Review policy and real authority

Persisted JSON contains an actor assertion, never a self-declared
`authority_resolved=true`. CURRENT loading calls an injected authority resolver
and fails if a current-related review head cannot resolve the actor and claimed
role. The built-in bundle has no reviewer events and therefore cannot claim an
internal, clinical, terminology, citation, license, or equivalence review.

| Domain | Exact subject | Allowed resolved role | Effect |
| --- | --- | --- | --- |
| clinical | ClaimRef | clinician | Required for clinical approval; pharmacist review does not substitute |
| pharmacy | ClaimRef | pharmacist | Separate advisory axis; its `changes_requested` state is displayed but does not lower the non-pharmacy aggregate |
| terminology | ClaimRef | terminologist | Additionally required for terminology support approval |
| internal consistency | SourceRef or ClaimRef | knowledge curator | Internal check only |
| citation verification | CitationRef | curator, librarian, terminologist, or clinician | Every citation must be approved for the claim axis to be approved |
| license decision | SourceRef | rights or compliance officer | Required for local-copy use |
| equivalence | exact manifestation/canonical SourceRef pair | librarian or curator | Required before a current manifestation edge is accepted |

There is no persisted `not_assessed` event. Absence of a trusted decision derives
`not_assessed`; it never means safe. A trusted rejected current-related head
blocks CURRENT selection. HISTORICAL mode preserves untrusted assertions with an
`unverified_assertion` annotation instead of treating them as approval.

## 4. Atomic bundle loading

`bundle_index_v1.json` is the only entry point. It pins each payload by exact
file ID/version, canonical relative path, raw byte size when provided, and
SHA-256. The loader
does not scan the data directory. It validates hashes before JSON parsing, then
validates every envelope and all cross-file references before returning a
registry. A failure returns no partial state.

Filesystem bundle and source-artifact adapters reject absolute paths, empty,
`.` or `..` segments, backslashes, symlinks, and root escape. The built-in bundle
uses `importlib.resources` and the same relative-path protocol.

CURRENT mode requires:

- every selected source, claim, binding, and gap ref to exist and be the exact
  head of its logical ID; an inactive head is retained for history and omitted
  rather than forcing an ineligible current selection;
- current records with eligible lifecycle;
- every selected binding's exact claim and cited sources selected;
- every selected artifact target resolvable and owned by its exact Pathway;
- every current-related review head's actor/role resolvable;
- trusted approvals for local copies and manifestation equivalence when used.

HISTORICAL mode validates schema, version chains, refs, current-ref structure,
and event topology but does not pretend missing runtime artifacts or identities
are valid. It renders them as unresolved or unverified so retired audit history
remains readable. It never opens a registered local third-party blob and labels
that content `not_read_in_historical_mode`.

## 5. GLP1-14D coverage matrix

The following is the **2026-08-13 GLP1-14D migration snapshot**. “Candidate”
means only that the repository declares a source/locator; no citation was
externally checked in this task. “Gap” records why a binding would be dishonest
or why design governance metadata is absent.

The labels below are abbreviated fixture-local display labels. The corresponding
JSON records carry the full typed exact refs (including parent canonical or
Pathway/catalog identity and version); the table is not an alternate identity
registry.

| Fixture-local artifact label | Classification | Claim or gap |
| --- | --- | --- |
| Questionnaire `nausea-present` | candidate binding | `glp1-nausea-collection-rationale` |
| Questionnaire `nausea-severity` | candidate binding | `loinc-81660-3-nausea-severity-terminology` |
| Questionnaire `vomiting-count-24h` | candidate binding | `glp1-vomiting-collection-rationale` |
| Questionnaire `fluid-intake-24h-estimated` | candidate binding | `glp1-fluid-intake-collection-rationale` |
| Questionnaire `abdominal-pain-present` | candidate binding | `glp1-abdominal-pain-collection-rationale` |
| Questionnaire `free-text-report` | design-governance gap | missing owner and formal decision |
| mapping `nausea-present` | design-governance gap | `positive_only` has no governed decision record |
| mapping `nausea-severity` | candidate binding | `loinc-81660-3-nausea-severity-terminology` |
| mapping `vomiting-count-24h` | candidate binding | `loinc-94070-0-emesis-count-24h-terminology` |
| mapping `fluid-intake-24h-estimated` | candidate binding | `loinc-75301-2-fluid-intake-24h-terminology` |
| mapping `abdominal-pain-present` | design-governance gap | `positive_only` has no governed decision record |
| questionnaire binding `nausea-present` | exact-terminology gap | SNOMED version and exact basis absent |
| questionnaire binding `nausea-severity` | candidate binding | `loinc-81660-3-nausea-severity-terminology` |
| questionnaire binding `vomiting-count-24h` | candidate binding | `loinc-94070-0-emesis-count-24h-terminology` |
| questionnaire binding `fluid-intake-24h-estimated` | candidate binding | `loinc-75301-2-fluid-intake-24h-terminology` |
| questionnaire binding `abdominal-pain-present` | exact-terminology gap | SNOMED version and exact basis absent |
| terminology concept `nausea` | exact-terminology gap | legacy sources do not justify the exact SNOMED choice |
| terminology concept `vomiting` | exact-terminology gap | no source locator for SNOMED `249497008` |
| terminology concept `abdominal-pain` | exact-terminology gap | legacy sources do not justify the exact SNOMED choice |
| whole PlanDefinition `urn:uuid:8f384dce…` @ `1.0.0` | design-governance gap | no action ID, owner, or formal decision |

For the original foundation snapshot, the dynamic view derived **20 unique exact artifacts**,
**11 registered relationships**, **9 explicit versioned gaps**, **0 verified
citation relationships**, and **0 claim-review-approved relationships**. The
last count concerns the knowledge claims only; it does not assert artifact or
binding approval. The 11
relationships reuse **7 unique claims** (four collection-rationale and three
terminology-support claims). These are absolute facts about the current fixture,
not loader constraints, assigned sequence numbers, percentages, or a future
Pathway size.

M5-K later adds a reference-only symptom view, one unbound diarrhea claim and
five explicit gaps without changing the 11 bindings. The current dynamic
Pathway view therefore derives **21 unique exact artifacts**, **11 registered
relationships**, and **14 explicit versioned gaps**. These remain fixture
snapshot counts, never a denominator, target or coverage percentage.

The six Questionnaire items, five mapping entries, five questionnaire
terminology bindings, three selected catalog concepts, and one PlanDefinition
are all represented exactly once. The catalog currently contains 49 concepts;
the remaining 46 are simply outside this direct concept-target slice, not
implicitly covered.

Education, clinical-rule, red-flag, and summary-definition targets are zero in
this slice. The pathway's 14-day/daily schedule is intentionally unbound because
the repository has no exact reviewed scheduling basis. Nothing here creates an
executable timing rule.

## 6. Built-in data posture

The bundle registers 15 independent source records and 13 exact legacy aliases.
FDA AccessData and DailyMed remain separate sources; title similarity is not
evidence that two URLs are the same document/version. All built-in source access
is `link_only`, all eight claims are `draft`, all quotes are null, and the event
registry is empty. Consequently all review axes derive `not_assessed`.

The two M5-K additions are official HPO and NCI PRO-CTCAE link-only records.
They are deliberately unbound: no HPO mapping changes the runtime SNOMED
catalog, and no PRO-CTCAE item, translation, score, or GLP1 applicability claim
is embedded or inferred.

The 11 binding records map one claim to one exact artifact target. Reuse is
explicit through repeated exact `ClaimRef`s; a binding does not contain a loose
claim list. The nine open closure gaps authorized for recording in this slice
are first-class versioned governance records, so future resolution appends a
successor instead of erasing the audit trail.

## 7. Read-only developer view

Use an exact Pathway version:

```console
python -m continucare.knowledge GLP1-14D --version 1.0.0
```

Add `--historical` to inspect historical records. Missing versions, unknown
pathways, invalid bundles, unresolved CURRENT targets, or untrusted CURRENT
review assertions return a non-zero exit code. The renderer always shows its
mode, three fixed governance disclaimers, derived coverage, exact sources and
locators, support/limitation text, review state, resolution state, and explicit
gaps. It never reads patient data and never derives runtime eligibility.

## 8. Validation and remaining limitations

Tests cover pinned-byte integrity, exact refs, append-only chains, current-head
selection, scope isolation, deterministic approval requirements, artifact
resolution, authority fail-closed behavior, local-copy safety, dynamic distinct
artifact/relationship/gap counts, rendering, CLI, and the unchanged GLP1
`clinical_rules=[]` fixture.

Known limitations retained deliberately:

- no public source or locator was checked online in this task;
- no real identity provider is wired; the built-in registry contains no review
  claims and the null resolver rejects any asserted review in CURRENT mode;
- no licensed third-party artifact is stored;
- no external evaluation corpus is downloaded or integrated;
- no runtime clinical-rule or publication-governance contract is changed;
- wheel inclusion of the new JSON resources is not claimed by this source-tree
  validation and must be verified before a packaged release;
- source-registry convergence with legacy pathway/terminology projections is a
  later migration, not silently declared complete here.
