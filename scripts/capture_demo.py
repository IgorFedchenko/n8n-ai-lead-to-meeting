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
        # Follow the actual invitation email as a recipient, without the synthetic dashboard.
        page.get_by_role("button", name="Run workflow", exact=True).click()
        expect(page.locator("#notice")).to_have_text("Workflow finished. CRM state verified.", timeout=120000)
        run_id = page.evaluate("lastRun.id || lastRun.run_id")
        mail = page.request.get(os.getenv("DEMO_URL", "http://localhost:8088") + "/demo/mock").json()["mail"]
        invitation = next(item for item in mail if item["key"] == "offer:" + run_id)
        private_url = invitation["body"].split("\nBook: ", 1)[1].strip()
        recipient = context.new_page()
        recipient.on("pageerror", lambda error: errors.append(str(error)))
        recipient.goto(private_url)
        expect(recipient.get_by_role("button", name="Book this slot")).to_have_count(3)
        recipient.screenshot(path=str(image_dir / "booking.png"), full_page=True)
        recipient.get_by_role("button", name="Book this slot").first.click()
        expect(recipient.locator("#status")).to_have_text(
            "Meeting confirmed. Your email confirmation has been sent.",
            timeout=120000,
        )
        page.locator("#scenario").select_option("invalid")
        page.get_by_role("button", name="Run workflow", exact=True).click()
        expect(page.locator("#notice")).to_have_text(
            "Queued for manual review. Warning recorded.", timeout=120000
        )
        page.screenshot(path=str(image_dir / "manual.png"), full_page=True)
        assert not errors, errors
        video = page.video
        context.close()
        if video:
            video.save_as(str(ROOT / "artifacts/video/demo.webm"))
        browser.close()
    print("PASS: browser form, replay, booking, private email link and screenshots; no JavaScript errors")


if __name__ == "__main__":
    main()
