"""Cross-platform event-loop runner compatible with psycopg async."""

import asyncio
import selectors
import sys
from collections.abc import Coroutine
from typing import Any


def _selector_event_loop() -> asyncio.AbstractEventLoop:
    return asyncio.SelectorEventLoop(selectors.SelectSelector())


def run_async[ResultT](coroutine: Coroutine[Any, Any, ResultT]) -> ResultT:
    """Run a top-level coroutine with a psycopg-compatible loop on Windows."""
    if sys.platform == "win32":
        return asyncio.run(coroutine, loop_factory=_selector_event_loop)
    return asyncio.run(coroutine)
