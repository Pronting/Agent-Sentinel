from __future__ import annotations

import functools
import time
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from typing import Any, TypeVar

try:
    from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest
except ImportError:  # pragma: no cover - keeps local test env usable before dependencies are installed.
    CONTENT_TYPE_LATEST = "text/plain; version=0.0.4; charset=utf-8"

    class _NoopMetric:
        def labels(self, **_: str) -> "_NoopMetric":
            return self

        def inc(self, _: float = 1.0) -> None:
            return None

        def dec(self, _: float = 1.0) -> None:
            return None

        def observe(self, _: float) -> None:
            return None

    class Counter(_NoopMetric):  # type: ignore[no-redef]
        def __init__(self, *_: object, **__: object) -> None:
            return None

    class Gauge(_NoopMetric):  # type: ignore[no-redef]
        def __init__(self, *_: object, **__: object) -> None:
            return None

    class Histogram(_NoopMetric):  # type: ignore[no-redef]
        def __init__(self, *_: object, **__: object) -> None:
            return None

    def generate_latest() -> bytes:  # type: ignore[no-redef]
        return b"# prometheus_client is not installed\n"


F = TypeVar("F", bound=Callable[..., Awaitable[Any]])
DEFAULT_GROUP_ID = "unknown"
DEFAULT_WORKFLOW_TYPE = "diagnosis"


