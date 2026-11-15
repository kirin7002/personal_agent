"""One opt-in live-model UI check. No credentials or hardware operations."""
import json
from pathlib import Path
from playwright.sync_api import sync_playwright, expect

ROOT = Path(__file__).resolve().parent.parent


with sync_playwright() as p:
    chrome = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
    browser = p.chromium.launch(executable_path=str(chrome) if chrome.exists() else None, headless=True)
    page = browser.new_page(viewport={"width": 1440, "height": 1050})
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.goto("http://127.0.0.1:8765", wait_until="domcontentloaded")
    expect(page.locator("#hermesStatus")).to_have_text("API 已连接", timeout=20000)
    page.locator("#message").fill("把所有分区空调调到23度")
    page.locator("#sendButton").click()
    expect(page.locator(".task").first.locator(".task-status")).to_have_text("模拟成功", timeout=150000)
    output = json.loads(page.locator("#jsonCode").inner_text())
    assert [{"tool": c["tool"], "arguments": c["arguments"]} for c in output["calls"]] == [{"tool": "control.climate.set_temperature", "arguments": {"zone": "all", "temperature_c": 23}}]
    assert output["executed"] is False
    assert "预置" not in page.locator("#previewSource").inner_text()
    assert not errors, errors
    (ROOT / "test-results").mkdir(exist_ok=True)
    page.screenshot(path=str(ROOT / "test-results/live-desktop.png"), full_page=True)
    browser.close()
    print("Live UI passed: Hermes API connected, DeepSeek generated a validated climate call, no vehicle execution.")
