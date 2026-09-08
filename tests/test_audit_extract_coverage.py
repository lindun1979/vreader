"""存量覆盖审计脚本测试（plan v4 Step 7）：v2/legacy/多重反解分类正确；只读（fixture
SHA-256 前后不变）。"""
import hashlib
import json

from ops import audit_extract_coverage as audit
from core import models as m

TX = ("青铜这题 GLM-5.3 一次就修对了。字节的 Step 3.7 Flash 也来挑战，第二轮修好。")


def _v2_rec(series, version, canonical, variant=""):
    return {"model_canonical": canonical, "model_raw": canonical, "model_series": series,
            "model_version": version, "model_variant": variant, "bug_level": "青铜",
            "bug_id": "B001", "solved_round": 1, "score": 1, "rounds": 1, "solved": True,
            "evidence_quote": "十个字以上的证据片段内容", "confidence": 0.9}


def _v2_extract(records):
    return {"schema_rev": 2, "video_id": "v1", "title": "", "extracted_at": "x",
            "extractor_version": "token_bug/2", "prompt_hash": "ph",
            "asr_model": "gladia-v2", "records": records}


def _legacy_rec(canonical):
    return {"model_canonical": canonical, "model_raw": canonical, "bug_level": "青铜",
            "bug_id": "B001", "score": 1, "rounds": 1, "solved": True,
            "evidence_quote": "十个字以上的证据片段内容", "confidence": 0.9}


def _legacy_extract(records):
    return {"video_id": "v1", "title": "", "extracted_at": "x",
            "extractor_version": "token_bug/1", "prompt_hash": "ph",
            "asr_model": "gladia-v2", "records": records}


def _covered(records):
    return audit._covered_series(records, m.load_series())


def test_v2_series_covered():
    covered, undec = _covered([_v2_rec("GLM", "5.3", "GLM-5.3"),
                               _v2_rec("Step", "3.7", "Step 3.7 Flash", "Flash")])
    assert covered == {"GLM", "Step"} and undec == []


def test_legacy_unique_reverse_parse_covers():
    covered, undec = _covered([_legacy_rec("GLM-5.3")])
    assert "GLM" in covered and undec == []


def test_unknown_record_covers_nothing():
    covered, undec = _covered([{"model_canonical": "UNKNOWN", "model_series": ""}])
    assert covered == set() and undec == []


def test_missing_series_flagged():
    # 转写提及 GLM+Step，只提取到 GLM → Step 疑似丢弃
    covered, _ = _covered([_v2_rec("GLM", "5.3", "GLM-5.3")])
    anchors = m.build_anchors(TX, "", data=m.load_series())
    missing = set(anchors["versions"]) - covered
    assert missing == {"Step"}


def test_audit_is_read_only(tmp_path):
    """跑 audit 前后所有 fixture 文件 SHA-256 不变（只读证明）。"""
    ch = tmp_path / "token_bug" / "v1"
    ch.mkdir(parents=True)
    (ch / "transcript.txt").write_text(TX, encoding="utf-8")
    (ch / "extract.json").write_text(
        json.dumps(_v2_extract([_v2_rec("GLM", "5.3", "GLM-5.3")]), ensure_ascii=False),
        encoding="utf-8")

    def digest(root):
        return {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in sorted(root.rglob("*")) if p.is_file()}

    before = digest(tmp_path)
    suspects = audit.audit(tmp_path)
    after = digest(tmp_path)
    assert before == after          # 无任何文件被改写/新增
    assert suspects == 1            # Step 疑似丢弃
