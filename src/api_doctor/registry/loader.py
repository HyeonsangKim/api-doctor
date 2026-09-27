"""레지스트리 로더 (FR-001).

등록 자료를 읽어 **hash 로 잠근다**. 작업 시작 시 잠근 hash 가 이후 모든 검사·감사
결과의 키가 되며, 이 파일들이 바뀌면 이전 결과는 자동으로 STALE 이 된다 (AC-13).

기대값(`expected/`)은 여기서 로드하지 않는다. 검증기만 별도 경로로 읽는다.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


class RegistryInvalid(RuntimeError):
    """REGISTRY_INVALID — 등록 자료가 계약을 만족하지 않습니다."""


def sha256_of(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _read_yaml(path: Path) -> tuple[dict[str, Any], str]:
    if not path.is_file():
        raise RegistryInvalid(f"등록 파일이 없습니다: {path}")
    raw = path.read_bytes()
    try:
        parsed = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise RegistryInvalid(f"YAML 파싱 실패 {path.name}: {exc}") from exc
    if not isinstance(parsed, dict):
        raise RegistryInvalid(f"최상위가 매핑이 아닙니다: {path.name}")
    return parsed, sha256_of(raw)


@dataclass(frozen=True, slots=True)
class FieldSpec:
    name: str
    type: str
    nullable: bool


@dataclass(frozen=True, slots=True)
class ValueRule:
    rule_id: str
    field: str
    kind: str
    pattern: str | None = None


@dataclass(frozen=True, slots=True)
class AuditRequirement:
    risk_id: str
    description: str


@dataclass(frozen=True, slots=True)
class Contract:
    """불변 조회 계약. 모델이 만들거나 바꿀 수 없다."""

    contract_id: str
    version: str
    query: dict[str, Any]
    expected_record_count: int
    identity_key: str
    required_fields: tuple[FieldSpec, ...]
    value_rules: tuple[ValueRule, ...]
    audit_requirements: tuple[AuditRequirement, ...]
    contract_hash: str

    def neutral_summary(self) -> dict[str, Any]:
        """에이전트에게 전달되는 중립 요약. 기대값·정답은 포함하지 않는다."""
        return {
            "contract_id": self.contract_id,
            "version": self.version,
            "query": dict(self.query),
            "identity_key": self.identity_key,
            "required_fields": [
                {"name": f.name, "type": f.type, "nullable": f.nullable}
                for f in self.required_fields
            ],
            "value_rules": [
                {"rule_id": r.rule_id, "field": r.field, "kind": r.kind}
                for r in self.value_rules
            ],
            "audit_requirements": [
                {"risk_id": a.risk_id, "description": a.description}
                for a in self.audit_requirements
            ],
        }


@dataclass(frozen=True, slots=True)
class Probe:
    """등록된 관측 절차 1건.

    `invariant` 는 **비공개 기대값 없이 검사 가능한 성질**이어야 한다.
    그래야 감사자가 정답을 보지 않고도 손실을 관측할 수 있다.
    `runs` 가 2개 이상이면 실행끼리 대조하는 probe 다.
    """

    probe_id: str
    description: str
    invariant: str
    runs: tuple[dict[str, Any], ...]
    covers: tuple[str, ...]
    is_baseline: bool

    @property
    def sandbox_runs(self) -> int:
        return len(self.runs)


@dataclass(frozen=True, slots=True)
class ProbeCatalog:
    version: str
    probes: tuple[Probe, ...]
    catalog_hash: str
    invariants: dict[str, str] = field(default_factory=dict)

    def get(self, probe_id: str) -> Probe | None:
        return next((p for p in self.probes if p.probe_id == probe_id), None)

    def coverage_of(self, probe_ids: tuple[str, ...]) -> set[str]:
        """주어진 probe 들이 덮는 위험 영역의 합집합. 고정 metadata 만 쓴다."""
        covered: set[str] = set()
        for pid in probe_ids:
            probe = self.get(pid)
            if probe is not None:
                covered.update(probe.covers)
        return covered


@dataclass(frozen=True, slots=True)
class Endpoint:
    method: str
    url_prefix: str
    allowed_params: frozenset[str]


@dataclass(frozen=True, slots=True)
class Dataset:
    dataset_id: str
    display_name: str
    default_snapshot: str | None
    live_ready: bool
    live_blockers: tuple[str, ...]
    provenance: dict[str, Any]
    allowed_endpoints: tuple[Endpoint, ...]
    allowed_doc_domains: tuple[str, ...]
    provider_errors: dict[str, str]
    request_shape: dict[str, Any]
    skill: str | None
    contract: Contract
    probes: ProbeCatalog
    dataset_hash: str
    root: Path

    def snapshot_path(self, snapshot_id: str) -> Path:
        return self.root / "snapshots" / f"{snapshot_id}.json"

    def expected_path(self) -> Path:
        """기대값 경로. 검증기만 사용한다."""
        return (
            self.root
            / "expected"
            / f"{self.contract.contract_id}.{self.contract.version}.json"
        )


def _parse_contract(path: Path) -> Contract:
    data, digest = _read_yaml(path)
    try:
        completeness = data["completeness"]
        return Contract(
            contract_id=str(data["contract_id"]),
            version=str(data["version"]),
            query=dict(data["query"]),
            expected_record_count=int(completeness["expected_record_count"]),
            identity_key=str(completeness["identity_key"]),
            required_fields=tuple(
                FieldSpec(str(f["name"]), str(f["type"]), bool(f.get("nullable", True)))
                for f in data["required_fields"]
            ),
            value_rules=tuple(
                ValueRule(
                    str(r["rule_id"]), str(r["field"]), str(r["kind"]), r.get("pattern")
                )
                for r in data.get("value_rules", [])
            ),
            audit_requirements=tuple(
                AuditRequirement(str(a["risk_id"]), str(a["description"]))
                for a in data.get("audit_requirements", [])
            ),
            contract_hash=digest,
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise RegistryInvalid(f"계약 형식 오류 {path.name}: {exc}") from exc


def _parse_probes(path: Path) -> ProbeCatalog:
    data, digest = _read_yaml(path)
    try:
        probes = tuple(
            Probe(
                probe_id=str(p["probe_id"]),
                description=str(p["description"]),
                invariant=str(p["invariant"]),
                runs=tuple(dict(r) for r in p["runs"]),
                covers=tuple(str(c) for c in (p.get("covers") or [])),
                is_baseline=bool(p.get("is_baseline", False)),
            )
            for p in data["probes"]
        )
    except (KeyError, TypeError) as exc:
        raise RegistryInvalid(f"probe catalog 형식 오류 {path.name}: {exc}") from exc

    ids = [p.probe_id for p in probes]
    if len(ids) != len(set(ids)):
        raise RegistryInvalid(f"probe_id 가 중복됩니다: {path.name}")
    if sum(p.is_baseline for p in probes) != 1:
        raise RegistryInvalid(f"baseline probe 는 정확히 1개여야 합니다: {path.name}")

    known = set(data.get("invariants") or {})
    for probe in probes:
        if probe.invariant not in known:
            raise RegistryInvalid(
                f"{probe.probe_id}: 등록되지 않은 invariant {probe.invariant!r}"
            )
        if not probe.runs:
            raise RegistryInvalid(f"{probe.probe_id}: runs 가 비어 있습니다")
    return ProbeCatalog(
        version=str(data["version"]), probes=probes, catalog_hash=digest,
        invariants={k: str(v.get("description", "")) for k, v in (data.get("invariants") or {}).items()},
    )


def load_dataset(root: Path) -> Dataset:
    """데이터셋 1개를 로드하고 계약과의 정합성을 검사한다."""
    data, digest = _read_yaml(root / "dataset.yaml")
    try:
        contract = _parse_contract(root / f"contract.{data['contract_version']}.yaml")
        probes = _parse_probes(root / f"probes.{data['contract_version']}.yaml")
        dataset = Dataset(
            dataset_id=str(data["dataset_id"]),
            display_name=str(data["display_name"]),
            default_snapshot=(
                str(data["default_snapshot"]) if data.get("default_snapshot") else None
            ),
            live_ready=bool(data.get("live_ready", False)),
            live_blockers=tuple(str(b) for b in (data.get("live_blockers") or [])),
            provenance=dict(data.get("provenance") or {}),
            allowed_endpoints=tuple(
                Endpoint(
                    method=str(e.get("method", "GET")).upper(),
                    url_prefix=str(e["url_prefix"]),
                    allowed_params=frozenset(str(p) for p in (e.get("allowed_params") or [])),
                )
                for e in data["allowed_endpoints"]
            ),
            allowed_doc_domains=tuple(str(d) for d in (data.get("allowed_doc_domains") or [])),
            provider_errors={
                str(k).upper(): str(v)
                for k, v in (data.get("provider_errors") or {}).items()
            },
            request_shape=dict(data.get("request_shape") or {}),
            skill=data.get("skill"),
            contract=contract,
            probes=probes,
            dataset_hash=digest,
            root=root,
        )
    except (KeyError, TypeError) as exc:
        raise RegistryInvalid(f"데이터셋 형식 오류 {root.name}: {exc}") from exc

    _validate(dataset)
    return dataset


def _validate(dataset: Dataset) -> None:
    """등록 자료끼리 어긋나면 로드 자체를 거절한다."""
    problems: list[str] = []

    if dataset.contract.contract_id != dataset.contract.contract_id.strip():
        problems.append("contract_id 에 공백이 있습니다")

    if not dataset.allowed_endpoints:
        problems.append("allowed_endpoints 가 비어 있습니다")

    # PRD §4.5: HTTPS 미확인 소스는 key 를 전송하는 live 지원에서 제외하고
    # 키 없는 fixture 로만 쓴다. 그래서 http 등록 자체는 막지 않고,
    # live_ready 와 함께일 때만 거절한다.
    if dataset.live_ready:
        for endpoint in dataset.allowed_endpoints:
            if not endpoint.url_prefix.startswith("https://"):
                problems.append(
                    f"live_ready 인데 HTTPS 가 아닙니다: {endpoint.url_prefix}. "
                    "키를 전송하는 경로는 HTTPS 여야 합니다."
                )
    elif not dataset.live_blockers:
        problems.append("live_ready 가 거짓이면 live_blockers 로 사유를 남겨야 합니다")

    # 계약이 요구하는 위험 영역을 catalog 가 전부 덮을 수 있어야 한다.
    # 못 덮으면 감사는 애초에 완료될 수 없으므로 등록 시점에 막는다.
    required = {a.risk_id for a in dataset.contract.audit_requirements}
    coverable = {c for p in dataset.probes.probes for c in p.covers}
    missing = required - coverable
    if missing:
        problems.append(f"probe catalog 가 덮지 못하는 위험 영역: {sorted(missing)}")

    if dataset.default_snapshot is not None:
        if not dataset.snapshot_path(dataset.default_snapshot).is_file():
            problems.append(
                f"default_snapshot 파일이 없습니다: {dataset.default_snapshot}"
            )

    if not dataset.expected_path().is_file():
        problems.append(f"기대값 파일이 없습니다: {dataset.expected_path().name}")

    if not (dataset.root / "snapshots").is_dir():
        problems.append("snapshots 디렉터리가 없습니다")

    if problems:
        raise RegistryInvalid(
            f"{dataset.dataset_id}: " + "; ".join(problems)
        )


def default_registry_root() -> Path:
    return Path(__file__).resolve().parents[3] / "registry"


def load_registry(root: Path | None = None) -> dict[str, Dataset]:
    """등록된 모든 데이터셋을 로드한다."""
    root = root or default_registry_root()
    datasets_dir = root / "datasets"
    if not datasets_dir.is_dir():
        raise RegistryInvalid(f"레지스트리를 찾을 수 없습니다: {datasets_dir}")

    loaded: dict[str, Dataset] = {}
    for child in sorted(datasets_dir.iterdir()):
        if not child.is_dir() or child.name.startswith("."):
            continue
        dataset = load_dataset(child)
        if dataset.dataset_id in loaded:
            raise RegistryInvalid(f"dataset_id 가 중복됩니다: {dataset.dataset_id}")
        if dataset.dataset_id != child.name:
            raise RegistryInvalid(
                f"디렉터리명과 dataset_id 가 다릅니다: {child.name} != {dataset.dataset_id}"
            )
        loaded[dataset.dataset_id] = dataset
    if not loaded:
        raise RegistryInvalid("등록된 데이터셋이 없습니다")
    return loaded
