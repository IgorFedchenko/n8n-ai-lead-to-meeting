"""Browser acceptance and screenshots against the running, synthetic-only n8n demo."""

import os
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parents[1]


def main():
    image_dir = ROOT / "docs/images"
    image_dir.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        context = browser.new_context(
            viewport={"width": 1440, "height": 1100}, record_video_dir=str(ROOT / "artifacts/video")
        )
        page = context.new_page()
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto(os.getenv("DEMO_URL", "http://localhost:8088"))
        page.get_by_role("button", name="Run workflow", exact=True).click()
        expect(page.locator("#notice")).to_have_text("Workflow finished. CRM state verified.", timeout=120000)
        before = [page.locator("#" + kind).inner_text() for kind in ("contacts", "deals")]
        page.get_by_role("button", name="Replay", exact=True).click()
        expect(page.locator("#notice")).to_have_text("Workflow finished. CRM state verified.", timeout=120000)
        after = [page.locator("#" + kind).inner_text() for kind in ("contacts", "deals")]
        assert before == after, "UI replay created duplicate CRM records"
        expect(page.locator("#slots button")).to_have_count(3)
        page.screenshot(path=str(image_dir / "demo.png"), full_page=True)
        page.locator("#slots button").first.click()
        expect(page.locator("#notice")).to_have_text(
            "Meeting recorded. Confirmation email delivered to the mock outbox.",
            timeout=120000,
        )
        expect(page.locator("#slots")).to_contain_text("Meeting confirmed:")
        page.screenshot(path=str(image_dir / "booked.png"), full_page=True)
        assert not errors, errors
        context.close()
        browser.close()
    print("PASS: browser form, replay, booking and screenshots; no JavaScript errors")


if __name__ == "__main__":
    main()
