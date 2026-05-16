from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, replace
from typing import Callable

logger = logging.getLogger(__name__)

TimeoutCallback = Callable[[str, str], None]


@dataclass(slots=True)
class TopicTask:
    task_id: str
    chat_id: str
    root_message_id: str
    query: str
    status: str = "running"
    current_node: str = ""
    card_message_id: str | None = None
    last_action: str | None = None
    timeout_timer: threading.Timer | None = None


class TopicTaskStore:
    """Thread-safe in-memory task pool for card decisions and timeouts."""

    def __init__(self, wait_seconds: int) -> None:
        self.wait_seconds = wait_seconds
        self._tasks: dict[str, TopicTask] = {}
        self._lock = threading.RLock()

    def create_task(self, task_id: str, chat_id: str, root_message_id: str, query: str) -> TopicTask:
        with self._lock:
            task = TopicTask(
                task_id=task_id,
                chat_id=chat_id,
                root_message_id=root_message_id,
                query=query,
            )
            self._tasks[task_id] = task
            logger.info("Interactive topic task created task_id=%s chat_id=%s", task_id, chat_id)
            return replace(task, timeout_timer=None)

    def mark_waiting(
        self,
        task_id: str,
        node_name: str,
        card_message_id: str | None,
        on_timeout: TimeoutCallback,
    ) -> TopicTask | None:
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                logger.warning("Cannot mark missing interactive topic task waiting task_id=%s", task_id)
                return None
            self._cancel_timer_locked(task)
            timer = threading.Timer(self.wait_seconds, on_timeout, args=(task_id, node_name))
            timer.daemon = True
            task.status = "pending"
            task.current_node = node_name
            task.card_message_id = card_message_id
            task.last_action = None
            task.timeout_timer = timer
            timer.start()
            logger.info(
                "Interactive topic task waiting task_id=%s node=%s timeout_seconds=%s",
                task_id,
                node_name,
                self.wait_seconds,
            )
            return replace(task, timeout_timer=None)

    def confirm_action(
        self,
        task_id: str,
        node_name: str,
        action: str,
        *,
        source: str,
    ) -> TopicTask | None:
        normalized_action = "retry" if action == "retry" else "next"
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                logger.info("Ignoring action for missing interactive topic task task_id=%s", task_id)
                return None
            if task.status != "pending":
                logger.info(
                    "Ignoring duplicate interactive topic action task_id=%s node=%s action=%s status=%s source=%s",
                    task_id,
                    node_name,
                    normalized_action,
                    task.status,
                    source,
                )
                return None
            if task.current_node != node_name:
                logger.info(
                    "Ignoring stale interactive topic action task_id=%s expected_node=%s actual_node=%s action=%s",
                    task_id,
                    task.current_node,
                    node_name,
                    normalized_action,
                )
                return None
            self._cancel_timer_locked(task)
            task.status = "retrying" if normalized_action == "retry" else "confirmed"
            task.last_action = normalized_action
            logger.info(
                "Interactive topic action accepted task_id=%s node=%s action=%s source=%s",
                task_id,
                node_name,
                normalized_action,
                source,
            )
            return replace(task, timeout_timer=None)

    def get_task(self, task_id: str) -> TopicTask | None:
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                return None
            return replace(task, timeout_timer=None)

    def finish_task(self, task_id: str) -> None:
        with self._lock:
            task = self._tasks.pop(task_id, None)
            if task is not None:
                self._cancel_timer_locked(task)
                logger.info("Interactive topic task finished task_id=%s", task_id)

    def _cancel_timer_locked(self, task: TopicTask) -> None:
        timer = task.timeout_timer
        task.timeout_timer = None
        if timer is None:
            return
        try:
            timer.cancel()
        except Exception:
            logger.exception("Failed to cancel interactive topic timer task_id=%s", task.task_id)
