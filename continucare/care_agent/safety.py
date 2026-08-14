"""Hybrid Safety Agent: deterministic hard gates plus optional LLM critic."""

from __future__ import annotations

import re
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from continucare.agents.contracts import (
    AgentStageTrace,
    CandidateIssue,
    CandidateIssueAction,
    ClarificationKind,
    ClarificationOption,
    ClarificationRequest,
    SemanticCandidate,
    SemanticResult,
    SemanticStatus,
    SemanticTask,
    SafetyReviewTask,
    SubjectType,
    Temporality,
)
from continucare.care_agent.mimo_enhancements import (
    SafetyCritic,
    governed_missing_findings,
    questionnaire_item_enabled,
)
from continucare.care_agent.numbers import (
    count_from_evidence,
    millilitres_from_evidence,
    parse_number_token,
)
from continucare.fhir.terminology import UCUM


_INSTRUCTION_PATTERN = re.compile(
    r"忽略.{0,12}(?:规则|指令|提示)|把.{0,12}(?:次数|答案|记录).{0,8}(?:改成|设为|写成)|system\s*prompt|prompt\s*injection",
    re.IGNORECASE,
)

_FIELD_LABELS = {
    "nausea-present": "当前是否恶心",
    "nausea-severity": "当前恶心程度",
    "vomiting-count-24h": "过去24小时呕吐次数",
    "fluid-intake-24h-estimated": "过去24小时液体摄入量",
    "abdominal-pain-present": "当前是否腹痛",
}
_FIELD_EVIDENCE_PATTERNS = {
    "nausea-present": re.compile(r"恶心|反胃|想吐"),
    "nausea-severity": re.compile(r"恶心|反胃|想吐"),
    "vomiting-count-24h": re.compile(r"呕吐|吐了?|吐过"),
    "fluid-intake-24h-estimated": re.compile(r"喝水|饮水|液体(?:摄入)?"),
    "abdominal-pain-present": re.compile(r"腹痛|肚子(?:痛|疼)"),
}
_REASON_TEXT = {
    "evidence_concept_mismatch": "引用的原话没有明确提到这个症状或指标，不能由其他表现推断。",
    "evidence_negation_mismatch": "候选的有/无判断与引用原话中的否定表达不一致。",
    "missing_24h_temporality": "原话没有明确覆盖完整的过去24小时，不能直接写入24小时字段。",
    "missing_current_temporality": "原话没有明确说明这是当前情况，不能直接写入当前症状字段。",
    "subject_not_patient": "原话描述的不是患者本人。",
    "invalid_evidence_span": "模型引用的证据无法在患者原话中逐字找到。",
    "answer_type_boolean": "候选答案类型不符合问卷布尔字段。",
    "answer_type_integer": "候选答案不是有效的非负整数。",
    "answer_option_not_governed": "候选答案不属于当前问卷允许的选项。",
    "answer_type_quantity": "候选数量不是有效的结构化数值。",
    "quantity_unit_not_governed": "候选数量的单位不符合当前 UCUM 映射。",
    "code_not_governed": "候选编码不属于当前锁定问卷。",
    "unexpected_code": "模型为不应编码的字段提供了编码。",
    "confirmation_required": "候选没有保留患者确认要求。",
    "unsafe_patient_wording": "候选措辞包含诊断、治疗或风险结论。",
    "negation_answer_conflict": "候选答案与模型给出的否定标签互相矛盾。",
    "unknown_link_id": "候选字段不属于当前锁定问卷。",
    "conflicting_answers": "同一个问卷字段出现了互相冲突的答案。",
    "enable_when_not_satisfied": "当前已确认信息尚未满足该问题的启用条件。",
    "answer_evidence_mismatch": "候选值与引用原话中的数值、选项或程度不一致。",
    "invalid_context_binding": "当前回答无法与唯一的上一轮已批准问题建立可追溯绑定。",
}


