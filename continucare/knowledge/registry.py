"""Load immutable knowledge releases shipped with ContinuCare."""

from __future__ import annotations

from dataclasses import dataclass
from importlib.resources import files

from continucare.knowledge.models import (
    CoverageReport,
    DataQualityRule,
    EvidenceClaim,
    KnowledgeRelease,
    MetricDefinition,
    PatientContent,
    ProductRecord,
    ReleaseManifest,
    SourceRecord,
    TerminologyEntry,
)


@dataclass(frozen=True)
class KnowledgeRegistry:
    release: KnowledgeRelease

    def source(self, source_id: str) -> SourceRecord:
        return self._find(self.release.sources, "source_id", source_id)

    def claim(self, claim_id: str) -> EvidenceClaim:
        return self._find(self.release.evidence_claims, "claim_id", claim_id)

    def metric(self, metric_id: str) -> MetricDefinition:
        return self._find(self.release.metrics, "metric_id", metric_id)

    @staticmethod
    def _find(items: list, attribute: str, value: str):
        item = next((item for item in items if getattr(item, attribute) == value), None)
        if item is None:
            raise LookupError(f"unknown {attribute} {value!r}")
        return item


def _read_json(data_dir, name: str, model):
    text = data_dir.joinpath(name).read_text("utf-8")
    if model is ReleaseManifest:
        return model.model_validate_json(text)
    return [model.model_validate(item) for item in __import__("json").loads(text)]


def load_cn_glp1_release() -> KnowledgeRegistry:
    data_dir = files("continucare.knowledge.data.cn_glp1.v1")
    release = KnowledgeRelease(
        manifest=_read_json(data_dir, "release_manifest.json", ReleaseManifest),
        sources=_read_json(data_dir, "source_registry.json", SourceRecord),
        products=_read_json(data_dir, "product_registry.json", ProductRecord),
        evidence_claims=_read_json(data_dir, "evidence_claims.json", EvidenceClaim),
        metrics=_read_json(data_dir, "metric_definitions.json", MetricDefinition),
        terminology=_read_json(data_dir, "terminology_manifest.json", TerminologyEntry),
        patient_content=_read_json(data_dir, "patient_content.zh-CN.json", PatientContent),
        data_quality_rules=_read_json(data_dir, "data_quality_rules.json", DataQualityRule),
        clinical_rules=__import__("json").loads(
            data_dir.joinpath("clinical_rules.json").read_text("utf-8")
        ),
        coverage=CoverageReport.model_validate_json(
            data_dir.joinpath("coverage_report.json").read_text("utf-8")
        ),
    )
    return KnowledgeRegistry(release)
