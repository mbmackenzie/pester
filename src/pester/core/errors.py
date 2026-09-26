from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pester.core.states import JobStatus


class PesterError(Exception):
    pass


class JobNotFoundError(PesterError):
    def __init__(self, job_id: str) -> None:
        super().__init__(f"job {job_id!r} not found")
        self.job_id = job_id


class IdempotencyConflictError(PesterError):
    """The same id was submitted again with different content."""


class IllegalTransitionError(PesterError):
    def __init__(self, current: "JobStatus", target: "JobStatus") -> None:
        super().__init__(f"illegal transition {current} -> {target}")
        self.current = current
        self.target = target