def instruction_like_text(text: str) -> bool:
    """Conservative preflight used before any external model request."""

    return bool(_INSTRUCTION_PATTERN.search(text))


class SafetyAgent:
    VERSION = "safety-agent-hybrid-v4"
    _FORBIDDEN_PATIENT_WORDING = ("诊断为", "建议停药", "建议加药", "治疗方案", "风险等级")
    _EXPLICIT_24H_LINKS = {
        "vomiting-count-24h",
        "fluid-intake-24h-estimated",
    }
    _CURRENT_LINKS = {
        "nausea-present",
        "nausea-severity",
        "abdominal-pain-present",
    }

    def __init__(self, critic: SafetyCritic | None = None):
        self.critic = critic

    def analyze(self, review_task: SafetyReviewTask) -> SemanticResult:
        task = review_task.semantic_task
        hard_result = self.review(task, review_task.draft)
        hard_trace = AgentStageTrace(
            stage="safety_hard_rules",
            agent_name="safety_agent",
            agent_version=self.VERSION,
            mode="deterministic_rules",
            status=hard_result.status.value,
            details={
                "candidate_count": len(hard_result.candidates),
                "clarification_count": len(hard_result.clarifications),
                "violation_count": len(hard_result.safety_violations),
            },
        )
        hard_result = hard_result.model_copy(
            update={"stage_traces": [*hard_result.stage_traces, hard_trace]}
        )
        if (
            self.critic is None
            or not self.critic.configured
            or hard_result.status == SemanticStatus.BLOCKED
        ):
            return hard_result.model_copy(
                update={
                    "stage_traces": [
                        *hard_result.stage_traces,
                        AgentStageTrace(
                            stage="safety_critic",
                            agent_name="safety_agent",
                            agent_version=self.VERSION,
                            mode="disabled_or_not_applicable",
                            status="skipped",
                        ),
                    ]
                }
            )
        try:
            outcome = self.critic.review(task, hard_result)
        except Exception as exc:
            return hard_result.model_copy(
                update={
                    "ignored_reasons": [
                        *hard_result.ignored_reasons,
                        f"safety_critic_fallback:{type(exc).__name__}",
                    ],
                    "stage_traces": [
                        *hard_result.stage_traces,
                        AgentStageTrace(
                            stage="safety_critic",
                            agent_name="safety_agent",
                            agent_version=self.VERSION,
                            mode="deterministic_fallback",
                            status="failed",
                            details={"error_type": type(exc).__name__},
                        ),
                    ],
                }
            )
        return self._apply_critic(task, hard_result, outcome)

    def _apply_critic(self, task, hard_result, outcome) -> SemanticResult:
        decision = outcome.decision
        review_groups = {}
        for item in decision.candidate_reviews:
            review_groups.setdefault(item.candidate_id, []).append(item)
        candidates: list[SemanticCandidate] = []
        clarifications = list(hard_result.clarifications)
        issues = list(hard_result.candidate_issues)
        violations = list(hard_result.safety_violations)
        global_downgrade = decision.overall_verdict in {"block", "human_review"}
        for candidate in hard_result.candidates:
            candidate_reviews = review_groups.get(candidate.candidate_id, [])
            review = candidate_reviews[0] if len(candidate_reviews) == 1 else None
            verdict = review.verdict if review is not None else "human_review"
            if review is not None and verdict == "pass":
                if review.evidence_status == "ambiguous":
                    verdict = "clarification_required"
                elif review.evidence_status == "unsupported":
                    verdict = "reject"
            if global_downgrade and verdict == "pass":
                verdict = "human_review"
            if verdict == "pass":
                candidates.append(candidate)
                continue
            reason_codes = (
                review.reason_codes
                if review is not None and review.reason_codes
                else ["safety_critic_review_required"]
            )
            explanation = (
                review.explanation
                if review is not None
                else "Safety Critic 未返回该候选的明确审核结果，系统要求进一步确认。"
            )
            if verdict in {"clarification_required", "human_review"}:
                clarifications.append(
                    _critic_clarification(candidate, explanation)
                )
                issues.append(
                    _critic_issue(
                        task,
                        candidate,
                        reason_codes,
                        explanation,
                        CandidateIssueAction.CLARIFICATION_REQUIRED,
                    )
                )
            else:
                violations.extend(
                    f"{candidate.candidate_id}:{code}" for code in reason_codes
                )
                issues.append(
                    _critic_issue(
                        task,
                        candidate,
                        reason_codes,
                        explanation,
                        CandidateIssueAction.REJECTED,
                    )
                )
        findings = governed_missing_findings(task, hard_result, outcome)
        status = _result_status(
            candidates,
            clarifications,
            blocked=hard_result.status == SemanticStatus.BLOCKED,
        )
        trace = AgentStageTrace(
            stage="safety_critic",
            agent_name="safety_agent",
            agent_version=self.VERSION,
            mode=outcome.mode,
            status=decision.overall_verdict,
            model_provider="xiaomi_mimo",
            model_name=self.critic.config.model_name,
            prompt_version=outcome.prompt_version,
            model_usage=outcome.model_usage,
            provider_request_id=outcome.provider_request_id,
            latency_ms=outcome.latency_ms,
            details={
                "reviewed_candidate_count": len(decision.candidate_reviews),
                "missing_supported_count": sum(
                    item.status.value == "supported" for item in findings
                ),
                "missing_ambiguous_count": sum(
                    item.status.value == "ambiguous" for item in findings
                ),
                "attempt_count": outcome.attempt_count,
            },
        )
        return hard_result.model_copy(
            update={
                "status": status,
                "candidates": candidates,
                "clarifications": clarifications,
                "candidate_issues": _deduplicate_issues(issues),
                "missing_items": findings,
                "safety_violations": sorted(set(violations)),
                "stage_traces": [*hard_result.stage_traces, trace],
            }
        )

    def review(self, task: SemanticTask, draft: SemanticResult) -> SemanticResult:
        violations = list(draft.safety_violations)
        candidate_issues = list(draft.candidate_issues)
        duplicate_ids = _conflicting_link_ids(draft.candidates)
        safe_candidates: list[SemanticCandidate] = []
        for candidate in draft.candidates:
            if candidate.link_id in duplicate_ids:
                errors = [f"{candidate.candidate_id}:conflicting_answers"]
                violations.extend(errors)
                candidate_issues.append(_candidate_issue(task, candidate, errors))
                continue
            errors = self.review_candidate(task, candidate)
            if errors:
                violations.extend(errors)
                candidate_issues.append(_candidate_issue(task, candidate, errors))
            else:
                safe_candidates.append(candidate)

        governed_answers = dict(task.existing_answers)
        governed_answers.update(
            {item.link_id: item.answer for item in safe_candidates}
        )
        dependency_safe_candidates: list[SemanticCandidate] = []
        for candidate in safe_candidates:
            item = next(
                item
                for item in task.allowed_items
                if item.link_id == candidate.link_id
            )
            if questionnaire_item_enabled(item, governed_answers):
                dependency_safe_candidates.append(candidate)
                continue
            errors = [f"{candidate.candidate_id}:enable_when_not_satisfied"]
            violations.extend(errors)
            candidate_issues.append(_candidate_issue(task, candidate, errors))
        safe_candidates = dependency_safe_candidates

        safe_clarifications = []
        for clarification in draft.clarifications:
            proposed = clarification.proposed_candidate
            if proposed is None:
                safe_clarifications.append(clarification)
                continue
            allow_unspecified = clarification.kind in {
                ClarificationKind.CONFIRM_TIME_WINDOW,
                ClarificationKind.CONFIRM_CURRENT,
            }
            errors = self.review_candidate(
                task, proposed, allow_unspecified_temporality=allow_unspecified
            )
            item = next(
                item
                for item in task.allowed_items
                if item.link_id == proposed.link_id
            )
            if not questionnaire_item_enabled(item, governed_answers):
                errors.append(
                    f"{proposed.candidate_id}:enable_when_not_satisfied"
                )
            if errors:
                violations.extend(errors)
                candidate_issues.append(_candidate_issue(task, proposed, errors))
            else:
                safe_clarifications.append(clarification)

        status = _result_status(
            safe_candidates,
            safe_clarifications,
            blocked=draft.status == SemanticStatus.BLOCKED,
        )
        return draft.model_copy(
            update={
                "status": status,
                "candidates": safe_candidates,
                "clarifications": safe_clarifications,
                "candidate_issues": _deduplicate_issues(candidate_issues),
                "safety_violations": sorted(set(violations)),
                "safety_agent_version": self.VERSION,
            }
        )

    def review_candidate(
        self,
        task: SemanticTask,
        candidate: SemanticCandidate,
        *,
        allow_unspecified_temporality: bool = False,
    ) -> list[str]:
        errors: list[str] = []
        items = {item.link_id: item for item in task.allowed_items}
        item = items.get(candidate.link_id)
        prefix = candidate.candidate_id
        if item is None:
            return [f"{prefix}:unknown_link_id"]
        if candidate.subject != SubjectType.PATIENT:
            errors.append(f"{prefix}:subject_not_patient")
        if not candidate.requires_patient_confirmation:
            errors.append(f"{prefix}:confirmation_required")
        context_bound = _valid_context_binding(task, candidate)
        if candidate.context_binding is not None and not context_bound:
            errors.append(f"{prefix}:invalid_context_binding")
        if not _evidence_matches(task.message_text, candidate):
            errors.append(f"{prefix}:invalid_evidence_span")
        elif not _evidence_supports_field(candidate) and not context_bound:
            errors.append(f"{prefix}:evidence_concept_mismatch")
        if any(word in candidate.patient_message for word in self._FORBIDDEN_PATIENT_WORDING):
            errors.append(f"{prefix}:unsafe_patient_wording")

        expected_code = item.codes[0] if item.codes else None
        if expected_code is not None:
            actual = candidate.questionnaire_code
            if actual is None or (actual.system, actual.code) != (
                expected_code.system,
                expected_code.code,
            ):
                errors.append(f"{prefix}:code_not_governed")
        elif candidate.questionnaire_code is not None:
            errors.append(f"{prefix}:unexpected_code")
        if task.terminology_catalog_id is not None:
            match = candidate.terminology_match
            if match is None:
                errors.append(f"{prefix}:terminology_match_missing")
            else:
                if (
                    match.catalog_id != task.terminology_catalog_id
                    or match.catalog_version != task.terminology_catalog_version
                ):
                    errors.append(f"{prefix}:terminology_catalog_mismatch")
                actual = candidate.questionnaire_code
                if actual is None or (
                    actual.system,
                    actual.code,
                    actual.version,
                ) != (
                    match.target_coding.system,
                    match.target_coding.code,
                    match.target_coding.version,
                ):
                    errors.append(f"{prefix}:terminology_target_code_mismatch")
                if match.validation_status != "repository_release_validated":
                    errors.append(f"{prefix}:terminology_code_not_validated")

        answer_errors = _answer_errors(item, candidate.answer)
        errors.extend(f"{prefix}:{error}" for error in answer_errors)
        if not answer_errors and not _answer_matches_evidence(item, candidate):
            errors.append(f"{prefix}:answer_evidence_mismatch")
        if candidate.link_id in self._EXPLICIT_24H_LINKS:
            if not (
                candidate.temporality == Temporality.EXPLICIT_24H
                or (
                    allow_unspecified_temporality
                    and candidate.temporality == Temporality.UNSPECIFIED
                )
            ):
                errors.append(f"{prefix}:missing_24h_temporality")
        if candidate.link_id in self._CURRENT_LINKS:
            if not (
                candidate.temporality == Temporality.CURRENT
                or (
                    allow_unspecified_temporality
                    and candidate.temporality == Temporality.UNSPECIFIED
                )
            ):
                errors.append(f"{prefix}:missing_current_temporality")
        if item.item_type == "boolean" and type(candidate.answer) is bool:
            if candidate.negated != (candidate.answer is False):
                errors.append(f"{prefix}:negation_answer_conflict")
            elif _evidence_supports_field(
                candidate
            ) and not _evidence_negation_matches(candidate):
                errors.append(f"{prefix}:evidence_negation_mismatch")
        return errors


