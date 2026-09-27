"""레지스트리 로드와 정합성 (FR-001)."""

from __future__ import annotations

import pytest
import yaml

from api_doctor.registry.loader import RegistryInvalid, load_dataset, load_registry


def test_seoul_library_loads_with_locked_hashes() -> None:
    dataset = load_registry()["seoul_library"]
    for digest in (dataset.dataset_hash, dataset.contract.contract_hash,
                   dataset.probes.catalog_hash):
        assert digest.startswith("sha256:") and len(digest) == 71


def test_hashes_are_stable_across_loads() -> None:
    """hash 가 로드마다 흔들리면 STALE 판정이 거짓이 된다."""
    first, second = load_registry(), load_registry()
    assert first["seoul_library"].contract.contract_hash == \
        second["seoul_library"].contract.contract_hash


def test_neutral_summary_excludes_expectations() -> None:
    """에이전트에게 가는 요약에 기대값·정답이 섞이면 안 된다."""
    summary = load_registry()["seoul_library"].contract.neutral_summary()
    serialized = str(summary)
    assert "expected_record_count" not in summary
    assert "12" not in serialized, "기대 건수가 요약에 노출됐습니다"
    assert "identity_values" not in serialized


def test_probe_catalog_covers_every_audit_requirement() -> None:
    """못 덮는 위험 영역이 있으면 감사는 영원히 완료될 수 없다."""
    dataset = load_registry()["seoul_library"]
    required = {a.risk_id for a in dataset.contract.audit_requirements}
    coverable = {c for p in dataset.probes.probes for c in p.covers}
    assert required <= coverable


def test_baseline_probe_does_not_count_toward_coverage() -> None:
    """AC-04: baseline 과 다른 probe 를 최소 1개 돌려야 감사가 완료된다."""
    catalog = load_registry()["seoul_library"].probes
    baseline = next(p for p in catalog.probes if p.is_baseline)
    assert baseline.covers == ()


def test_rejects_non_https_endpoint(tmp_path) -> None:
    src = load_registry()["seoul_library"].root
    dst = tmp_path / "seoul_library"
    dst.mkdir()
    for child in src.rglob("*"):
        target = dst / child.relative_to(src)
        target.parent.mkdir(parents=True, exist_ok=True)
        if child.is_file():
            target.write_bytes(child.read_bytes())

    data = yaml.safe_load((dst / "dataset.yaml").read_text())
    data["allowed_endpoints"][0]["url_prefix"] = "http://openapi.seoul.go.kr:8088/"
    (dst / "dataset.yaml").write_text(yaml.safe_dump(data, allow_unicode=True))

    with pytest.raises(RegistryInvalid, match="HTTPS"):
        load_dataset(dst)
