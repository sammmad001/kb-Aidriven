"""V1.2: PipelineTrace — structured observability for query pipeline steps.

Provides per-query trace IDs and step-level input/output/duration logging,
replacing the previous ad-hoc logger.info calls that couldn't be correlated
across pipeline stages.

Example log output with trace:
  [trace:a1b2c3] understand → entities=["海力士","存储","知识"] (2ms)
  [trace:a1b2c3] resolve → resolved=2, unresolved=0 (5ms)
  [trace:a1b2c3] retrieve → nodes=3, paths=1 (12ms)
  [trace:a1b2c3] gate → SUFFICIENT
  [trace:a1b2c3] generate → answer_len=342, confidence=0.85 (1500ms)
"""

from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass
class StepTrace:
    """Record of a single pipeline step's execution."""
    step: str
    input_summary: str = ""
    output_summary: str = ""
    duration_ms: float = 0.0
    error: str | None = None


@dataclass
class PipelineTrace:
    """Full trace of a single query pipeline execution.

    Usage:
        trace = PipelineTrace()
        trace.step_start("understand", entities=question)
        # ... do work ...
        trace.step_end(output_summary="entities=3", duration_ms=2.0)
        trace.log()
    """

    trace_id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    steps: list[StepTrace] = field(default_factory=list)
    _current_step: str | None = field(default=None, init=False)
    _current_start: float = field(default=0.0, init=False)

    def step_start(self, step: str, **inputs: object) -> None:
        """Begin timing a pipeline step."""
        self._current_step = step
        self._current_start = time.monotonic()
        input_summary = ", ".join(f"{k}={v!r}" for k, v in inputs.items())[:120]
        self.steps.append(StepTrace(step=step, input_summary=input_summary))

    def step_end(self, output_summary: str = "", error: str | None = None) -> None:
        """End timing and record the step result."""
        duration = (time.monotonic() - self._current_start) * 1000
        if self.steps:
            step = self.steps[-1]
            step.output_summary = output_summary[:200]
            step.duration_ms = round(duration, 1)
            step.error = error
        self._current_step = None

    def log(self) -> None:
        """Emit a structured log line for each step in the trace."""
        for step in self.steps:
            parts = [f"[trace:{self.trace_id}] {step.step}"]
            if step.input_summary:
                parts.append(f"in=({step.input_summary})")
            if step.output_summary:
                parts.append(f"out=({step.output_summary})")
            if step.error:
                parts.append(f"err={step.error}")
            parts.append(f"({step.duration_ms:.0f}ms)")
            if step.error:
                logger.warning(" ".join(parts))
            else:
                logger.info(" ".join(parts))