def _answer_errors(item, answer: Any) -> list[str]:
    if item.item_type == "boolean" and type(answer) is not bool:
        return ["answer_type_boolean"]
    if item.item_type == "integer" and (type(answer) is not int or answer < 0):
        return ["answer_type_integer"]
    if item.item_type == "choice":
        allowed = {option.code for option in item.answer_options}
        if not isinstance(answer, str) or answer not in allowed:
            return ["answer_option_not_governed"]
    if item.item_type == "quantity":
        if not isinstance(answer, dict) or type(answer.get("value")) not in {int, float}:
            return ["answer_type_quantity"]
        if (
            answer.get("system") != UCUM
            or answer.get("code") != "mL"
            or answer.get("unit") != "mL"
            or answer["value"] < 0
        ):
            return ["quantity_unit_not_governed"]
    return []


def _answer_matches_evidence(item, candidate: SemanticCandidate) -> bool:
    if item.item_type == "integer":
        value = count_from_evidence(candidate.evidence_text)
        if value is None and candidate.context_binding is not None:
            match = re.search(
                r"\d+|[零〇一二两三四五六七八九十]+",
                candidate.evidence_text,
            )
            value = parse_number_token(match.group(0)) if match else None
        return value == candidate.answer
    if item.item_type == "quantity":
        value = millilitres_from_evidence(candidate.evidence_text)
        if value is None and candidate.context_binding is not None:
            match = re.search(
                r"\d+(?:\.\d+)?|[零〇一二两三四五六七八九十]+",
                candidate.evidence_text,
            )
            value = parse_number_token(match.group(0)) if match else None
        return value == candidate.answer["value"]
    if item.item_type == "choice":
        matched_codes = {
            option.code
            for option in item.answer_options
            if any(
                term and term in candidate.evidence_text
                for term in [option.display, *option.semantic_aliases]
            )
        }
        return matched_codes == {candidate.answer}
    return True


