"""Tests for runtime.verifier (M7-T02).

Covers the 6 spec tests:
- two user-hint short-circuits (careful / fast)
- numeric content rule
- PSM/API content rule
- concept-only question short-circuit
- max_rounds REVISE loop terminates with verified=False
"""

from agent_runtime.verifier import (
    TriggerDecision,
    VerifyResult,
    should_trigger,
    verify,
)


def test_user_careful_hint_triggers():
    d = should_trigger("X", "Y", user_hint="请仔细点回答这个")
    assert isinstance(d, TriggerDecision)
    assert d.trigger is True
    assert d.reason == "user_explicit_careful"


def test_user_fast_hint_skips():
    d = should_trigger("X", "Y", user_hint="快速看下就行")
    assert d.trigger is False
    assert d.reason == "user_explicit_fast"


def test_quantitative_claim_triggers():
    """Quantitative claim = number + unit (qps/ms/%/GB/...). Verifier
    should trigger because misstating these is actionably wrong."""
    d = should_trigger("限流多少", "限流值是 100 qps，超过 200 报错")
    assert d.trigger is True
    assert d.reason == "quantitative_claim"


def test_quantitative_misc_units_trigger():
    """Spot-check several units to lock in coverage."""
    for sample in [
        "p99 是 250ms",
        "占用 12 GB",
        "命中率 85%",
        "处理 30 个订单",
        "重试 5 次",
    ]:
        d = should_trigger("?", sample)
        assert d.trigger is True, f"missed quant claim in: {sample!r}"
        assert d.reason == "quantitative_claim"


def test_bare_id_or_error_code_does_not_trigger():
    """Log IDs / error codes / trace IDs are labels not claims — they
    must NOT trigger verifier (was the dominant false-positive that wasted
    ~70% of verifier budget pre-fix)."""
    samples = [
        "错误码 230027 表示 Permission denied",
        "log_id=02177815274372 检查日志",
        "errorCode=10005 errorMessage=Duplicate entry",
        "trace id 6955348451890610178 已记录",
        "日志 ID：021778152743723fdbdfdbdfdbdfdbd00000000000003e7bb35c8",
    ]
    for s in samples:
        d = should_trigger("分析报警", s)
        assert d.trigger is False, (
            f"bare-id case wrongly triggered: {s!r} (reason={d.reason})"
        )


def test_psm_answer_triggers():
    d = should_trigger("billing PSM", "PSM 是 lark.apaas.spring_billing")
    assert d.trigger is True
    assert d.reason == "service_or_api"


def test_concept_question_skips():
    d = should_trigger("什么是 entitlement", "权益模型是一种通用授权抽象")
    assert d.trigger is False
    assert d.reason == "concept_query"


async def test_revise_loop_stops_at_max_rounds():
    """If verifier always returns REVISE, verify() returns verified=False after max_rounds."""
    call_count = 0

    async def always_revise(*, work_dir, question, draft):
        nonlocal call_count
        call_count += 1
        return "REVISE:\n- still wrong"

    r = await verify(
        work_dir="/tmp/fake",
        question="X",
        draft_answer="Y",
        max_rounds=2,
        _runner=always_revise,
    )
    assert isinstance(r, VerifyResult)
    assert r.verified is False
    assert r.rounds_used == 2
    assert "still wrong" in r.concerns
    assert call_count == 2


def test_psm_regex_excludes_version_and_date_and_pkg():
    """PSM rule must not false-positive on common dotted strings that aren't PSMs."""
    for not_psm in ["1.0.0", "2026.04.23", "v1.2.3", "file.tar.gz", "Foo.Bar.Baz"]:
        d = should_trigger("?", f"我们 {not_psm} 上线")
        assert d.reason != "service_or_api", f"{not_psm} wrongly classified as PSM"


def test_parse_output_passport_does_not_pass():
    """PASSPORT/PASSAGE etc. must NOT be interpreted as PASS."""
    from agent_runtime.verifier import _parse_output

    verdict, concerns = _parse_output("PASSPORT required for verification")
    assert verdict == "REVISE"
    assert "unparseable" in concerns[0].lower() or "PASSPORT" in concerns[0]
