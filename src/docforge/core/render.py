import atexit
import json
import logging
import os
import queue
import select
import struct
import subprocess
import sys
import threading
import time
from concurrent.futures import Future

from docforge.core.tools import ToolError

log = logging.getLogger(__name__)

ENGINES = ("typst", "html")


class TemplateError(ValueError):
    """The template itself is broken; the message is safe to show to its author."""


def render(engine: str, source: str, data: dict, pdfa: bool = False, timeout: float = 30) -> bytes:
    if engine == "typst":
        return _typst(source, data, pdfa, timeout)
    if engine == "html":
        return _html(source, data, timeout)
    raise ValueError(f"engine must be one of {ENGINES}")


def _typst(source: str, data: dict, pdfa: bool, timeout: float) -> bytes:
    payload = json.dumps({"source": source, "data": data, "pdfa": pdfa}).encode()
    from docforge.observability import timed

    with timed("typst"):
        status, body = typst_pool.compile(payload, timeout)
    if status == 0:
        return body
    raise TemplateError(body.decode(errors="replace"))


class _TypstProcess:
    """One warm compiler process. Killed and restarted on timeout, crash, or after MAX_RENDERS."""

    MAX_RENDERS = 200

    def __init__(self) -> None:
        self.proc: subprocess.Popen[bytes] | None = None
        self.renders = 0

    def pid(self) -> int | None:
        return self.proc.pid if self.proc is not None and self.proc.poll() is None else None

    def _start(self) -> subprocess.Popen[bytes]:
        self.kill()
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "docforge.core.typst_worker"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        )
        self.renders = 0
        return self.proc

    def kill(self) -> None:
        if self.proc is not None and self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait(timeout=5)
        self.proc = None

    def compile(self, payload: bytes, timeout: float) -> tuple[int, bytes]:
        proc = self.proc
        if proc is None or proc.poll() is not None or self.renders >= self.MAX_RENDERS:
            proc = self._start()
        assert proc.stdin is not None and proc.stdout is not None
        deadline = time.monotonic() + timeout
        try:
            proc.stdin.write(struct.pack(">I", len(payload)) + payload)
            proc.stdin.flush()
            status, length = struct.unpack(">BI", self._read(proc.stdout, 5, deadline))
            body = self._read(proc.stdout, length, deadline)
        except TimeoutError:
            self.kill()
            raise TemplateError(f"template took longer than {timeout:g}s") from None
        except (BrokenPipeError, EOFError, OSError) as e:
            self.kill()
            raise ToolError(f"typst worker died: {e}") from e
        self.renders += 1
        return status, body

    @staticmethod
    def _read(stream, n: int, deadline: float) -> bytes:
        fd = stream.fileno()
        buf = b""
        while len(buf) < n:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not select.select([fd], [], [], remaining)[0]:
                raise TimeoutError
            chunk = os.read(fd, n - len(buf))
            if not chunk:
                raise EOFError("worker closed its output")
            buf += chunk
        return buf


class _TypstPool:
    def __init__(self, size: int) -> None:
        self.size = max(1, size)
        self._free: queue.Queue[_TypstProcess] = queue.Queue()
        self.processes = [_TypstProcess() for _ in range(self.size)]
        for p in self.processes:
            self._free.put(p)

    def compile(self, payload: bytes, timeout: float) -> tuple[int, bytes]:
        proc = self._free.get()
        try:
            return proc.compile(payload, timeout)
        finally:
            self._free.put(proc)

    def shutdown(self) -> None:
        for p in self.processes:
            p.kill()


def _html(source: str, data: dict, timeout: float) -> bytes:
    from jinja2 import TemplateError as JinjaError
    from jinja2.sandbox import ImmutableSandboxedEnvironment

    try:
        html = ImmutableSandboxedEnvironment(autoescape=True).from_string(source).render(data=data)
    except JinjaError as e:
        raise TemplateError(str(e)) from e
    from docforge.observability import timed

    with timed("chromium"):
        return chromium.render(html, timeout)


