"""Application service for analyze → clarify/confirm → Layer-2 draft."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from pydantic import BaseModel, ConfigDict

from continucare.agents.contracts import (
    AgentRunRecord,
    AgentRuntimeOutcome,
    AgentStageTrace,
    AnswerOptionContract,
    CodingContract,
    CandidateOrigin,
    ClarificationKind,
    ClarificationOption,
    ClarificationRequest,
    ContextEvidenceBinding,
    ContextResolution,
    ContextResolutionDecision,
    ConversationContext,
    ConversationTurnContext,
    EnableWhenContract,
    LongTermMemoryItem,
    PendingActionContext,
    PendingActionType,
    QuestionnaireItemContract,
    ReportedSymptomMention,
    SemanticCandidate,
    SemanticResult,
    SemanticStatus,
    SemanticTask,
    SafetyReviewTask,
    SubjectType,
    Temporality,
    TemporalResolutionBasis,
    TerminologyMatchContract,
)
from continucare.agents.registry import AgentRegistry, RegisteredAgent
from continucare.agents.runtime import AgentRuntime
from continucare.care_agent.agent import CareSemanticAgent
from continucare.care_agent.mimo_enhancements import (
    LanguageRewriter,
    MiMoLanguageRewriter,
    MiMoSafetyCritic,
    SafetyCritic,
)
from continucare.care_agent.model_api import (
    SemanticModelAdapter,
    build_model_adapter,
)
from continucare.care_agent.numbers import (
    count_from_evidence,
    millilitres_from_evidence,
    parse_number_token,
)
from continucare.care_agent.safety import SafetyAgent
from continucare.care_agent.subjects import classify_evidence_subject
from continucare.care_agent.temporal import (
    build_temporal_context,
    candidate_temporal_resolution,
)
from continucare.care_engine import CareEngine
from continucare.config import get_settings
from continucare.db import utc_now_iso
from continucare.fhir.questionnaires import flatten_questionnaire_items
from continucare.services.audit import record_audit_event
from continucare.models import ConfirmedAnswerContext, ConfirmedSymptomReport
from continucare.fhir.terminology import UCUM
from continucare.terminology import (
    RepositoryTerminologyBackend,
)
from continucare.terminology.catalog import (
    DYNAMIC_LINK_PREFIX,
    TerminologySearchBackend,
)


SEMANTIC_ALIAS_EXTENSION_URL = (
    "urn:continucare:StructureDefinition:answer-semantic-alias"
)


class SemanticInteraction(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    task: SemanticTask
    result: SemanticResult
    record: AgentRunRecord
    idempotent_replay: bool = False


class CareAgentService:
    """The only bridge from agents into CareEngine, gated by patient action."""

    def __init__(
        self,
        store,
        *,
        care_engine: CareEngine | None = None,
        model_adapter: SemanticModelAdapter | None = None,
        safety_critic: SafetyCritic | None = None,
        language_rewriter: LanguageRewriter | None = None,
        patient_timezone: str | None = None,
        terminology_backend: TerminologySearchBackend | None = None,
    ):
        self.store = store
        self.care_engine = care_engine or CareEngine(store)
        selected_adapter = model_adapter or build_model_adapter()
        config = selected_adapter.config
        self.agent = CareSemanticAgent(model_adapter=selected_adapter)
        selected_critic = safety_critic or MiMoSafetyCritic(config)
        self.safety = SafetyAgent(critic=selected_critic)
        self.language_rewriter = language_rewriter or MiMoLanguageRewriter(config)
        self.patient_timezone = patient_timezone or get_settings().patient_timezone
        self.terminology = terminology_backend or RepositoryTerminologyBackend()
        registry = AgentRegistry()
        registry.register(
            RegisteredAgent(
                name="care_agent",
                version=self.agent.VERSION,
                handler=self.agent,
                allowed_tools=(),
            )
        )
        registry.register(
            RegisteredAgent(
                name="safety_agent",
                version=self.safety.VERSION,
                handler=self.safety,
                allowed_tools=(),
            )
        )
        timeout = self.agent.model_adapter.config.timeout_seconds * 2 + 2.0
        self.runtime = AgentRuntime(registry, timeout_seconds=timeout)

    def analyze(
        self,
        session_id: str,
        message_text: str,
        *,
        received_at: str | None = None,
    ) -> SemanticInteraction:
        session = self._session(session_id)
        questionnaire = self.care_engine.questionnaire_for_session(session)
        task = self._build_task(
            session,
            questionnaire,
            message_text,
            received_at=received_at,
        )
        latest_turn = (
            task.conversation_context.recent_turns[-1]
            if task.conversation_context.recent_turns
            else None
        )
        if latest_turn is not None and latest_turn.message_text == task.message_text:
            latest_record = self.store.get_agent_run(latest_turn.run_id)
            if latest_record is not None:
                latest_result = SemanticResult.model_validate(
                    latest_record.output_json
                )
                latest_temporal = latest_result.temporal_context
                if (
                    latest_temporal is not None
                    and latest_temporal.local_date
                    == task.temporal_context.local_date
                ):
                    return SemanticInteraction(
                        task=task.model_copy(update={"task_id": latest_record.task_id}),
                        result=latest_result,
                        record=latest_record,
                        idempotent_replay=True,
                    )
        existing = self.store.get_agent_run_by_task(task.task_id)
        if existing:
            return SemanticInteraction(
                task=task,
                result=SemanticResult.model_validate(existing.output_json),
                record=existing,
                idempotent_replay=True,
            )

        contextual = self._contextual_resolution(task)
        if contextual is not None:
            result, source_record, candidates, actions, decision, option_id = contextual
            now = utc_now_iso()
            outcome = AgentRuntimeOutcome(
                result=result,
                started_at=now,
                completed_at=now,
                agent_name="care_agent",
                agent_version=self.agent.VERSION,
            )
            record = self._record(task, outcome)
            self.store.save_agent_run(record)
            if candidates:
                self._apply_confirmed(source_record, candidates)
                self._confirmation_audit(
                    source_record,
                    candidates,
                    decision="natural_language_context_accepted",
                )
            for action in actions:
                self.store.resolve_conversation_action(
                    action_id=action.action_id,
                    source_run_id=action.source_run_id,
                    session_id=task.session_id,
                    response_run_id=record.run_id,
                    decision=decision.value,
                    option_id=option_id,
                    response_text=task.message_text,
                    resolved_at=record.completed_at,
                )
            self._context_resolution_audit(task, result)
            return SemanticInteraction(task=task, result=result, record=record)

        contextual_draft = self._contextual_answer_candidate(task)
        if contextual_draft is None:
            care_outcome = self.runtime.run("care_agent", task)
        else:
            now = utc_now_iso()
            care_outcome = AgentRuntimeOutcome(
                result=contextual_draft,
                started_at=now,
                completed_at=now,
                agent_name="care_agent",
                agent_version=self.agent.VERSION,
            )
        draft = care_outcome.result.model_copy(
            update={
                "stage_traces": [
                    *care_outcome.result.stage_traces,
                    self._care_stage_trace(care_outcome),
                ]
            }
        )
        draft = self._resolve_terminology(task, draft)
        safety_outcome = self.runtime.run(
            "safety_agent",
            SafetyReviewTask(
                task_id=f"{task.task_id}:safety",
                semantic_task=task,
                draft=draft,
            ),
        )
        result = self._resolve_missing_items(task, safety_outcome.result)
        result = self._attach_temporal_context(task, result)
        result = self._rewrite_patient_language(task, result)
        result = result.model_copy(
            update={"model_usage": _aggregate_stage_usage(result.stage_traces)}
        )
        outcome = AgentRuntimeOutcome(
            result=result,
            started_at=care_outcome.started_at,
            completed_at=utc_now_iso(),
            agent_name=care_outcome.agent_name,
            agent_version=care_outcome.agent_version,
            idempotent_replay=care_outcome.idempotent_replay,
        )
        record = self._record(task, outcome)
        self.store.save_agent_run(record)
        for candidate in outcome.result.candidates:
            binding = candidate.context_binding
            if binding is None:
                continue
            self.store.resolve_conversation_action(
                action_id=binding.source_action_id,
                source_run_id=binding.source_run_id,
                session_id=task.session_id,
                response_run_id=record.run_id,
                decision=ContextResolutionDecision.ACCEPTED.value,
                response_text=task.message_text,
                resolved_at=record.completed_at,
            )
        record_audit_event(
            self.store,
            patient_id=session.patient_id,
            entity_type="AgentRun",
            entity_id=record.run_id,
            event_type="semantic_analysis_completed",
            actor_type="controlled_care_agent",
            details={
                "session_id": session.session_id,
                "task_id": task.task_id,
                "mode": record.mode,
                "status": record.status,
                "candidate_link_ids": [
                    item.link_id for item in outcome.result.candidates
                ],
                "clarification_count": len(outcome.result.clarifications),
                "safety_violation_count": len(outcome.result.safety_violations),
                "candidate_issues": [
                    {
                        "link_id": issue.link_id,
                        "action": issue.action.value,
                        "reason_codes": issue.reason_codes,
                    }
                    for issue in outcome.result.candidate_issues
                ],
                "agent_stages": [
                    {
                        "stage": trace.stage,
                        "agent_name": trace.agent_name,
                        "mode": trace.mode,
                        "status": trace.status,
                        "prompt_version": trace.prompt_version,
                        "model_usage": trace.model_usage,
                        "latency_ms": trace.latency_ms,
                    }
                    for trace in outcome.result.stage_traces
                ],
                "model_usage": outcome.result.model_usage,
                "provider_request_id": outcome.result.provider_request_id,
                "patient_confirmation_required": True,
                "followup_occurrence_id": task.temporal_context.followup_occurrence_id,
                "patient_timezone": task.temporal_context.patient_timezone,
                "received_at_local": task.temporal_context.received_at_local,
            },
        )
        return SemanticInteraction(
            task=task,
            result=outcome.result,
            record=record,
            idempotent_replay=outcome.idempotent_replay,
        )

    def _contextual_answer_candidate(
        self, task: SemanticTask
    ) -> SemanticResult | None:
        """Bind a concise value to one approved pending question without guessing."""

        actions = task.conversation_context.pending_actions
        if len(actions) != 1:
            return None
        action = actions[0]
        if (
            action.action_type != PendingActionType.CLARIFICATION
            or action.proposed_answer is not None
            or action.link_id is None
        ):
            return None
        question = next(
            (item for item in task.allowed_items if item.link_id == action.link_id),
            None,
        )
        if question is None:
            return None
        parsed = _contextual_answer(task.message_text, question)
        if parsed is None:
            return None
        answer, negated = parsed
        if action.link_id in {"nausea-present", "nausea-severity", "abdominal-pain-present"}:
            temporality = Temporality.CURRENT
        elif action.link_id in {
            "vomiting-count-24h",
            "fluid-intake-24h-estimated",
        }:
            temporality = Temporality.EXPLICIT_24H
        else:
            return None
        evidence = task.message_text
        display = _contextual_answer_display(answer, question)
        patient_message = f"我记录到您回答了“{question.text}”：{display}。请确认这项记录是否正确。"
        candidate = SemanticCandidate(
            candidate_id=(
                "candidate-"
                + uuid5(
                    NAMESPACE_URL,
                    f"{task.task_id}|{action.action_id}|{repr(answer)}",
                ).hex
            ),
            link_id=action.link_id,
            answer=answer,
            questionnaire_code=question.codes[0] if question.codes else None,
            evidence_text=evidence,
            evidence_start=0,
            evidence_end=len(evidence),
            subject=SubjectType.PATIENT,
            temporality=temporality,
            negated=negated,
            context_binding=ContextEvidenceBinding(
                source_run_id=action.source_run_id,
                source_action_id=action.action_id,
            ),
            patient_message=patient_message,
            template_id="confirm_contextual_answer",
        )
        return SemanticResult(
            run_id=f"run-{uuid5(NAMESPACE_URL, task.task_id + '|context-answer').hex}",
            task_id=task.task_id,
            status=SemanticStatus.NEEDS_CONFIRMATION,
            mode="deterministic_context_answer",
            care_agent_version=self.agent.VERSION,
            safety_agent_version="pending",
            language_policy_version="context-answer-v1",
            candidates=[candidate],
            stage_traces=[
                AgentStageTrace(
                    stage="conversation_context_binding",
                    agent_name="care_agent",
                    agent_version=self.agent.VERSION,
                    mode="deterministic_pending_question_binding",
                    status="bound",
                    details={
                        "source_run_id": action.source_run_id,
                        "source_action_id": action.action_id,
                        "link_id": action.link_id,
                    },
                )
            ],
            completed_at=utc_now_iso(),
        )

    def _contextual_resolution(self, task: SemanticTask):
        """Resolve only unambiguous short answers to one pending governed action."""

        actions = task.conversation_context.pending_actions
        if not actions:
            return None
        decision = _short_reply_decision(task.message_text, actions)
        if decision is None:
            return None
        selected = actions
        if len(actions) > 1 and not _all_actions_reply(task.message_text):
            return None
        source_run_ids = {item.source_run_id for item in selected}
        if len(source_run_ids) != 1:
            return None
        source_record = self.store.get_agent_run(source_run_ids.pop())
        if source_record is None:
            return None
        source_result = SemanticResult.model_validate(source_record.output_json)
        accepted: list[SemanticCandidate] = []
        option_id = None
        if decision == ContextResolutionDecision.ACCEPTED:
            for action in selected:
                if action.action_type == PendingActionType.CANDIDATE_CONFIRMATION:
                    candidate = next(
                        (
                            item
                            for item in source_result.candidates
                            if item.candidate_id == action.action_id
                        ),
                        None,
                    )
                else:
                    clarification = next(
                        (
                            item
                            for item in source_result.clarifications
                            if item.clarification_id == action.action_id
                        ),
                        None,
                    )
                    if clarification is None:
                        return None
                    option = next(
                        (
                            item
                            for item in clarification.options
                            if item.accepts_candidate
                        ),
                        None,
                    )
                    candidate = clarification.proposed_candidate
                    if option is None or candidate is None:
                        return None
                    option_id = option.option_id
                    if clarification.kind == ClarificationKind.CONFIRM_TIME_WINDOW:
                        candidate = candidate.model_copy(
                            update={"temporality": Temporality.EXPLICIT_24H}
                        )
                    elif clarification.kind == ClarificationKind.CONFIRM_CURRENT:
                        candidate = candidate.model_copy(
                            update={"temporality": Temporality.CURRENT}
                        )
                if candidate is None:
                    return None
                prior_temporal = source_result.temporal_context
                if prior_temporal is not None:
                    candidate = candidate.model_copy(
                        update={
                            "effective_time": candidate_temporal_resolution(
                                candidate,
                                prior_temporal,
                                basis=(
                                    TemporalResolutionBasis.PATIENT_CONFIRMATION
                                    if action.action_type
                                    == PendingActionType.CLARIFICATION
                                    else TemporalResolutionBasis.EXPLICIT_PATIENT_TEXT
                                ),
                                inherited_from_action_id=action.action_id,
                            )
                        }
                    )
                prior_task = self._task_for_record(source_record)
                if self.safety.review_candidate(prior_task, candidate):
                    return None
                accepted.append(candidate)
        elif decision == ContextResolutionDecision.REJECTED:
            option_id = "no"
        else:
            option_id = "unsure"

        applied_links = sorted({item.link_id for item in accepted})
        explanation = {
            ContextResolutionDecision.ACCEPTED: (
                "已将这句话作为对上一轮待确认内容的明确肯定回答处理。"
            ),
            ContextResolutionDecision.REJECTED: (
                "已将这句话作为对上一轮待确认内容的否定回答处理，未写入候选。"
            ),
            ContextResolutionDecision.UNSURE: (
                "已记录患者暂不确定，未写入候选。"
            ),
        }[decision]
        resolution = ContextResolution(
            decision=decision,
            source_run_id=source_record.run_id,
            action_ids=[item.action_id for item in selected],
            applied_link_ids=applied_links,
            response_text=task.message_text,
            explanation=explanation,
        )
        result = SemanticResult(
            run_id=f"run-{uuid5(NAMESPACE_URL, task.task_id + '|context').hex}",
            task_id=task.task_id,
            status=SemanticStatus.CONTEXT_RESOLVED,
            mode="deterministic_context_resolver",
            care_agent_version=self.agent.VERSION,
            safety_agent_version=self.safety.VERSION,
            language_policy_version="context-resolution-v1",
            temporal_context=task.temporal_context,
            context_resolution=resolution,
            stage_traces=[
                AgentStageTrace(
                    stage="conversation_context_resolution",
                    agent_name="care_agent",
                    agent_version=self.agent.VERSION,
                    mode="deterministic_pending_action_binding",
                    status=decision.value,
                    details={
                        "source_run_id": source_record.run_id,
                        "action_ids": resolution.action_ids,
                        "applied_link_ids": applied_links,
                    },
                )
            ],
            completed_at=utc_now_iso(),
        )
        return result, source_record, accepted, selected, decision, option_id

    def _resolve_terminology(
        self, task: SemanticTask, draft: SemanticResult
    ) -> SemanticResult:
        """Retrieve every code from the versioned catalog before Safety review."""

        decorated: list[SemanticCandidate] = []
        handled_concepts: set[str] = set()
        for candidate in draft.candidates:
            match = self.terminology.catalog.match_for_questionnaire(
                link_id=candidate.link_id,
                coding=candidate.questionnaire_code,
                matched_text=candidate.evidence_text,
            )
            candidate = candidate.model_copy(update={"terminology_match": match})
            decorated.append(candidate)
            handled_concepts.add(match.concept_id)

        existing_clarifications: list[ClarificationRequest] = []
        for clarification in draft.clarifications:
            proposed = clarification.proposed_candidate
            if proposed is not None:
                match = self.terminology.catalog.match_for_questionnaire(
                    link_id=proposed.link_id,
                    coding=proposed.questionnaire_code,
                    matched_text=proposed.evidence_text,
                )
                proposed = proposed.model_copy(update={"terminology_match": match})
                clarification = clarification.model_copy(
                    update={"proposed_candidate": proposed}
                )
                handled_concepts.add(match.concept_id)
            existing_clarifications.append(clarification)

        if draft.status == SemanticStatus.BLOCKED:
            return draft.model_copy(
                update={
                    "candidates": decorated,
                    "clarifications": existing_clarifications,
                    "reported_symptom_mentions": [],
                    "stage_traces": [
                        *draft.stage_traces,
                        AgentStageTrace(
                            stage="terminology_resolution",
                            agent_name="terminology_resolver",
                            agent_version=(
                                f"repository-catalog-{self.terminology.catalog.version}"
                            ),
                            mode="blocked_input_no_lookup",
                            status="skipped",
                        ),
                    ],
                }
            )

        mentions_by_span: dict[tuple[int, int], ReportedSymptomMention] = {}
        for mention in draft.reported_symptom_mentions:
            key = (mention.evidence_start, mention.evidence_end)
            current = mentions_by_span.get(key)
            if current is None or (
                not self.terminology.search(current.symptom_text)
                and self.terminology.search(mention.symptom_text)
            ):
                mentions_by_span[key] = mention
        mentions = list(mentions_by_span.values())
        known_spans = {(item.evidence_start, item.evidence_end) for item in mentions}
        for start, end, _ in self.terminology.scan(task.message_text):
            evidence = task.message_text[start:end]
            identity = (start, end)
            if identity in known_spans:
                continue
            mentions.append(
                ReportedSymptomMention(
                    mention_id=(
                        "symptom-mention-"
                        + uuid5(
                            NAMESPACE_URL,
                            f"{task.task_id}|{start}|{end}|{evidence}",
                        ).hex
                    ),
                    symptom_text=evidence,
                    evidence_text=evidence,
                    evidence_start=start,
                    evidence_end=end,
                    subject=classify_evidence_subject(task.message_text, start, end),
                    temporality=_mention_temporality(task, start, end),
                    negated=_mention_negated(task.message_text, start),
                )
            )
            known_spans.add(identity)

        new_clarifications: list[ClarificationRequest] = []
        unresolved: list[ReportedSymptomMention] = []
        emitted_concepts = set(handled_concepts)
        for mention in sorted(mentions, key=lambda item: item.evidence_start):
            if (
                mention.subject != SubjectType.PATIENT
                or mention.negated
                or mention.temporality == Temporality.HISTORICAL
            ):
                continue
            catalog_matches = self.terminology.search(mention.symptom_text)
            matches = [
                item
                for item in catalog_matches
                if item.concept_id not in handled_concepts
            ]
            if not matches:
                # Try the full verbatim span once; otherwise keep it for the
                # patient-visible, human-review path rather than inventing a code.
                evidence_matches = self.terminology.search(mention.evidence_text)
                catalog_matches = [*catalog_matches, *evidence_matches]
                matches = [
                    item
                    for item in evidence_matches
                    if item.concept_id not in handled_concepts
                ]
            matches = [
                item for item in matches if item.concept_id not in emitted_concepts
            ]
            matches = [
                item for item in matches if self._symptom_match_is_recordable(task, item)
            ]
            if not matches:
                if not catalog_matches:
                    unresolved.append(mention)
                continue
            if len(matches) > 1:
                new_clarifications.append(
                    self._terminology_clarification(task, mention, matches)
                )
                emitted_concepts.update(item.concept_id for item in matches)
                continue
            match = matches[0]
            candidate = self._symptom_candidate(task, mention, match)
            emitted_concepts.add(match.concept_id)
            if candidate.temporality == Temporality.UNSPECIFIED:
                new_clarifications.append(
                    ClarificationRequest(
                        clarification_id=(
                            "clarify-"
                            + uuid5(
                                NAMESPACE_URL,
                                f"{candidate.candidate_id}|current",
                            ).hex
                        ),
                        kind=ClarificationKind.CONFIRM_CURRENT,
                        prompt=(
                            f"我从术语目录匹配到“{match.preferred_zh}”。"
                            "这是您现在仍有的情况吗？"
                        ),
                        proposed_candidate=candidate,
                        reported_symptom=mention,
                        options=[
                            ClarificationOption(
                                option_id="yes_current",
                                label="是，现在仍有",
                                accepts_candidate=True,
                            ),
                            ClarificationOption(option_id="no", label="不是现在"),
                            ClarificationOption(option_id="unsure", label="不确定"),
                        ],
                    )
                )
            else:
                decorated.append(candidate)

        all_clarifications = [*existing_clarifications, *new_clarifications]
        if draft.status == SemanticStatus.BLOCKED:
            status = SemanticStatus.BLOCKED
        elif all_clarifications:
            status = SemanticStatus.NEEDS_CLARIFICATION
        elif decorated:
            status = SemanticStatus.NEEDS_CONFIRMATION
        else:
            status = SemanticStatus.NO_MATCH
        trace = AgentStageTrace(
            stage="terminology_resolution",
            agent_name="terminology_resolver",
            agent_version=f"repository-catalog-{self.terminology.catalog.version}",
            mode="deterministic_repository_lookup",
            status=status.value,
            details={
                "catalog_id": self.terminology.catalog.catalog_id,
                "catalog_version": self.terminology.catalog.version,
                "matched_candidate_count": len(decorated),
                "clarification_count": len(new_clarifications),
                "unresolved_mention_count": len(unresolved),
            },
        )
        ignored = list(draft.ignored_reasons)
        if unresolved:
            ignored.append(
                "存在未命中当前 GLP-1 术语目录的患者原话；已保留，待术语人员或医生复核。"
            )
        return draft.model_copy(
            update={
                "status": status,
                "candidates": decorated,
                "clarifications": all_clarifications,
                "reported_symptom_mentions": unresolved,
                "ignored_reasons": list(dict.fromkeys(ignored)),
                "stage_traces": [*draft.stage_traces, trace],
            }
        )

    def _symptom_match_is_recordable(
        self, task: SemanticTask, match: TerminologyMatchContract
    ) -> bool:
        binding = next(
            (
                item
                for item in self.terminology.catalog.questionnaire_bindings
                if item.symptom_concept_id == match.concept_id
            ),
            None,
        )
        if binding is None:
            return True
        question = next(
            (item for item in task.allowed_items if item.link_id == binding.link_id),
            None,
        )
        # A phrase alone cannot safely supply a count, quantity, or choice.
        return question is not None and question.item_type == "boolean"

    def _symptom_candidate(
        self,
        task: SemanticTask,
        mention: ReportedSymptomMention,
        match: TerminologyMatchContract,
        *,
        force_current: bool = False,
    ) -> SemanticCandidate:
        binding = next(
            (
                item
                for item in self.terminology.catalog.questionnaire_bindings
                if item.symptom_concept_id == match.concept_id
            ),
            None,
        )
        allowed = {item.link_id: item for item in task.allowed_items}
        if binding is not None and allowed[binding.link_id].item_type == "boolean":
            link_id = binding.link_id
            target_code = allowed[link_id].codes[0]
            origin = CandidateOrigin.PATHWAY_MONITORED
            match = self.terminology.catalog.match_for_questionnaire(
                link_id=link_id,
                coding=target_code,
                matched_text=mention.evidence_text,
            ).model_copy(
                update={
                    "matched_alias": match.matched_alias,
                    "match_method": match.match_method,
                }
            )
        else:
            link_id = f"{DYNAMIC_LINK_PREFIX}{match.concept_id}"
            target_code = match.target_coding
            origin = (
                CandidateOrigin.PATHWAY_MONITORED
                if binding is not None
                else CandidateOrigin.PATIENT_REPORTED_NEW
            )
        temporality = Temporality.CURRENT if force_current else mention.temporality
        time_label = {
            Temporality.CURRENT: "现在的",
            Temporality.EXPLICIT_24H: "过去24小时内的",
            Temporality.HISTORICAL: "此前的",
            Temporality.UNSPECIFIED: "",
        }[temporality]
        return SemanticCandidate(
            candidate_id=(
                "candidate-"
                + uuid5(
                    NAMESPACE_URL,
                    f"{task.task_id}|{mention.mention_id}|{match.concept_id}",
                ).hex
            ),
            link_id=link_id,
            answer=True,
            questionnaire_code=target_code,
            evidence_text=mention.evidence_text,
            evidence_start=mention.evidence_start,
            evidence_end=mention.evidence_end,
            subject=mention.subject,
            temporality=temporality,
            negated=False,
            patient_message=(
                f"我从 GLP-1 术语目录匹配到“{match.preferred_zh}”"
                f"（SNOMED CT {match.coding.code}），请确认这是您{time_label}情况。"
            ),
            template_id="confirm_terminology_match",
            origin=origin,
            terminology_match=match,
        )

    def _terminology_clarification(
        self,
        task: SemanticTask,
        mention: ReportedSymptomMention,
        matches: list[TerminologyMatchContract],
    ) -> ClarificationRequest:
        options = [
            ClarificationOption(
                option_id=f"concept::{item.concept_id}",
                label=item.preferred_zh,
                accepts_candidate=True,
                terminology_match=item,
            )
            for item in matches
        ]
        options.append(ClarificationOption(option_id="unsure", label="都不确定"))
        return ClarificationRequest(
            clarification_id=(
                "clarify-"
                + uuid5(
                    NAMESPACE_URL,
                    f"{task.task_id}|terminology|{mention.mention_id}",
                ).hex
            ),
            kind=ClarificationKind.TERMINOLOGY_DISAMBIGUATION,
            prompt=(
                f"您说的“{mention.evidence_text}”可能对应几种不同记录。"
                "请选择当前最符合的一项："
            ),
            target_link_id=None,
            options=options,
            reported_symptom=mention,
        )

    def _attach_temporal_context(
        self, task: SemanticTask, result: SemanticResult
    ) -> SemanticResult:
        def enrich(candidate: SemanticCandidate) -> SemanticCandidate:
            binding = candidate.context_binding
            return candidate.model_copy(
                update={
                    "effective_time": candidate_temporal_resolution(
                        candidate,
                        task.temporal_context,
                        basis=(
                            TemporalResolutionBasis.PENDING_QUESTION
                            if binding is not None
                            else TemporalResolutionBasis.EXPLICIT_PATIENT_TEXT
                        ),
                        inherited_from_action_id=(
                            binding.source_action_id
                            if binding is not None
                            else None
                        ),
                    )
                }
            )

        candidates = [enrich(item) for item in result.candidates]
        clarifications = [
            item.model_copy(
                update={
                    "proposed_candidate": (
                        enrich(item.proposed_candidate)
                        if item.proposed_candidate is not None
                        else None
                    )
                }
            )
            for item in result.clarifications
        ]
        return result.model_copy(
            update={
                "candidates": candidates,
                "clarifications": clarifications,
                "temporal_context": task.temporal_context,
                "stage_traces": [
                    *result.stage_traces,
                    AgentStageTrace(
                        stage="temporal_context",
                        agent_name="care_agent",
                        agent_version=self.agent.VERSION,
                        mode="deterministic_patient_local_time",
                        status="resolved",
                        details={
                            "patient_timezone": task.temporal_context.patient_timezone,
                            "local_date": task.temporal_context.local_date,
                            "followup_occurrence_id": (
                                task.temporal_context.followup_occurrence_id
                            ),
                            "detected_mentions": [
                                item.expression
                                for item in task.temporal_context.detected_mentions
                            ],
                        },
                    ),
                ],
            }
        )

    def _context_resolution_audit(
        self, task: SemanticTask, result: SemanticResult
    ) -> None:
        resolution = result.context_resolution
        if resolution is None:
            return
        record_audit_event(
            self.store,
            patient_id=task.patient_id,
            entity_type="AgentRun",
            entity_id=result.run_id,
            event_type="conversation_context_resolved",
            actor_type="deterministic_context_resolver",
            details={
                "session_id": task.session_id,
                "source_run_id": resolution.source_run_id,
                "action_ids": resolution.action_ids,
                "decision": resolution.decision.value,
                "applied_link_ids": resolution.applied_link_ids,
                "followup_occurrence_id": (
                    task.temporal_context.followup_occurrence_id
                ),
                "received_at_local": task.temporal_context.received_at_local,
            },
        )

    def _care_stage_trace(
        self, outcome: AgentRuntimeOutcome
    ) -> AgentStageTrace:
        result = outcome.result
        config = self.agent.model_adapter.config
        model_mode = result.mode == "model_api:xiaomi_mimo"
        return AgentStageTrace(
            stage="care_extraction",
            agent_name="care_agent",
            agent_version=self.agent.VERSION,
            mode=result.mode,
            status=result.status.value,
            model_provider=config.provider if model_mode else None,
            model_name=config.model_name if model_mode else None,
            prompt_version=config.prompt_version if model_mode else None,
            model_usage=result.model_usage if model_mode else None,
            provider_request_id=result.provider_request_id if model_mode else None,
            latency_ms=_elapsed_ms(outcome.started_at, outcome.completed_at),
        )

    def _resolve_missing_items(
        self, task: SemanticTask, result: SemanticResult
    ) -> SemanticResult:
        supported = sorted(
            {
                item.link_id
                for item in result.missing_items
                if item.status.value == "supported"
            }
        )
        focused = None
        focused_attempt_failed = False
        if supported:
            try:
                focused = self.agent.extract_focused(task, supported)
            except Exception as exc:
                focused_attempt_failed = True
                result = result.model_copy(
                    update={
                        "ignored_reasons": [
                            *result.ignored_reasons,
                            f"focused_reextract_fallback:{type(exc).__name__}",
                        ],
                        "stage_traces": [
                            *result.stage_traces,
                            AgentStageTrace(
                                stage="focused_reextract",
                                agent_name="care_agent",
                                agent_version=self.agent.VERSION,
                                mode="questionnaire_clarification_fallback",
                                status="failed",
                                details={
                                    "requested_link_ids": supported,
                                    "error_type": type(exc).__name__,
                                },
                            ),
                        ],
                    }
                )
            if focused is None and not focused_attempt_failed:
                result = result.model_copy(
                    update={
                        "stage_traces": [
                            *result.stage_traces,
                            AgentStageTrace(
                                stage="focused_reextract",
                                agent_name="care_agent",
                                agent_version=self.agent.VERSION,
                                mode="questionnaire_clarification_fallback",
                                status="unavailable",
                                details={"requested_link_ids": supported},
                            ),
                        ]
                    }
                )
        if focused is not None:
            focused = focused.model_copy(
                update={
                    "candidates": [
                        item.model_copy(
                            update={
                                "terminology_match": (
                                    self.terminology.catalog.match_for_questionnaire(
                                        link_id=item.link_id,
                                        coding=item.questionnaire_code,
                                        matched_text=item.evidence_text,
                                    )
                                )
                            }
                        )
                        for item in focused.candidates
                    ],
                    "clarifications": [
                        clarification.model_copy(
                            update={
                                "proposed_candidate": (
                                    clarification.proposed_candidate.model_copy(
                                        update={
                                            "terminology_match": (
                                                self.terminology.catalog.match_for_questionnaire(
                                                    link_id=clarification.proposed_candidate.link_id,
                                                    coding=clarification.proposed_candidate.questionnaire_code,
                                                    matched_text=clarification.proposed_candidate.evidence_text,
                                                )
                                            )
                                        }
                                    )
                                    if clarification.proposed_candidate is not None
                                    else None
                                )
                            }
                        )
                        for clarification in focused.clarifications
                    ],
                }
            )
            existing_links = {item.link_id for item in result.candidates}
            merged = result.model_copy(
                update={
                    "candidates": [
                        *result.candidates,
                        *[
                            item
                            for item in focused.candidates
                            if item.link_id not in existing_links
                        ],
                    ],
                    "clarifications": [
                        *result.clarifications,
                        *focused.clarifications,
                    ],
                    "candidate_issues": [
                        *result.candidate_issues,
                        *focused.candidate_issues,
                    ],
                }
            )
            merged = self.safety.review(task, merged)
            resolved_links = {item.link_id for item in merged.candidates}
            resolved_links.update(
                item.proposed_candidate.link_id
                for item in merged.clarifications
                if item.proposed_candidate is not None
            )
            trace = AgentStageTrace(
                stage="focused_reextract",
                agent_name="care_agent",
                agent_version=self.agent.VERSION,
                mode=focused.mode,
                status=(
                    "resolved"
                    if set(supported) <= resolved_links
                    else "partial"
                ),
                model_provider="xiaomi_mimo",
                model_name=self.agent.model_adapter.config.model_name,
                prompt_version=self.agent.model_adapter.config.prompt_version,
                model_usage=focused.model_usage,
                provider_request_id=focused.provider_request_id,
                details={
                    "requested_link_ids": supported,
                    "resolved_link_ids": sorted(set(supported) & resolved_links),
                },
            )
            result = merged.model_copy(
                update={"stage_traces": [*result.stage_traces, trace]}
            )
        return self._append_missing_clarifications(task, result)

    def _append_missing_clarifications(
        self, task: SemanticTask, result: SemanticResult
    ) -> SemanticResult:
        represented = {item.link_id for item in result.candidates}
        represented.update(
            item.proposed_candidate.link_id
            for item in result.clarifications
            if item.proposed_candidate is not None
        )
        pending = [
            item
            for item in result.missing_items
            if item.status.value in {"supported", "ambiguous"}
            and item.link_id not in represented
        ]
        if not pending:
            return result
        questions = {item.link_id: item for item in task.allowed_items}
        clarifications = list(result.clarifications)
        for finding in pending:
            question = questions[finding.link_id]
            clarifications.append(
                ClarificationRequest(
                    clarification_id=f"clarify-missing-{task.task_id}-{finding.link_id}",
                    kind=ClarificationKind.MISSING_DEPENDENT_VALUE,
                    target_link_id=finding.link_id,
                    prompt=(
                        f"为了避免漏记，还需要请您确认：{question.text}？"
                    ),
                    options=[
                        ClarificationOption(
                            option_id="no",
                            label="在下方完整问卷中填写",
                        ),
                        ClarificationOption(
                            option_id="unsure",
                            label="我暂时不确定",
                        ),
                    ],
                )
            )
        return result.model_copy(
            update={
                "status": SemanticStatus.NEEDS_CLARIFICATION,
                "clarifications": clarifications,
            }
        )

    def _rewrite_patient_language(
        self, task: SemanticTask, result: SemanticResult
    ) -> SemanticResult:
        if not self.language_rewriter.configured or result.status == SemanticStatus.BLOCKED:
            trace = AgentStageTrace(
                stage="language_rewrite",
                agent_name="care_agent",
                agent_version=self.agent.VERSION,
                mode="deterministic_template",
                status="skipped",
            )
            return result.model_copy(
                update={"stage_traces": [*result.stage_traces, trace]}
            )
        try:
            outcome = self.language_rewriter.rewrite(task, result)
        except Exception as exc:
            trace = AgentStageTrace(
                stage="language_rewrite",
                agent_name="care_agent",
                agent_version=self.agent.VERSION,
                mode="deterministic_template_fallback",
                status="failed",
                details={"error_type": type(exc).__name__},
            )
            return result.model_copy(
                update={
                    "ignored_reasons": [
                        *result.ignored_reasons,
                        f"language_rewriter_fallback:{type(exc).__name__}",
                    ],
                    "stage_traces": [*result.stage_traces, trace],
                }
            )

        candidates = [
            item.model_copy(
                update={
                    "patient_message": outcome.rewrites.get(
                        item.candidate_id, item.patient_message
                    )
                }
            )
            for item in result.candidates
        ]
        clarifications = [
            item.model_copy(
                update={
                    "prompt": outcome.rewrites.get(
                        item.clarification_id, item.prompt
                    )
                }
            )
            for item in result.clarifications
        ]
        trace = AgentStageTrace(
            stage="language_rewrite",
            agent_name="care_agent",
            agent_version=self.agent.VERSION,
            mode=outcome.mode,
            status="completed",
            model_provider="xiaomi_mimo",
            model_name=self.agent.model_adapter.config.model_name,
            prompt_version=outcome.prompt_version,
            model_usage=outcome.model_usage,
            provider_request_id=outcome.provider_request_id,
            latency_ms=outcome.latency_ms,
            details={
                "rewritten_count": len(outcome.rewrites),
                "template_fallback_count": len(outcome.rejected_reasons),
                "fallback_reasons": outcome.rejected_reasons,
                "attempt_count": outcome.attempt_count,
            },
        )
        reasons = list(result.ignored_reasons)
        if outcome.rejected_reasons:
            reasons.append("language_rewriter_partial_template_fallback")
        return result.model_copy(
            update={
                "candidates": candidates,
                "clarifications": clarifications,
                "ignored_reasons": reasons,
                "stage_traces": [*result.stage_traces, trace],
            }
        )

    def confirm_candidates(
        self, run_id: str, candidate_ids: list[str]
    ):
        if not candidate_ids:
            raise ValueError("请选择至少一项记录后再确认")
        record, result = self._stored_result(run_id)
        available = {item.candidate_id: item for item in result.candidates}
        unknown = set(candidate_ids) - set(available)
        if unknown:
            raise ValueError("确认内容不属于该次安全审核结果")
        candidates = [available[item_id] for item_id in candidate_ids]
        session = self._apply_confirmed(record, candidates)
        self._confirmation_audit(record, candidates, decision="accepted")
        for candidate in candidates:
            self._close_action(
                record,
                candidate.candidate_id,
                decision=ContextResolutionDecision.ACCEPTED,
            )
        return session

    def reject_candidates(self, run_id: str, candidate_ids: list[str]) -> None:
        record, result = self._stored_result(run_id)
        available = {item.candidate_id: item for item in result.candidates}
        candidates = [available[item_id] for item_id in candidate_ids if item_id in available]
        self._confirmation_audit(record, candidates, decision="rejected")
        for candidate in candidates:
            self._close_action(
                record,
                candidate.candidate_id,
                decision=ContextResolutionDecision.REJECTED,
            )

    def confirm_original_text(self, run_id: str):
        """Patient explicitly chooses to retain only their verbatim report."""

        record, result = self._stored_result(run_id)
        if result.status.value == "blocked":
            raise ValueError("指令型文本不能作为患者健康原话保存")
        session = self._apply_confirmed(record, [])
        self._confirmation_audit(record, [], decision="verbatim_only_accepted")
        for action_id in [
            *[item.candidate_id for item in result.candidates],
            *[item.clarification_id for item in result.clarifications],
        ]:
            self._close_action(
                record,
                action_id,
                decision=ContextResolutionDecision.REJECTED,
            )
        return session

    def resolve_clarification(
        self, run_id: str, clarification_id: str, option_id: str
    ):
        record, result = self._stored_result(run_id)
        clarification = next(
            (
                item
                for item in result.clarifications
                if item.clarification_id == clarification_id
            ),
            None,
        )
        if clarification is None:
            raise ValueError("澄清问题不属于该次分析结果")
        option = next(
            (item for item in clarification.options if item.option_id == option_id),
            None,
        )
        if option is None:
            raise ValueError("澄清选项无效")
        if clarification.kind == ClarificationKind.TERMINOLOGY_DISAMBIGUATION:
            mention = clarification.reported_symptom
            if option.terminology_match is None or mention is None:
                self._confirmation_audit(
                    record, [], decision=f"terminology_{option_id}"
                )
                self._close_action(
                    record,
                    clarification.clarification_id,
                    decision=ContextResolutionDecision.UNSURE,
                    option_id=option_id,
                )
                return self._session(record.session_id)
            task = self._task_for_record(record)
            candidate = self._symptom_candidate(
                task,
                mention,
                option.terminology_match,
                force_current=True,
            )
            candidate = candidate.model_copy(
                update={
                    "effective_time": candidate_temporal_resolution(
                        candidate,
                        task.temporal_context,
                        basis=TemporalResolutionBasis.PATIENT_CONFIRMATION,
                        inherited_from_action_id=clarification.clarification_id,
                    )
                }
            )
            errors = self.safety.review_candidate(task, candidate)
            if errors:
                raise ValueError("术语澄清后的候选未通过安全校验")
            session = self._apply_confirmed(record, [candidate])
            self._confirmation_audit(
                record, [candidate], decision="terminology_clarification_accepted"
            )
            self._close_action(
                record,
                clarification.clarification_id,
                decision=ContextResolutionDecision.ACCEPTED,
                option_id=option_id,
            )
            return session
        candidate = clarification.proposed_candidate
        if not option.accepts_candidate or candidate is None:
            self._confirmation_audit(
                record,
                [candidate] if candidate else [],
                decision=f"clarification_{option_id}",
            )
            self._close_action(
                record,
                clarification.clarification_id,
                decision=(
                    ContextResolutionDecision.UNSURE
                    if option_id == "unsure"
                    else ContextResolutionDecision.REJECTED
                ),
                option_id=option_id,
            )
            return self._session(record.session_id)

        if clarification.kind.value == "confirm_time_window":
            candidate = candidate.model_copy(
                update={"temporality": Temporality.EXPLICIT_24H}
            )
        elif clarification.kind.value == "confirm_current":
            candidate = candidate.model_copy(update={"temporality": Temporality.CURRENT})
        task = self._task_for_record(record)
        errors = self.safety.review_candidate(task, candidate)
        if errors:
            raise ValueError("澄清后的候选未通过安全校验")
        session = self._apply_confirmed(record, [candidate])
        self._confirmation_audit(record, [candidate], decision="clarification_accepted")
        self._close_action(
            record,
            clarification.clarification_id,
            decision=ContextResolutionDecision.ACCEPTED,
            option_id=option_id,
        )
        return session

    def _apply_confirmed(
        self, record: AgentRunRecord, candidates: list[SemanticCandidate]
    ):
        session = self._session(record.session_id)
        if session.patient_id != record.patient_id:
            raise ValueError("Agent 运行记录与随访会话患者不一致")
        answers: dict[str, Any] = dict(session.answers)
        for candidate in candidates:
            if not candidate.link_id.startswith(DYNAMIC_LINK_PREFIX):
                answers[candidate.link_id] = candidate.answer
        original = record.input_text.strip()
        if original:
            previous = str(answers.get("free-text-report", "")).strip()
            lines = previous.splitlines() if previous else []
            if original not in lines:
                answers["free-text-report"] = "\n".join([*lines, original])
        updated = self.care_engine.save_draft(session.session_id, answers)
        result = SemanticResult.model_validate(record.output_json)
        temporal = result.temporal_context
        for candidate in candidates:
            effective = candidate.effective_time
            if effective is None and temporal is not None:
                effective = candidate_temporal_resolution(candidate, temporal)
            now = utc_now_iso()
            terminology_match = (
                candidate.terminology_match.model_dump(mode="json")
                if candidate.terminology_match is not None
                else None
            )
            if candidate.link_id.startswith(DYNAMIC_LINK_PREFIX):
                if candidate.terminology_match is None:
                    raise ValueError("患者自述症状缺少经过校验的术语匹配")
                match = candidate.terminology_match
                self.store.save_confirmed_symptom_report(
                    ConfirmedSymptomReport(
                        report_id=(
                            "symptom-report-"
                            + uuid5(
                                NAMESPACE_URL,
                                f"{record.run_id}|{candidate.candidate_id}",
                            ).hex
                        ),
                        session_id=session.session_id,
                        concept_id=match.concept_id,
                        preferred_zh=match.preferred_zh,
                        coding=match.coding.model_dump(mode="json"),
                        terminology_match=terminology_match,
                        source_kind=candidate.origin.value,
                        source_run_id=record.run_id,
                        evidence_text=candidate.evidence_text,
                        evidence_start=candidate.evidence_start,
                        evidence_end=candidate.evidence_end,
                        followup_occurrence_id=(
                            temporal.followup_occurrence_id
                            if temporal is not None
                            else f"occurrence-{session.session_id}"
                        ),
                        patient_timezone=(
                            temporal.patient_timezone
                            if temporal is not None
                            else self.patient_timezone
                        ),
                        reported_at=(
                            temporal.received_at_local
                            if temporal is not None
                            else record.completed_at
                        ),
                        effective_start=(
                            effective.effective_start
                            if effective is not None
                            else None
                        ),
                        effective_end=(
                            effective.effective_end if effective is not None else None
                        ),
                        temporal_kind=(
                            effective.kind.value if effective is not None else None
                        ),
                        created_at=now,
                    )
                )
                continue
            self.store.save_confirmed_answer_context(
                ConfirmedAnswerContext(
                    answer_context_id=(
                        "answer-context-"
                        + uuid5(
                            NAMESPACE_URL,
                            f"{record.run_id}|{candidate.candidate_id}|{candidate.link_id}",
                        ).hex
                    ),
                    session_id=session.session_id,
                    link_id=candidate.link_id,
                    answer=candidate.answer,
                    source_run_id=record.run_id,
                    followup_occurrence_id=(
                        temporal.followup_occurrence_id
                        if temporal is not None
                        else f"occurrence-{session.session_id}"
                    ),
                    patient_timezone=(
                        temporal.patient_timezone
                        if temporal is not None
                        else self.patient_timezone
                    ),
                    reported_at=(
                        temporal.received_at_local
                        if temporal is not None
                        else record.completed_at
                    ),
                    effective_start=(
                        effective.effective_start if effective is not None else None
                    ),
                    effective_end=(
                        effective.effective_end if effective is not None else None
                    ),
                    temporal_kind=(
                        effective.kind.value if effective is not None else None
                    ),
                    resolution_basis=(
                        effective.basis.value if effective is not None else None
                    ),
                    raw_text=record.input_text,
                    terminology_match=terminology_match,
                    created_at=now,
                )
            )
        return updated

    def _close_action(
        self,
        record: AgentRunRecord,
        action_id: str,
        *,
        decision: ContextResolutionDecision,
        option_id: str | None = None,
    ) -> None:
        self.store.resolve_conversation_action(
            action_id=action_id,
            source_run_id=record.run_id,
            session_id=record.session_id,
            decision=decision.value,
            option_id=option_id,
            resolved_at=utc_now_iso(),
        )

    def _confirmation_audit(self, record, candidates, *, decision: str) -> None:
        record_audit_event(
            self.store,
            patient_id=record.patient_id,
            entity_type="AgentRun",
            entity_id=record.run_id,
            event_type="semantic_candidate_patient_decision",
            actor_type="synthetic_patient",
            details={
                "session_id": record.session_id,
                "decision": decision,
                "candidate_ids": [item.candidate_id for item in candidates],
                "confirmed_link_ids": [item.link_id for item in candidates],
                "clinical_assessment": "not_assessed",
            },
        )

    def _record(
        self, task: SemanticTask, outcome: AgentRuntimeOutcome
    ) -> AgentRunRecord:
        config = self.agent.model_adapter.config
        return AgentRunRecord(
            run_id=outcome.result.run_id,
            task_id=task.task_id,
            patient_id=task.patient_id,
            session_id=task.session_id,
            agent_name=outcome.agent_name,
            agent_version=outcome.agent_version,
            mode=outcome.result.mode,
            input_text=task.message_text,
            input_hash=hashlib.sha256(task.message_text.encode("utf-8")).hexdigest(),
            output_json=outcome.result.model_dump(mode="json"),
            status=outcome.result.status.value,
            model_provider=(
                config.provider if self.agent.model_adapter.configured else None
            ),
            model_name=(
                config.model_name if self.agent.model_adapter.configured else None
            ),
            prompt_version=config.prompt_version,
            started_at=outcome.started_at,
            completed_at=outcome.completed_at,
        )

    def _stored_result(self, run_id: str):
        record = self.store.get_agent_run(run_id)
        if record is None:
            raise ValueError("Agent 运行记录不存在")
        return record, SemanticResult.model_validate(record.output_json)

    def _task_for_record(self, record: AgentRunRecord) -> SemanticTask:
        session = self._session(record.session_id)
        questionnaire = self.care_engine.questionnaire_for_session(session)
        result = SemanticResult.model_validate(record.output_json)
        return self._build_task(
            session,
            questionnaire,
            record.input_text,
            task_id=record.task_id,
            received_at=(
                result.temporal_context.received_at_utc
                if result.temporal_context is not None
                else record.started_at
            ),
        )

    def _session(self, session_id: str):
        session = self.store.get_care_session(session_id)
        if session is None:
            raise ValueError("随访会话不存在")
        if session.status.value != "in_progress":
            raise ValueError("只有进行中的随访可以使用对话辅助填写")
        return session

    def _build_task(
        self,
        session,
        questionnaire: dict[str, Any],
        message_text: str,
        *,
        task_id: str | None = None,
        received_at: str | None = None,
    ) -> SemanticTask:
        message_text = message_text.strip()
        received_at = received_at or utc_now_iso()
        temporal_context = build_temporal_context(
            session_id=session.session_id,
            session_created_at=session.created_at,
            message_text=message_text,
            patient_timezone=self.patient_timezone,
            received_at=received_at,
        )
        conversation_context = self._conversation_context(
            session.session_id,
            temporal_context.followup_occurrence_id,
        )
        digest = hashlib.sha256(message_text.encode("utf-8")).hexdigest()
        context_identity = json.dumps(
            {
                "local_date": temporal_context.local_date,
                "latest_run_id": (
                    conversation_context.recent_turns[-1].run_id
                    if conversation_context.recent_turns
                    else None
                ),
                "pending_action_ids": [
                    item.action_id for item in conversation_context.pending_actions
                ],
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        task_id = task_id or (
            "semantic-"
            + uuid5(
                NAMESPACE_URL,
                f"{session.session_id}|{digest}|{context_identity}",
            ).hex
        )
        return SemanticTask(
            task_id=task_id,
            patient_id=session.patient_id,
            session_id=session.session_id,
            pathway_code=session.pathway_code,
            pathway_version=session.pathway_version,
            questionnaire_canonical=session.questionnaire_canonical,
            questionnaire_version=session.questionnaire_version,
            terminology_catalog_id=self.terminology.catalog.catalog_id,
            terminology_catalog_version=self.terminology.catalog.version,
            message_text=message_text,
            existing_answers=session.answers,
            conversation_context=conversation_context,
            long_term_memory=[
                LongTermMemoryItem(
                    observation_id=item.observation_id,
                    code_system=item.code_system,
                    code=item.code,
                    display=item.code_display,
                    value_display=item.value_display,
                    effective_time=item.effective_time,
                    source_kind=item.evidence.source_kind,
                )
                for item in self.store.list_observations(session.patient_id)[:50]
            ],
            temporal_context=temporal_context,
            allowed_items=[
                *_questionnaire_contracts(questionnaire),
                *self.terminology.catalog.questionnaire_contracts(),
            ],
            created_at=temporal_context.received_at_utc,
        )

    def _conversation_context(
        self, session_id: str, followup_occurrence_id: str
    ) -> ConversationContext:
        # Short-term memory is the current daily follow-up occurrence, not an
        # arbitrary five-turn window.  A defensive cap only limits prompt size.
        max_turns = 100
        newest = []
        for record in self.store.list_agent_runs(session_id):
            result = SemanticResult.model_validate(record.output_json)
            temporal = result.temporal_context
            if (
                temporal is None
                or temporal.followup_occurrence_id == followup_occurrence_id
            ):
                newest.append(record)
            if len(newest) >= max_turns:
                break
        resolved = self.store.resolved_conversation_action_ids(session_id)
        recent_turns = []
        for record in reversed(newest):
            result = SemanticResult.model_validate(record.output_json)
            recent_turns.append(
                ConversationTurnContext(
                    run_id=record.run_id,
                    message_text=record.input_text,
                    status=result.status,
                    completed_at=record.completed_at,
                    candidate_link_ids=[
                        item.link_id for item in result.candidates
                    ],
                )
            )

        pending: list[PendingActionContext] = []
        for record in newest:
            result = SemanticResult.model_validate(record.output_json)
            run_actions = [
                PendingActionContext(
                    action_id=item.candidate_id,
                    action_type=PendingActionType.CANDIDATE_CONFIRMATION,
                    source_run_id=record.run_id,
                    link_id=item.link_id,
                    prompt=item.patient_message,
                    proposed_answer=item.answer,
                )
                for item in result.candidates
                if item.candidate_id not in resolved
            ]
            run_actions.extend(
                PendingActionContext(
                    action_id=item.clarification_id,
                    action_type=PendingActionType.CLARIFICATION,
                    source_run_id=record.run_id,
                    link_id=(
                        item.proposed_candidate.link_id
                        if item.proposed_candidate is not None
                        else item.target_link_id
                    ),
                    kind=item.kind,
                    prompt=item.prompt,
                    option_ids=[option.option_id for option in item.options],
                    proposed_answer=(
                        item.proposed_candidate.answer
                        if item.proposed_candidate is not None
                        else None
                    ),
                )
                for item in result.clarifications
                if item.clarification_id not in resolved
            )
            if run_actions:
                pending = run_actions
                break
        return ConversationContext(
            recent_turns=recent_turns,
            pending_actions=pending,
            memory_scope="daily_followup_session",
            followup_occurrence_id=followup_occurrence_id,
            max_turns=max_turns,
        )


def _questionnaire_contracts(questionnaire: dict[str, Any]):
    contracts = []
    for item in flatten_questionnaire_items(questionnaire.get("item", [])):
        if item.get("type") in {"display", "group"}:
            continue
        contracts.append(
            QuestionnaireItemContract(
                link_id=item["linkId"],
                item_type=item["type"],
                text=item.get("text", item["linkId"]),
                codes=[CodingContract.model_validate(code) for code in item.get("code", [])],
                answer_options=[
                    AnswerOptionContract(
                        code=option["valueCoding"]["code"],
                        system=option["valueCoding"]["system"],
                        display=option["valueCoding"].get("display"),
                        semantic_aliases=[
                            extension["valueString"]
                            for extension in option.get("extension", [])
                            if extension.get("url") == SEMANTIC_ALIAS_EXTENSION_URL
                            and isinstance(extension.get("valueString"), str)
                        ],
                    )
                    for option in item.get("answerOption", [])
                    if "valueCoding" in option
                ],
                enable_when=[
                    EnableWhenContract(
                        question=condition["question"],
                        operator=condition["operator"],
                        answer=_enable_when_answer(condition),
                    )
                    for condition in item.get("enableWhen", [])
                ],
                enable_behavior=item.get("enableBehavior"),
                required=item.get("required", False),
                repeats=item.get("repeats", False),
            )
        )
    return contracts


def _enable_when_answer(condition: dict[str, Any]) -> Any:
    answers = [
        value for key, value in condition.items() if key.startswith("answer")
    ]
    if len(answers) != 1:
        raise ValueError("Questionnaire.enableWhen must contain one answer[x]")
    return answers[0]


def _elapsed_ms(started_at: str, completed_at: str) -> int:
    try:
        started = datetime.fromisoformat(started_at)
        completed = datetime.fromisoformat(completed_at)
        return max(0, round((completed - started).total_seconds() * 1000))
    except ValueError:
        return 0


def _aggregate_stage_usage(
    traces: list[AgentStageTrace],
) -> dict[str, int] | None:
    totals: dict[str, int] = {}
    for trace in traces:
        for key, value in (trace.model_usage or {}).items():
            totals[key] = totals.get(key, 0) + value
    return totals or None


def _mention_temporality(
    task: SemanticTask, start: int, end: int
) -> Temporality:
    mentions = task.temporal_context.detected_mentions
    overlapping = [
        item
        for item in mentions
        if item.evidence_start < end and item.evidence_end > start
    ]
    considered = overlapping or mentions
    if any(
        item.kind.value in {"point_in_time", "partial_local_day"}
        for item in considered
    ):
        return Temporality.CURRENT
    if any(item.kind.value == "rolling_24h" for item in considered):
        return Temporality.EXPLICIT_24H
    if any(item.kind.value == "local_calendar_day" for item in considered):
        return Temporality.HISTORICAL
    return Temporality.UNSPECIFIED


def _mention_negated(text: str, start: int) -> bool:
    prefix = text[max(0, start - 6) : start]
    return bool(re.search(r"(?:没有|没|未|无|不再|并无)\s*$", prefix))


_AFFIRMATIVE_REPLY = re.compile(
    r"^(?:是(?:的)?|对(?:的)?|没错|嗯+|好(?:的)?|确定|确认)[。！！!,.，\s]*$"
)
_NEGATIVE_REPLY = re.compile(
    r"^(?:不是|不对|没有|否|不确认)[。！！!,.，\s]*$"
)
_UNSURE_REPLY = re.compile(
    r"^(?:不确定|不知道|不清楚|记不清|想不起来)[。！！!,.，\s]*$"
)
_ALL_ACCEPTED_REPLY = re.compile(
    r"^(?:都对|都正确|全部正确|全部都对|都是的)[。！！!,.，\s]*$"
)


def _all_actions_reply(message_text: str) -> bool:
    return bool(_ALL_ACCEPTED_REPLY.fullmatch(message_text.strip()))


def _short_reply_decision(
    message_text: str, actions: list[PendingActionContext]
) -> ContextResolutionDecision | None:
    text_value = message_text.strip()
    if _all_actions_reply(text_value):
        if all(
            item.action_type == PendingActionType.CANDIDATE_CONFIRMATION
            for item in actions
        ):
            return ContextResolutionDecision.ACCEPTED
        return None
    if len(actions) != 1:
        return None
    if _AFFIRMATIVE_REPLY.fullmatch(text_value):
        return ContextResolutionDecision.ACCEPTED
    if _NEGATIVE_REPLY.fullmatch(text_value):
        return ContextResolutionDecision.REJECTED
    if _UNSURE_REPLY.fullmatch(text_value):
        return ContextResolutionDecision.UNSURE
    return None


def _contextual_answer(message_text: str, question) -> tuple[Any, bool] | None:
    text_value = message_text.strip()
    if question.item_type == "choice":
        matching = []
        for option in question.answer_options:
            terms = [option.code, option.display, *option.semantic_aliases]
            if any(term and term in text_value for term in terms):
                matching.append(option.code)
        if len(set(matching)) == 1:
            return matching[0], False
        return None
    if question.item_type == "boolean":
        if _AFFIRMATIVE_REPLY.fullmatch(text_value):
            return True, False
        if _NEGATIVE_REPLY.fullmatch(text_value):
            return False, True
        return None
    if question.item_type == "integer":
        value = count_from_evidence(text_value)
        if value is None:
            token_match = re.search(r"\d+|[零〇一二两三四五六七八九十]+", text_value)
            value = parse_number_token(token_match.group(0)) if token_match else None
        return (value, False) if type(value) is int and value >= 0 else None
    if question.item_type == "quantity":
        value = millilitres_from_evidence(text_value)
        if value is None:
            token_match = re.search(r"\d+(?:\.\d+)?|[零〇一二两三四五六七八九十]+", text_value)
            value = parse_number_token(token_match.group(0)) if token_match else None
        if type(value) not in {int, float} or value < 0:
            return None
        return {
            "value": value,
            "unit": "mL",
            "system": UCUM,
            "code": "mL",
        }, False
    return None


def _contextual_answer_display(answer: Any, question) -> str:
    if answer is True:
        return "是"
    if answer is False:
        return "否"
    if isinstance(answer, dict) and "value" in answer:
        return f"{answer['value']} {answer.get('unit', '')}".strip()
    option = next(
        (item for item in question.answer_options if item.code == answer),
        None,
    )
    if option is not None:
        return {
            "Mild": "轻度",
            "Moderate": "中度",
            "Severe": "重度",
        }.get(option.display, option.display or option.code)
    return str(answer)
