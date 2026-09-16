import json
import subprocess
import sys

from docforge.core.tools import ToolError, run

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
    from playwright.sync_api import Error as PlaywrightError
    from playwright.sync_api import sync_playwright

    try:
        html = ImmutableSandboxedEnvironment(autoescape=True).from_string(source).render(data=data)
    except JinjaError as e:
        raise TemplateError(str(e)) from e

    # ponytail: one browser per render (~0.5s). Keep a warm browser pool when volume grows.
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            try:
                # No JavaScript, no network: HTML templates are untrusted.
                context = browser.new_context(java_script_enabled=False, offline=True)
                page = context.new_page()
                page.route("**/*", lambda route: route.abort())
                page.set_content(html, wait_until="load", timeout=timeout * 1000)
                return page.pdf(format="A4", print_background=True, prefer_css_page_size=True)
            finally:
                browser.close()
    except PlaywrightError as e:
        raise ToolError(f"chromium failed: {e}") from e
