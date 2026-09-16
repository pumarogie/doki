import logging
import subprocess
from collections.abc import Sequence
from pathlib import PurePath

log = logging.getLogger(__name__)


class ToolError(RuntimeError):
    """An external binary failed. The message is for logs, never for clients."""


def run(
    args: Sequence[str], timeout: float = 120, ok_codes: Sequence[int] = (0,), stdin: bytes | None = None
) -> subprocess.CompletedProcess[bytes]:
    from doki.observability import TOOL_SECONDS

    try:
        with TOOL_SECONDS.labels(PurePath(args[0]).name).time():
            proc = subprocess.run(list(args), input=stdin, capture_output=True, timeout=timeout, check=False)
    except FileNotFoundError as e:
        raise ToolError(f"binary not found: {args[0]}") from e
    except subprocess.TimeoutExpired as e:
        raise ToolError(f"{args[0]} timed out after {timeout}s") from e
    if proc.returncode not in ok_codes:
        raise ToolError(f"{args[0]} exited {proc.returncode}: {proc.stderr[-2000:].decode(errors='replace')}")
    return proc