def _evidence_matches(text: str, candidate: SemanticCandidate) -> bool:
    return (
        candidate.evidence_end <= len(text)
        and text[candidate.evidence_start : candidate.evidence_end]
        == candidate.evidence_text
    )


def _valid_context_binding(
    task: SemanticTask, candidate: SemanticCandidate
) -> bool:
    binding = candidate.context_binding
    if binding is None:
        return False
    matching = [
        item
        for item in task.conversation_context.pending_actions
        if item.action_id == binding.source_action_id
        and item.source_run_id == binding.source_run_id
        and item.link_id == candidate.link_id
        and item.action_type.value == "clarification"
    ]
    return len(task.conversation_context.pending_actions) == 1 and len(matching) == 1


def _evidence_supports_field(candidate: SemanticCandidate) -> bool:
    pattern = _FIELD_EVIDENCE_PATTERNS.get(candidate.link_id)
    return pattern is None or bool(pattern.search(candidate.evidence_text))


def _evidence_negation_matches(candidate: SemanticCandidate) -> bool:
    pattern = _FIELD_EVIDENCE_PATTERNS.get(candidate.link_id)
    if pattern is None:
        return True
    match = pattern.search(candidate.evidence_text)
    if match is None:
        return False
    prefix = candidate.evidence_text[max(0, match.start() - 5) : match.start()]
    evidence_negated = bool(re.search(r"(?:没有|没|未|无|不)\s*$", prefix))
    return evidence_negated == candidate.negated


