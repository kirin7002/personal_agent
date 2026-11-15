"""Browser checks for hierarchy, query semantics, profile selection and import/export."""
import json
from pathlib import Path
from playwright.sync_api import sync_playwright, expect

ROOT = Path(__file__).resolve().parent.parent
OUTPUT = ROOT / "test-results"
OUTPUT.mkdir(exist_ok=True)


with sync_playwright() as p:
    chrome = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
    browser = p.chromium.launch(executable_path=str(chrome) if chrome.exists() else None, headless=True)
    context = browser.new_context(viewport={"width": 1440, "height": 1050}, accept_downloads=True)
    page = context.new_page()
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.goto("http://127.0.0.1:8765", wait_until="domcontentloaded")
    page.locator("#openCatalog").click()
    expect(page.locator("#catalogSummary")).to_contain_text("车控 50")
    expect(page.locator("#catalog .catalog-tool")).to_have_count(3)
    def folder(title):
        page.get_by_text("▸ "+title, exact=True).locator("..").get_by_role("button", name="浏览下级 →").click()
    def root():
        page.get_by_role("button", name="全部能力", exact=True).click()
    def card(tid):
        return page.get_by_text(tid, exact=True).locator("..")
    folder("车控"); folder("空调")
    expect(page.locator("#catalog .catalog-tool")).to_have_count(10)
    card("control.climate.set_temperature").get_by_role("button", name="查看 Schema").click()
    expect(page.locator("#jsonCode")).to_contain_text('"temperature_c"')
    assert json.loads(page.locator("#jsonCode").inner_text())["input_schema"]["properties"]["temperature_c"]["minimum"] == 16
    page.get_by_role("button", name="关闭调用预览").click()
    card("control.climate.set_temperature").get_by_role("button", name="预置验证").click()
    expect(page.locator("#previewState")).to_have_text("模拟成功")
    assert json.loads(page.locator("#jsonCode").inner_text())["outcome"] == "prepared"
    page.get_by_role("button", name="关闭调用预览").click()
    root(); folder("车书"); folder("悬架")
    card("manual.suspension.lookup").get_by_role("button", name="预置验证").click()
    expect(page.locator("#previewState")).to_have_text("查询完成")
    query = json.loads(page.locator("#jsonCode").inner_text())
    assert query["calls"] == [] and query["results"][0]["data"]["source"]["kind"] == "mock_manual"
    page.get_by_role("button", name="关闭调用预览").click()
    with page.expect_response(lambda r: r.request.method == "POST" and r.url.endswith("/api/session/profile")):
        page.locator("#profileSelect").select_option("demo-no-suspension")
    root(); folder("车控"); folder("悬架")
    card("control.suspension.set_height").get_by_role("button", name="预置验证").click()
    expect(page.locator("#previewState")).to_have_text("不支持")
    assert json.loads(page.locator("#jsonCode").inner_text())["results"][0]["error"]["code"] == "NOT_EQUIPPED"
    page.get_by_role("button", name="关闭调用预览").click()
    with page.expect_download() as downloaded:
        page.locator("#exportCatalog").click()
    path = OUTPUT / "browser-export.bundle.json"
    downloaded.value.save_as(path)
    exported = json.loads(path.read_text())
    assert len(exported["tools"]) == 68
    page.locator("#importFile").set_input_files({"name": "invalid.json", "mimeType": "application/json", "buffer": b'{"format_version":"invalid"}'})
    expect(page.locator("#toast")).to_contain_text("导入未生效")
    page.locator("#importFile").set_input_files(str(path))
    expect(page.locator("#importPreview")).to_be_visible()
    changes = json.loads(page.locator("#importDiff").inner_text())
    assert changes["changes"]["tools"] == {"added": [], "removed": [], "modified": []}
    page.locator("#applyImport").click()
    expect(page.locator("#importPreview")).to_be_hidden()
    expect(page.locator("#catalog .catalog-tool")).to_have_count(3)
    folder("车控"); folder("空调")
    card("control.climate.set_airflow").get_by_role("button", name="查看 Schema").click()
    expect(page.locator("#toast")).to_be_hidden(timeout=7000)
    page.screenshot(path=str(OUTPUT / "catalog-desktop.png"), full_page=True)
    page.set_viewport_size({"width": 390, "height": 844})
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    page.screenshot(path=str(OUTPUT / "catalog-mobile.png"), full_page=True)
    assert not errors, errors
    browser.close()
    print("Catalog UI passed: hierarchy, lazy schema, query/control distinction, profiles, export/import preview/apply, responsive layout.")
