"""Browser-to-worker integration against a separate service and scripted Ollama."""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from playwright.sync_api import sync_playwright


@pytest.fixture
def standalone(tmp_path):
    entered = threading.Event()
    release = threading.Event()
    calls = []

    class Ollama(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def reply(self, payload):
            body = json.dumps(payload).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            self.reply({"models": [{"name": "qwen2.5-coder:3b-instruct"}]})

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
            if self.path == "/api/show":
                self.reply({"capabilities": ["completion", "tools"], "model_info": {"qwen2.context_length": 8192}})
                return
            if self.path != "/api/chat":
                self.reply({})
                return
            calls.append(body)
            if len(calls) == 1:
                entered.set()
                assert release.wait(15)
                message = {"role": "assistant", "content": "", "tool_calls": [{"function": {
                    "name": "write_file", "arguments": {"filepath": "verified.txt", "content": "The standalone worker finished.\n"},
                }}]}
            else:
                message = {"role": "assistant", "content": "Created verified.txt in this project. The task is complete."}
            self.reply({"model": "qwen2.5-coder:3b-instruct", "message": message, "done": True,
                        "done_reason": "stop", "prompt_eval_count": 200, "eval_count": 30})

    model_server = ThreadingHTTPServer(("127.0.0.1", 0), Ollama)
    threading.Thread(target=model_server.serve_forever, daemon=True).start()
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    projects = tmp_path / "projects"
    projects.mkdir()
    env = {**os.environ, "SHAMSU_HOME": str(tmp_path / "home"), "SHAMSU_WORKSPACE_ROOT": str(projects),
           "SHAMSU_OLLAMA_BASE_URL": f"http://127.0.0.1:{model_server.server_port}",
           "SHAMSU_MODEL": "qwen2.5-coder:3b-instruct", "PYTHONUTF8": "1"}
    env.pop("SHAMSU_LEGACY_ROUTING", None)
    log = (tmp_path / "service.log").open("w", encoding="utf-8")
    process = subprocess.Popen(
        [sys.executable, "-m", "shamsu.webui", "--port", str(port)], env=env,
        stdout=log, stderr=subprocess.STDOUT,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    url = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        try:
            urllib.request.urlopen(url + "/healthz", timeout=.5).close()
            break
        except OSError:
            if process.poll() is not None:
                pytest.fail((tmp_path / "service.log").read_text(encoding="utf-8"))
            time.sleep(.1)
    else:
        pytest.fail("Standalone service did not become ready")
    try:
        yield url, projects, entered, release, tmp_path
    finally:
        release.set()
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=3)
        log.close()
        model_server.shutdown()
        model_server.server_close()


def test_browser_task_finishes_after_tab_closes_and_restores_history(standalone):
    url, projects, entered, release, artifacts = standalone
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1280, "height": 800})
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto(url)
        page.get_by_role("button", name="Add a project", exact=True).click()
        page.get_by_label("Folder path", exact=True).fill("browser-project")
        page.get_by_label("Create this folder if it does not exist").check()
        page.get_by_role("button", name="Add project", exact=True).click()
        page.get_by_role("button", name="New conversation", exact=True).first.click()
        page.get_by_label("Task", exact=True).fill("Create verified.txt containing a note that the standalone worker finished.")
        page.get_by_role("button", name="Send", exact=True).click()
        assert entered.wait(10), page.locator("body").inner_text()
        location = page.evaluate("location.href")
        assert "session=" in location
        page.close()
        release.set()
        target = projects / "browser-project" / "verified.txt"
        deadline = time.monotonic() + 20
        while not target.exists() and time.monotonic() < deadline:
            time.sleep(.1)
        assert target.read_text() == "The standalone worker finished.\n"
        page = browser.new_page(viewport={"width": 1280, "height": 800})
        page.goto(location)
        try:
            page.get_by_text("The task is complete.", exact=False).wait_for(timeout=15000)
        except Exception:
            pytest.fail(f"{page.url}\n{page.locator('body').inner_text()}")
        assert page.locator(".msg.user").count() == 1
        assert page.locator(".msg.assistant").count() == 1
        assert page.locator("#composer").is_visible()
        assert not errors
        browser.close()


def test_ui_ignores_stale_session_responses_and_composing_enter(standalone):
    url, _, _, release, _ = standalone
    release.set()
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(url)
        page.wait_for_function("typeof openSession === 'function'")
        result = page.evaluate("""async () => {
          subscribe = () => {}; refreshQueue = () => {};
          let finishA;
          api = (path) => path.includes('/A/') ? new Promise(resolve => finishA = resolve) : Promise.resolve({messages:[{role:'assistant',content:'Answer B'}]});
          const workspace={id:'test-project',name:'Project',path:'/project'};
          const first=openSession({session_id:'A',title:'Thread A'},workspace);
          await openSession({session_id:'B',title:'Thread B'},workspace);
          finishA({messages:[{role:'assistant',content:'Answer A'}]}); await first;
          let submits=0; el('composer').requestSubmit=()=>submits++;
          onComposerKey({key:'Enter',isComposing:true,preventDefault(){}});
          const card=approvalCard({approval_id:'preview',description:'Change a file',preview:'Review this exact diff'});
          return {title:el('thread-title').textContent,body:el('conversation').textContent,submits,preview:card.textContent};
        }""")
        assert result["title"] == "Thread B"
        assert "Answer B" in result["body"] and "Answer A" not in result["body"]
        assert result["submits"] == 0
        assert "Review this exact diff" in result["preview"]
        browser.close()