def _candidate_issue(
    task: SemanticTask,
    candidate: SemanticCandidate,
    errors: list[str],
) -> CandidateIssue:
    reason_codes = list(
        dict.fromkeys(
            error.split(":", 1)[1] if ":" in error else error
            for error in errors
        )
    )
    item = next(
        (item for item in task.allowed_items if item.link_id == candidate.link_id),
        None,
    )
    field_label = _FIELD_LABELS.get(
        candidate.link_id,
        item.text if item is not None else candidate.link_id,
    )
    explanation = " ".join(
        _REASON_TEXT.get(code, f"候选未通过安全规则：{code}。")
        for code in reason_codes
    )
    issue_id = "issue-" + uuid5(
        NAMESPACE_URL,
        "|".join((candidate.candidate_id, *reason_codes)),
    ).hex
    return CandidateIssue(
        issue_id=issue_id,
        candidate_id=candidate.candidate_id,
        link_id=candidate.link_id,
        field_label=field_label,
        proposed_answer=candidate.answer,
        evidence_text=candidate.evidence_text,
        action=CandidateIssueAction.REJECTED,
        reason_codes=reason_codes,
        explanation=explanation,
    )


def _critic_issue(
    task: SemanticTask,
    candidate: SemanticCandidate,
    reason_codes: list[str],
    explanation: str,
    action: CandidateIssueAction,
) -> CandidateIssue:
    item = next(
        (item for item in task.allowed_items if item.link_id == candidate.link_id),
        None,
    )
    field_label = _FIELD_LABELS.get(
        candidate.link_id,
        item.text if item is not None else candidate.link_id,
    )
    normalized_codes = list(dict.fromkeys(reason_codes)) or [
        "safety_critic_review_required"
    ]
    return CandidateIssue(
        issue_id="issue-"
        + uuid5(
            NAMESPACE_URL,
            "|".join(
                (
                    candidate.candidate_id,
                    action.value,
                    *normalized_codes,
                )
            ),
        ).hex,
        candidate_id=candidate.candidate_id,
        link_id=candidate.link_id,
        field_label=field_label,
        proposed_answer=candidate.answer,
        evidence_text=candidate.evidence_text,
        action=action,
        reason_codes=normalized_codes,
        explanation=explanation,
    )