class _BrowserWorker:
    """One thread owning one headless Chromium. Playwright's sync API is bound to its creating thread."""

    def __init__(self, jobs: "queue.Queue[tuple[str, float, Future[bytes]] | None]", name: str) -> None:
        self._jobs = jobs
        self._browser = None
        self._playwright = None
        self.served = 0
        self.thread = threading.Thread(target=self._loop, name=name, daemon=True)
        self.thread.start()

    def browser_id(self) -> int | None:
        return None if self._browser is None else id(self._browser)

    def _launch(self):
        from playwright.sync_api import sync_playwright

        if self._playwright is None:
            self._playwright = sync_playwright().start()
        # Docker gives /dev/shm only 64 MB; let Chromium use /tmp instead.
        self._browser = self._playwright.chromium.launch(args=["--disable-dev-shm-usage"])
        return self._browser

    def _render_once(self, html: str, timeout: float) -> bytes:
        browser = self._browser if self._browser is not None and self._browser.is_connected() else self._launch()
        # No JavaScript, no network: HTML templates are untrusted.
        context = browser.new_context(java_script_enabled=False, offline=True)
        try:
            page = context.new_page()
            page.route("**/*", lambda route: route.abort())
            page.set_content(html, wait_until="load", timeout=timeout * 1000)
            return page.pdf(format="A4", print_background=True, prefer_css_page_size=True)
        finally:
            context.close()

    def _loop(self) -> None:
        from playwright.sync_api import Error as PlaywrightError

        while True:
            item = self._jobs.get()
            if item is None:
                break
            html, timeout, fut = item
            try:
                try:
                    fut.set_result(self._render_once(html, timeout))
                except PlaywrightError:
                    # Browser died or a page hung: relaunch once, then give up on this request.
                    self._close_browser()
                    try:
                        fut.set_result(self._render_once(html, timeout))
                    except PlaywrightError as e:
                        fut.set_exception(ToolError(f"chromium failed: {e}"))
            except Exception as e:  # noqa: BLE001  a worker thread must never die silently
                fut.set_exception(ToolError(f"render thread error: {e}"))
            finally:
                self.served += 1
        self._close_browser()

    def _close_browser(self) -> None:
        from playwright.sync_api import Error as PlaywrightError

        if self._browser is not None:
            try:
                self._browser.close()
            except PlaywrightError:
                log.info("browser already gone during close")
            self._browser = None


class _Chromium:
    """A small pool of long-lived headless browsers fed from one queue. Started on first use."""

    def __init__(self, size: int) -> None:
        self.size = max(1, size)
        self._jobs: queue.Queue[tuple[str, float, Future[bytes]] | None] = queue.Queue()
        self._lock = threading.Lock()
        self.workers: list[_BrowserWorker] = []

    def render(self, html: str, timeout: float) -> bytes:
        self._ensure_started()
        fut: Future[bytes] = Future()
        self._jobs.put((html, timeout, fut))
        return fut.result(timeout=timeout + 30)

    def browser_ids(self) -> set[int]:
        return {i for w in self.workers if (i := w.browser_id()) is not None}

    def _ensure_started(self) -> None:
        with self._lock:
            self.workers = [w for w in self.workers if w.thread.is_alive()]
            while len(self.workers) < self.size:
                self.workers.append(_BrowserWorker(self._jobs, f"chromium-{len(self.workers)}"))

    def shutdown(self) -> None:
        for _ in self.workers:
            self._jobs.put(None)
        for w in self.workers:
            w.thread.join(timeout=10)


chromium = _Chromium(int(os.environ.get("RENDER_WORKERS", "2")))
atexit.register(chromium.shutdown)
typst_pool = _TypstPool(int(os.environ.get("TYPST_WORKERS", "2")))
atexit.register(typst_pool.shutdown)
