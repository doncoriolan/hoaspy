"""A Chromium browser driven over its DevTools port, for the sources that
answer only a real browser (a Cloudflare Turnstile widget that the page
itself passes, a device check, session-bound result pages).

Nothing is minted, replayed or solved from Python: the browser loads the
site's own page and the collector reads what the page shows or runs the
page's own JavaScript. Start the browser with remote debugging on the
port first (README: Browser-driven collectors); the collectors were written
against BrowserOS, a Chromium fork.

    devtools = DevTools()                       # raises PermissionError when no browser answers
    with devtools.context() as context:         # fresh cookies, disposed on exit
        tab = context.open_tab(url)
        value = tab.evaluate("document.title")
"""

from __future__ import annotations

import json
import time
import urllib.request

import websocket

DEVTOOLS_URL = "http://127.0.0.1:9100"


class DevTools:
    """One connection to the browser's DevTools websocket."""

    def __init__(self, url: str = DEVTOOLS_URL):
        self.url = url
        try:
            version = json.load(urllib.request.urlopen(url + "/json/version", timeout=5))
        except Exception as exc:
            raise PermissionError(f"no browser answers at {url} ({exc}); start the browser with its "
                                  "DevTools port there (README: Browser-driven collectors) and rerun") from exc
        self.socket = websocket.create_connection(version["webSocketDebuggerUrl"], suppress_origin=True)
        self.message_id = 0

    def send(self, method: str, params: dict | None = None, session_id: str | None = None,
             timeout: float = 10) -> dict | None:
        """One DevTools command; its answer, or None when none came in time."""
        self.message_id += 1
        message = {"id": self.message_id, "method": method, "params": params or {}}
        if session_id:
            message["sessionId"] = session_id
        self.socket.send(json.dumps(message))
        return self.wait(self.message_id, timeout)

    def wait(self, message_id: int, timeout: float) -> dict | None:
        """The answer to `message_id`, events on the way being discarded."""
        self.socket.settimeout(timeout)
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                message = json.loads(self.socket.recv())
            except Exception:
                break
            if message.get("id") == message_id:
                return message
        return None

    def drain(self, seconds: float) -> None:
        """Read and discard events for `seconds` — a pause that keeps the
        socket's queue from filling while a page loads."""
        self.wait(-1, seconds)

    def evaluate(self, session_id: str, js: str, timeout: float = 30, await_promise: bool = False):
        """The value of a JavaScript expression in the page; RuntimeError when it threw."""
        answer = self.send("Runtime.evaluate", {"expression": js, "returnByValue": True,
                                                "awaitPromise": await_promise}, session_id=session_id, timeout=timeout)
        result = (answer or {}).get("result", {})
        if "exceptionDetails" in result:
            raise RuntimeError("page JS threw: " + json.dumps(result["exceptionDetails"])[:300])
        return result.get("result", {}).get("value")

    def context(self) -> BrowserContext:
        return BrowserContext(self)

    def close(self) -> None:
        try:
            self.socket.close()
        except Exception:
            pass


class BrowserContext:
    """A throw-away browser context: its own cookies and storage, gone when
    it is disposed. Use it as a context manager."""

    def __init__(self, devtools: DevTools):
        self.devtools = devtools
        self.context_id = devtools.send("Target.createBrowserContext")["result"]["browserContextId"]
        self.tabs: list[Tab] = []

    def open_tab(self, url: str = "about:blank", background: bool = True) -> Tab:
        tab = Tab(self, url, background)
        self.tabs.append(tab)
        return tab

    def dispose(self) -> None:
        for tab in list(self.tabs):
            tab.close()
        try:
            self.devtools.send("Target.disposeBrowserContext", {"browserContextId": self.context_id})
        except Exception:
            pass

    def __enter__(self) -> BrowserContext:
        return self

    def __exit__(self, *exc) -> None:
        self.dispose()


class Tab:
    """One page in a BrowserContext, attached with a DevTools session."""

    def __init__(self, context: BrowserContext, url: str, background: bool):
        self.context = context
        self.devtools = context.devtools
        created = self.devtools.send("Target.createTarget", {
            "url": "about:blank", "browserContextId": context.context_id, "background": background})
        self.target_id = created["result"]["targetId"]
        self.session_id = self.devtools.send("Target.attachToTarget", {
            "targetId": self.target_id, "flatten": True})["result"]["sessionId"]
        self.devtools.send("Page.enable", session_id=self.session_id)
        if url != "about:blank":
            self.navigate(url)

    def navigate(self, url: str) -> None:
        self.devtools.send("Page.navigate", {"url": url}, session_id=self.session_id)

    def evaluate(self, js: str, timeout: float = 30, await_promise: bool = False):
        return self.devtools.evaluate(self.session_id, js, timeout=timeout, await_promise=await_promise)

    def close(self) -> None:
        if self.target_id:
            try:
                self.devtools.send("Target.closeTarget", {"targetId": self.target_id})
            except Exception:
                pass
            self.target_id = None
        if self in self.context.tabs:
            self.context.tabs.remove(self)