class Monitor:
    """Manual Prometheus metrics for the AIOps LangGraph and Feishu workflow."""

    def __init__(self) -> None:
        self.enabled = True

        # Node execution latency by LangGraph node name and group id.
        self.workflow_node_duration_seconds = Histogram(
            "workflow_node_duration_seconds",
            "LangGraph node execution duration in seconds.",
            ["node_name", "group_id"],
            buckets=(0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60),
        )
        self.workflow_total_duration_seconds = Histogram(
            "workflow_total_duration_seconds",
            "End-to-end workflow duration in seconds.",
            ["workflow_type", "group_id"],
            buckets=(0.1, 0.5, 1, 2.5, 5, 10, 30, 60, 120, 300),
        )
        self.llm_call_duration_seconds = Histogram(
            "llm_call_duration_seconds",
            "LLM call duration in seconds.",
            ["model"],
            buckets=(0.1, 0.5, 1, 2.5, 5, 10, 30, 60),
        )

        # Counters for workflow volume, cache quality, retrieval and errors.
        self.workflow_node_count_total = Counter(
            "workflow_node_count_total",
            "Total LangGraph node invocations.",
            ["node_name", "group_id", "status"],
        )
        self.workflow_cache_hit_total = Counter(
            "workflow_cache_hit_total",
            "Similar-case cache hit count.",
            ["group_id"],
        )
        self.workflow_cache_miss_total = Counter(
            "workflow_cache_miss_total",
            "Similar-case cache miss count.",
            ["group_id"],
        )
        self.rag_retrieval_count_total = Counter(
            "rag_retrieval_count_total",
            "RAG retrieval request count.",
            ["retriever", "group_id"],
        )
        self.tool_call_count_total = Counter(
            "tool_call_count_total",
            "Tool call count.",
            ["tool_name", "group_id", "status"],
        )
        self.feedback_positive_total = Counter(
            "feedback_positive_total",
            "Positive user feedback count.",
            ["group_id"],
        )
        self.feedback_negative_total = Counter(
            "feedback_negative_total",
            "Negative user feedback count.",
            ["group_id"],
        )
        self.error_count_total = Counter(
            "error_count_total",
            "Application error count by error type.",
            ["error_type"],
        )
        self.token_consumption_total = Counter(
            "token_consumption_total",
            "LLM token consumption.",
            ["model", "token_type"],
        )
        self.current_workflow_active = Gauge(
            "current_workflow_active",
            "Currently active workflows.",
            ["workflow_type", "group_id"],
        )

    def configure(self, *, enabled: bool) -> None:
        self.enabled = enabled

    @contextmanager
    def track_node(self, node_name: str, group_id: str | None = None) -> Iterator[None]:
        """Context manager example: `with monitor.track_node("retrieve", group_id): ...`."""
        if not self.enabled:
            yield
            return
        labels = {"node_name": node_name, "group_id": _label(group_id)}
        started = time.perf_counter()
        status = "success"
        try:
            yield
        except Exception:
            status = "error"
            self.record_error(f"{node_name}_error")
            raise
        finally:
            self.workflow_node_duration_seconds.labels(**labels).observe(time.perf_counter() - started)
            self.workflow_node_count_total.labels(**labels, status=status).inc()

    def track_node_async(self, node_name: str) -> Callable[[F], F]:
        """Async decorator example: `@monitor.track_node_async("understand")`."""

        def decorator(func: F) -> F:
            @functools.wraps(func)
            async def wrapper(state: dict[str, Any], *args: Any, **kwargs: Any) -> Any:
                with self.track_node(node_name, group_id_from_state(state)):
                    return await func(state, *args, **kwargs)

            return wrapper  # type: ignore[return-value]

        return decorator

    @contextmanager
    def track_workflow(self, workflow_type: str, group_id: str | None = None) -> Iterator[None]:
        if not self.enabled:
            yield
            return
        labels = {"workflow_type": workflow_type, "group_id": _label(group_id)}
        started = time.perf_counter()
        self.current_workflow_active.labels(**labels).inc()
        try:
            yield
        except Exception:
            self.record_error(f"{workflow_type}_workflow_error")
            raise
        finally:
            self.workflow_total_duration_seconds.labels(**labels).observe(time.perf_counter() - started)
            self.current_workflow_active.labels(**labels).dec()

    def record_llm_call(self, model: str, duration_seconds: float) -> None:
        if self.enabled:
            self.llm_call_duration_seconds.labels(model=_label(model)).observe(duration_seconds)

    def record_tokens(self, model: str, *, prompt_tokens: int = 0, completion_tokens: int = 0) -> None:
        if not self.enabled:
            return
        self.token_consumption_total.labels(model=_label(model), token_type="prompt").inc(max(prompt_tokens, 0))
        self.token_consumption_total.labels(model=_label(model), token_type="completion").inc(max(completion_tokens, 0))

    def record_cache_hit(self, group_id: str | None = None) -> None:
        if self.enabled:
            self.workflow_cache_hit_total.labels(group_id=_label(group_id)).inc()

    def record_cache_miss(self, group_id: str | None = None) -> None:
        if self.enabled:
            self.workflow_cache_miss_total.labels(group_id=_label(group_id)).inc()

    def record_rag_retrieval(self, retriever: str, group_id: str | None = None) -> None:
        if self.enabled:
            self.rag_retrieval_count_total.labels(retriever=_label(retriever), group_id=_label(group_id)).inc()

    def record_tool_call(self, tool_name: str, group_id: str | None = None, *, status: str = "success") -> None:
        if self.enabled:
            self.tool_call_count_total.labels(tool_name=_label(tool_name), group_id=_label(group_id), status=_label(status)).inc()

    def record_feedback(self, positive: bool, group_id: str | None = None) -> None:
        if not self.enabled:
            return
        if positive:
            self.feedback_positive_total.labels(group_id=_label(group_id)).inc()
        else:
            self.feedback_negative_total.labels(group_id=_label(group_id)).inc()

    def record_error(self, error_type: str) -> None:
        if self.enabled:
            self.error_count_total.labels(error_type=_label(error_type)).inc()

    def render_latest(self) -> bytes:
        return generate_latest()


monitor = Monitor()


def configure_monitoring(*, enabled: bool) -> None:
    monitor.configure(enabled=enabled)


def group_id_from_state(state: dict[str, Any] | None) -> str:
    if not isinstance(state, dict):
        return DEFAULT_GROUP_ID
    return _label(state.get("chat_id"))


def trace_id_from_parts(chat_id: str | None, message_id: str | None) -> str:
    if chat_id and message_id:
        return f"{chat_id}_{message_id}"
    return chat_id or message_id or "unknown"


def trace_id_from_state(state: dict[str, Any] | None) -> str:
    if not isinstance(state, dict):
        return "unknown"
    existing = str(state.get("trace_id") or "").strip()
    if existing:
        return existing
    return trace_id_from_parts(str(state.get("chat_id") or ""), str(state.get("thread_root_message_id") or ""))


def _label(value: object) -> str:
    text = str(value or DEFAULT_GROUP_ID).strip()
    return text or DEFAULT_GROUP_ID
