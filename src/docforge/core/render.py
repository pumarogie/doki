import atexit
import json
import logging
import queue
import subprocess
import sys
import threading
from concurrent.futures import Future

from docforge.core.tools import ToolError, run

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
    try:
        return run([sys.executable, "-m", "docforge.core.typst_worker"], timeout=timeout, stdin=payload).stdout
    except ToolError as e:
        cause = e.__cause__
        if isinstance(cause, subprocess.TimeoutExpired):
            raise TemplateError(f"template took longer than {timeout:g}s") from e
        if "exited 2:" in str(e):
            raise TemplateError(str(e).split("exited 2:", 1)[1].strip()) from e
        raise


def _html(source: str, data: dict, timeout: float) -> bytes:
    from jinja2 import TemplateError as JinjaError
    from jinja2.sandbox import ImmutableSandboxedEnvironment

    try:
        html = ImmutableSandboxedEnvironment(autoescape=True).from_string(source).render(data=data)
    except JinjaError as e:
        raise TemplateError(str(e)) from e
    return chromium.render(html, timeout)


class _Chromium:
    """One long-lived headless Chromium per process, driven from a dedicated thread.

    Playwright's sync API is bound to the thread that created it, so all browser work is
    funnelled through a queue. One render at a time per process; add contexts in parallel
    when render throughput becomes the bottleneck.
    """

    def __init__(self) -> None:
        self._queue: queue.Queue[tuple[str, float, Future[bytes]] | None] = queue.Queue()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._browser = None
        self._playwright = None

    def render(self, html: str, timeout: float) -> bytes:
        self._ensure_thread()
        fut: Future[bytes] = Future()
        self._queue.put((html, timeout, fut))
        return fut.result(timeout=timeout + 30)

    def browser_id(self) -> int | None:
        """Identity of the live browser object; changes only when the browser is relaunched."""
        return None if self._browser is None else id(self._browser)

    def _ensure_thread(self) -> None:
        with self._lock:
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(target=self._loop, name="chromium-render", daemon=True)
                self._thread.start()

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
            item = self._queue.get()
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
            except Exception as e:  # noqa: BLE001  the render thread must never die silently
                fut.set_exception(ToolError(f"render thread error: {e}"))

    def _close_browser(self) -> None:
        from playwright.sync_api import Error as PlaywrightError

        if self._browser is not None:
            try:
                self._browser.close()
            except PlaywrightError:
                log.info("browser already gone during close")
            self._browser = None

    def shutdown(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            self._queue.put(None)
            self._thread.join(timeout=10)


chromium = _Chromium()
atexit.register(chromium.shutdown)
