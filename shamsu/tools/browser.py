"""Playwright-backed browser automation for local preview and debugging."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import httpx

from shamsu.safety.approval import ask_approval
from shamsu.safety.approval_manager import ApprovalManager
from shamsu.safety.sandbox import Sandbox
from shamsu.session.manager import SessionLogger
from shamsu.types import ApprovalRequest

COMMON_LOCAL_URLS = (
    "http://127.0.0.1:8000",
    "http://127.0.0.1:3000",
    "http://127.0.0.1:5173",
    "http://127.0.0.1:8080",
    "http://127.0.0.1:4173",
    "http://localhost:8000",
    "http://localhost:3000",
    "http://localhost:5173",
    "http://localhost:8080",
    "http://localhost:4173",
)


@dataclass(frozen=True)
class BrowserActionResult:
    ok: bool
    message: str = ""
    url: str = ""
    title: str = ""
    visible_text: str = ""
    screenshot_path: str = ""
    console_errors: tuple[str, ...] = ()


@dataclass(frozen=True)
class BrowserCapabilityStatus:
    available: bool
    state: str
    message: str
    executable_path: str = ""


class BrowserTool:
    def __init__(
        self,
        workspace_root: Path,
        approval_func: Callable[[ApprovalRequest], bool] = ask_approval,
        session_logger: SessionLogger | None = None,
        approval_manager: ApprovalManager | None = None,
        action_ledger: Any | None = None,
    ) -> None:
        self.workspace_root = Path(workspace_root).resolve()
        self.sandbox = Sandbox(self.workspace_root)
        self.approval_func = approval_func
        self.approval_manager = approval_manager or ApprovalManager(approval_func, session_logger)
        self.session_logger = session_logger
        self.action_ledger = action_ledger
        self._playwright_ctx = None
        self._browser = None
        self._context = None
        self._page = None
        self._console_errors: list[str] = []

    def status(self) -> BrowserCapabilityStatus:
        try:
            playwright = self._load_playwright()
        except RuntimeError as exc:
            return BrowserCapabilityStatus(False, "missing_dependency", str(exc))
        context = None
        browser = None
        try:
            context = playwright().start()
            browser = context.chromium.launch(headless=True)
            browser.close()
            browser = None
            return BrowserCapabilityStatus(True, "ready", "Playwright Chromium is available.")
        except Exception as exc:
            message = str(exc)
            if "Executable doesn't exist" in message or "playwright install" in message:
                return BrowserCapabilityStatus(
                    False,
                    "missing_browser",
                    "Playwright is installed, but Chromium is missing. Run `python -m playwright install chromium`.",
                )
            return BrowserCapabilityStatus(False, "failed", message)
        finally:
            if browser is not None:
                self._close_quietly(browser)
            if context is not None:
                self._stop_playwright_quietly(context)

    def open(self, url: str, reason: str = "", require_approval: bool = True) -> BrowserActionResult:
        if require_approval and not self._approve(
            "Start a local browser session and open a page.",
            "medium",
            url,
            reason or "SHAMSU wants to inspect a page for preview or debugging.",
        ):
            return BrowserActionResult(ok=False, message="Browser access denied by user.")
        try:
            self._ensure_page()
            self._console_errors.clear()
            self._page.goto(url, wait_until="domcontentloaded")
            title = self._page.title()
            text = self._page.locator("body").inner_text(timeout=3000)[:8000]
            self._log(
                "browser.opened",
                {"url": self._page.url, "title": title, "console_errors": list(self._console_errors)},
                f"Opened browser page: {url}",
            )
            return BrowserActionResult(
                ok=True,
                url=self._page.url,
                title=title,
                visible_text=text,
                console_errors=tuple(self._console_errors),
            )
        except Exception as exc:
            message = str(exc)
            self._log("browser.failed", {"url": url, "error": message}, f"Browser failed to open: {url}")
            return BrowserActionResult(ok=False, message=message)

    def read(self) -> BrowserActionResult:
        if self._page is None:
            return BrowserActionResult(ok=False, message="No browser page is open yet.")
        try:
            title = self._page.title()
            text = self._page.locator("body").inner_text(timeout=3000)[:8000]
            self._log("browser.read", {"url": self._page.url, "title": title}, "Read browser page")
            return BrowserActionResult(
                ok=True,
                url=self._page.url,
                title=title,
                visible_text=text,
                console_errors=tuple(self._console_errors),
            )
        except Exception as exc:
            message = str(exc)
            self._log("browser.failed", {"url": getattr(self._page, "url", ""), "error": message}, "Browser read failed")
            return BrowserActionResult(ok=False, message=message)

    def click(self, selector: str) -> BrowserActionResult:
        if self._page is None:
            return BrowserActionResult(ok=False, message="No browser page is open yet.")
        if not self._approve(
            "Click an element in the browser.",
            "medium",
            selector,
            "Clicking may change page state or submit data.",
        ):
            return BrowserActionResult(ok=False, message="Browser click denied by user.")
        try:
            self._page.locator(selector).first.click(timeout=5000)
            self._log("browser.clicked", {"url": self._page.url, "selector": selector}, f"Clicked {selector}")
            return self.read()
        except Exception as exc:
            message = str(exc)
            self._log("browser.failed", {"selector": selector, "error": message}, f"Browser click failed: {selector}")
            return BrowserActionResult(ok=False, message=message)

    def type_text(self, selector: str, text: str) -> BrowserActionResult:
        if self._page is None:
            return BrowserActionResult(ok=False, message="No browser page is open yet.")
        if not self._approve(
            "Type text into a browser field.",
            "medium",
            f"{selector}\n{text}",
            "Typing may change page state or submit sensitive content.",
        ):
            return BrowserActionResult(ok=False, message="Browser typing denied by user.")
        try:
            self._page.locator(selector).first.fill(text, timeout=5000)
            self._log("browser.typed", {"url": self._page.url, "selector": selector}, f"Typed into {selector}")
            return self.read()
        except Exception as exc:
            message = str(exc)
            self._log("browser.failed", {"selector": selector, "error": message}, f"Browser typing failed: {selector}")
            return BrowserActionResult(ok=False, message=message)

    def screenshot(self) -> BrowserActionResult:
        if self._page is None:
            return BrowserActionResult(ok=False, message="No browser page is open yet.")
        output_dir = self.sandbox.validate(Path(".shamsu") / "cache" / "browser")
        output_dir.mkdir(parents=True, exist_ok=True)
        filename = f"{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}.png"
        path = self.sandbox.validate(output_dir / filename)
        try:
            self._page.screenshot(path=str(path), full_page=True)
            self._log("browser.screenshot", {"url": self._page.url, "path": str(path)}, "Captured browser screenshot")
            return BrowserActionResult(
                ok=True,
                url=self._page.url,
                screenshot_path=str(path),
                message="Screenshot saved.",
                console_errors=tuple(self._console_errors),
            )
        except Exception as exc:
            message = str(exc)
            self._log("browser.failed", {"error": message}, "Browser screenshot failed")
            return BrowserActionResult(ok=False, message=message)

    def discover_local_url(self) -> str:
        for url in COMMON_LOCAL_URLS:
            try:
                response = httpx.get(url, timeout=1.5)
                if response.status_code < 500:
                    return url
            except Exception:
                continue
        return ""

    def close(self) -> None:
        page, self._page = self._page, None
        context, self._context = self._context, None
        browser, self._browser = self._browser, None
        playwright_ctx, self._playwright_ctx = self._playwright_ctx, None
        # Close the browser context first. That is the owner Playwright uses to
        # drain page/event work; closing only the page can leave TargetClosedError
        # futures behind when the process exits from inside the TUI.
        if context is not None:
            self._close_quietly(context)
        else:
            self._close_quietly(page)
        self._close_quietly(browser)
        self._stop_playwright_quietly(playwright_ctx)

    def _close_quietly(self, target: Any) -> None:
        if target is None:
            return
        try:
            target.close()
        except Exception:
            return

    def _stop_playwright_quietly(self, playwright_ctx: Any) -> None:
        if playwright_ctx is None:
            return
        try:
            playwright_ctx.stop()
        except Exception:
            return

    def _ensure_page(self) -> None:
        if self._page is not None:
            return
        playwright = self._load_playwright()
        self._playwright_ctx = playwright().start()
        self._browser = self._playwright_ctx.chromium.launch(headless=True)
        self._context = self._browser.new_context()
        self._page = self._context.new_page()
        self._page.on(
            "console",
            lambda message: self._console_errors.append(message.text)
            if message.type == "error"
            else None,
        )
        self._page.on("pageerror", lambda error: self._console_errors.append(str(error)))

    def _load_playwright(self) -> Any:
        try:
            from playwright.sync_api import sync_playwright
        except Exception as exc:  # pragma: no cover - exercised through result handling
            raise RuntimeError(
                "Playwright is not available. Reinstall SHAMSU so it can install browser support."
            ) from exc
        return sync_playwright

    def _approve(self, description: str, risk_level: str, preview: str, reason: str) -> bool:
        request = ApprovalRequest(
            action_type="mcp_tool",
            description=description,
            risk_level=risk_level,  # type: ignore[arg-type]
            preview=preview,
            reason=reason,
        )
        self._log("browser.approval.requested", {"preview": preview, "reason": reason}, description)
        self.approval_manager.session_logger = self.session_logger
        approved = self.approval_manager.ask(request)
        self._log("browser.approval.result", {"preview": preview, "approved": approved}, f"Browser approval result: {approved}")
        return approved

    def _log(self, event_type: str, payload: dict, summary: str) -> None:
        if self.session_logger:
            self.session_logger.log(event_type, payload, summary, workflow_id="browser")
        if self.action_ledger:
            try:
                self.action_ledger.log_event(event_type.replace(".", "_"), **payload)
            except Exception:
                pass
