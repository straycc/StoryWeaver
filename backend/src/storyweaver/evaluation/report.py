"""把评测运行指标汇总为可直接检查的 JSON 报告。"""

from __future__ import annotations

from .models import EvaluationRunResult, GroupRunResult


def build_operational_summary(result: EvaluationRunResult) -> dict[str, object]:
    """汇总成功率、模型调用、Token 和端到端耗时。"""

    return {
        "schema_version": 1,
        "run_id": result.run_id,
        "case_id": result.case_id,
        "requested_chapters": result.requested_chapters,
        "status": result.status,
        "groups": {
            "bare": _group_summary(result.bare, result.requested_chapters),
            "storyweaver": _group_summary(
                result.storyweaver, result.requested_chapters
            ),
        },
        "quality": {
            "status": "pending_manual_or_model_grading",
            "note": "质量结论必须来自 blind/scorecard.csv 或模型盲评，不能由运行指标推断。",
        },
    }


def _group_summary(group: GroupRunResult, requested_chapters: int) -> dict[str, object]:
    succeeded = sum(item.succeeded for item in group.metrics)
    chapter_elapsed = sum(item.elapsed_seconds for item in group.metrics)
    setup_tokens = group.setup_input_tokens + group.setup_output_tokens
    chapter_tokens = sum(item.total_tokens for item in group.metrics)
    total_elapsed = group.setup_elapsed_seconds + chapter_elapsed
    total_tokens = setup_tokens + chapter_tokens
    return {
        "generated_chapters": len(group.chapters),
        "succeeded_chapters": succeeded,
        "success_rate": round(succeeded / requested_chapters, 4),
        "model_calls": group.setup_model_calls
        + sum(item.model_calls for item in group.metrics),
        "failed_model_calls": group.setup_failed_model_calls
        + sum(item.failed_model_calls for item in group.metrics),
        "retry_count": group.setup_retry_count
        + sum(item.retry_count for item in group.metrics),
        "input_tokens": group.setup_input_tokens
        + sum(item.input_tokens for item in group.metrics),
        "output_tokens": group.setup_output_tokens
        + sum(item.output_tokens for item in group.metrics),
        "total_tokens": total_tokens,
        "elapsed_seconds": round(total_elapsed, 3),
        "setup": {
            "model_calls": group.setup_model_calls,
            "failed_model_calls": group.setup_failed_model_calls,
            "retry_count": group.setup_retry_count,
            "input_tokens": group.setup_input_tokens,
            "output_tokens": group.setup_output_tokens,
            "elapsed_seconds": round(group.setup_elapsed_seconds, 3),
            "error": group.setup_error,
        },
        "average_tokens_per_generated_chapter": round(
            total_tokens / len(group.chapters), 2
        )
        if group.chapters
        else None,
        "average_seconds_per_generated_chapter": round(
            total_elapsed / len(group.chapters), 3
        )
        if group.chapters
        else None,
    }
