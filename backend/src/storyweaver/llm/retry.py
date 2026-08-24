"""模型调用的有限重试策略。

重试是应用层恢复策略，不管理模型消息、工具或 Agent 状态。
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import TypeVar

from tenacity import wait_exponential_jitter


_LOGGER = logging.getLogger(__name__)
ResultT = TypeVar("ResultT")
RetryOperation = Callable[["RetryContext"], Awaitable[ResultT]]


class RetryCategory(StrEnum):
    TRANSIENT = "transient"
    OUTPUT_FORMAT = "output_format"
    DOMAIN_VALIDATION = "domain_validation"
    NON_RETRYABLE = "non_retryable"


@dataclass(frozen=True, slots=True)
class WorkerRetryPolicy:
    max_attempts: int = 3
    max_repairs: int = 1
    initial_delay_seconds: float = 0.5
    backoff_factor: float = 2.0
    max_delay_seconds: float = 8.0
    jitter: bool = True

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError("max_attempts 必须大于 0")
        if not 0 <= self.max_repairs < self.max_attempts:
            raise ValueError("max_repairs 必须在 0 到 max_attempts-1 之间")
        if self.initial_delay_seconds < 0 or self.max_delay_seconds < 0:
            raise ValueError("重试等待时间不能小于 0")
        if self.backoff_factor < 1:
            raise ValueError("backoff_factor 不能小于 1")


@dataclass(frozen=True, slots=True)
class RetryContext:
    attempt: int
    max_attempts: int
    repair_error: str | None = None

    @property
    def is_repair(self) -> bool:
        return self.repair_error is not None


def classify_retry_error(error: BaseException) -> RetryCategory:
    """按稳定错误语义区分重投、修复与立即失败。"""

    detail = f"{type(error).__name__}: {error}".casefold()
    if isinstance(error, asyncio.CancelledError):
        return RetryCategory.NON_RETRYABLE
    if re.search(r"http\s+(?:400|401|402|403|404|405|409|422)\b", detail):
        return RetryCategory.NON_RETRYABLE
    if any(value in detail for value in ("insufficient balance", "余额不足", "api key", "用户取消")):
        return RetryCategory.NON_RETRYABLE
    if isinstance(error, TimeoutError) or any(value in detail for value in (
        "timeout", "timed out", "超时", "无法连接", "connection reset",
        "connection refused", "temporarily unavailable", "http 408", "http 429",
        "http 500", "http 502", "http 503", "http 504", "空 content", "content 为空",
        "空正文", "非空章节正文",
    )):
        return RetryCategory.TRANSIENT
    if any(value in detail for value in (
        "json", "结构化输出", "无法转换", "解析失败", "缺少字段", "未知字段",
        "输出类型不符合", "serializationerror", "modelresponseerror",
        "workerexecutionerror",
    )):
        return RetryCategory.OUTPUT_FORMAT
    if any(value in detail for value in (
        "validation", "章节号不一致", "编号不连续", "正文过长", "正文过短",
        "标题不能", "未知角色", "未知伏笔", "已解决伏笔", "直接冲突",
        "不能学习不存在", "不得复用已有",
    )):
        return RetryCategory.DOMAIN_VALIDATION
    return RetryCategory.NON_RETRYABLE


async def run_with_retry(
    *,
    worker_name: str,
    operation: RetryOperation[ResultT],
    policy: WorkerRetryPolicy | None = None,
) -> ResultT:
    """执行有限重试；格式与领域失败将错误回传给下一次修复调用。"""

    configured = policy or WorkerRetryPolicy()
    repairs = 0
    transient_retries = 0
    repair_error: str | None = None
    for attempt in range(1, configured.max_attempts + 1):
        try:
            return await operation(RetryContext(attempt, configured.max_attempts, repair_error))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            category = classify_retry_error(exc)
            can_retry = attempt < configured.max_attempts
            delay = 0.0
            if category in {RetryCategory.OUTPUT_FORMAT, RetryCategory.DOMAIN_VALIDATION}:
                if repairs >= configured.max_repairs:
                    can_retry = False
                else:
                    repairs += 1
                    repair_error = f"{type(exc).__name__}: {exc}"
            elif category is RetryCategory.TRANSIENT:
                transient_retries += 1
                if configured.initial_delay_seconds:
                    wait = wait_exponential_jitter(
                        initial=configured.initial_delay_seconds,
                        max=configured.max_delay_seconds,
                        exp_base=configured.backoff_factor,
                    )
                    delay = wait(type("State", (), {"attempt_number": transient_retries})())
                    if not configured.jitter:
                        delay = min(
                            configured.max_delay_seconds,
                            configured.initial_delay_seconds * configured.backoff_factor ** (transient_retries - 1),
                        )
            else:
                can_retry = False
            if not can_retry:
                raise
            _LOGGER.warning(
                "Worker 重试 worker=%s attempt=%d/%d category=%s delay_seconds=%.2f error_type=%s error=%s",
                worker_name, attempt + 1, configured.max_attempts, category.value,
                delay, type(exc).__name__, exc,
            )
            if delay:
                await asyncio.sleep(delay)
    raise AssertionError("有限重试未产生终态")