def _critic_clarification(
    candidate: SemanticCandidate, explanation: str
) -> ClarificationRequest:
    return ClarificationRequest(
        clarification_id="clarify-"
        + uuid5(
            NAMESPACE_URL,
            f"{candidate.candidate_id}|semantic-review",
        ).hex,
        kind=ClarificationKind.SEMANTIC_REVIEW,
        prompt=(
            f"{candidate.patient_message} "
            "这项表达可能存在歧义，请您确认；不确定时可以在完整问卷中修改。"
        ),
        proposed_candidate=candidate,
        options=[
            ClarificationOption(
                option_id="yes_semantic",
                label="是，这项记录正确",
                accepts_candidate=True,
            ),
            ClarificationOption(option_id="no", label="不是"),
            ClarificationOption(option_id="unsure", label="我不确定"),
        ],
    )


def _deduplicate_issues(issues: list[CandidateIssue]) -> list[CandidateIssue]:
    return list({item.issue_id: item for item in issues}.values())


def _conflicting_link_ids(candidates: list[SemanticCandidate]) -> set[str]:
    by_link: dict[str, set[str]] = {}
    for candidate in candidates:
        by_link.setdefault(candidate.link_id, set()).add(repr(candidate.answer))
    return {link_id for link_id, answers in by_link.items() if len(answers) > 1}


def _result_status(candidates, clarifications, *, blocked: bool) -> SemanticStatus:
    if blocked:
        return SemanticStatus.BLOCKED
    if clarifications:
        return SemanticStatus.NEEDS_CLARIFICATION
    if candidates:
        return SemanticStatus.NEEDS_CONFIRMATION
    return SemanticStatus.NO_MATCH
