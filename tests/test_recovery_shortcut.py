"""2j：恢复起点按产物前推——产物齐全时不因上游（抖音 detail API）不通而卡死。"""
import json

from core import config, db, douyin, pipeline

TRANSCRIPT = "青铜这题 GLM-5.3 一次就修对了，表现不错，全程逻辑清晰。"


def _extract(aid):
    return {
        "video_id": aid, "title": "标题", "extracted_at": "2026-01-01T00:00:00",
        "extractor_version": "token_bug/1", "prompt_hash": "abc123",
        "asr_model": "gladia-v2",
        "records": [{
            "model_canonical": "GLM-5.3", "model_raw": "glm5.3", "bug_level": "青铜",
            "bug_id": "b1", "score": 1, "rounds": 1, "solved": True,
            "evidence_quote": "GLM-5.3 一次就修对了", "confidence": 0.9,
        }],
    }


def test_recovery_shortcut_when_upstream_down(conn, data_dir, monkeypatch):
    aid = "rec1"
    vd = config.video_dir("token_bug", aid)
    vd.mkdir(parents=True)
    (vd / "transcript.txt").write_text(TRANSCRIPT, encoding="utf-8")
    (vd / "extract.json").write_text(json.dumps(_extract(aid)), encoding="utf-8")
    db.insert_task(conn, aweme_id=aid, channel="token_bug", raw_link="x",
                   chat_id="c", sender_id="s", title="t")

    def boom(*a, **k):
        raise douyin.DownloadError("抖音 detail 挂了")
    monkeypatch.setattr(douyin, "fetch_detail", boom)
    monkeypatch.setattr(douyin, "download", boom)

    status = pipeline.process_task(conn, db.get_task(conn, aid))
    assert status == db.SUCCEEDED
    # 上榜（confidence 0.9 ≥ 阈值 → auto_ok）
    assert db.board_visible_ids(conn), "应有可见记录"
