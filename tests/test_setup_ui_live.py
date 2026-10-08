"""Read-only browser checks against the running, configured Chatbot Admin Console.

CHAT_SETUP_URL=http://127.0.0.1:18996 CHAT_SETUP_PASSWORD_FILE=/path/to/admin-password \
    uv run python -m pytest -xq tests/test_setup_ui_live.py
"""

import os
from pathlib import Path

from playwright.sync_api import expect, sync_playwright


def test_saved_provider_options_are_visible_in_editor():
    url = os.environ["CHAT_SETUP_URL"]
    password = Path(os.environ["CHAT_SETUP_PASSWORD_FILE"]).read_text().strip()
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.goto(url)
        page.locator("#password").fill(password)
        page.get_by_role("button", name="Open Chatbot Admin Console").click()
        expect(page.locator("#console")).to_be_visible()
        response = page.request.get(url + "/api/state")
        assert response.ok
        state = response.json()
        connection = next(c for c in state["settings"]["connections"] if c["platform"] == "irc")
        expected = {**connection["options"], **connection["capture"]["options"]}
        assert expected.get("server") and expected.get("port"), "Use a configured IRC connection"
        page.locator('.nav[data-tab="capture"]').click()
        panel = page.locator("#capture-connections article").filter(
            has=page.get_by_role("heading", name="#_ " + connection["name"], exact=True)
        )
        panel.get_by_role("button", name="Configure", exact=True).click()
        expect(page.locator('#editor input[name="server"]')).to_have_value(expected["server"])
        expect(page.locator('#editor input[name="port"]')).to_have_value(str(expected["port"]))
        expect(page.locator('#editor input[name="channels"]')).to_have_value(", ".join(expected["channels"]))
        assert page.locator('#editor input[name="tls"]').is_checked() is bool(expected.get("tls"))
        page.locator("#close-editor").click()
        browser.close()


def test_provider_guides_load_real_images_and_change_with_transport():
    url = os.environ["CHAT_SETUP_URL"]
    password = Path(os.environ["CHAT_SETUP_PASSWORD_FILE"]).read_text().strip()
    errors = []
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1440, "height": 1100})
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto(url)
        page.locator("#password").fill(password)
        page.get_by_role("button", name="Open Chatbot Admin Console").click()
        expect(page.locator("#console")).to_be_visible()
        page.locator('.nav[data-tab="connections"]').click()
        for provider in (
            "Slack",
            "Discord",
            "Email",
            "Zulip",
            "Telegram",
            "WhatsApp",
            "IRC",
            "iMessage",
            "Messenger",
            "Beeper",
        ):
            page.locator(".provider-card").filter(has=page.get_by_text(provider, exact=True)).click()
            guide = page.locator("#setup-guide")
            expect(guide).to_have_attribute("open", "")
            expect(guide.locator(".guide-steps > li")).to_have_count(3)
            assert guide.locator(".guide-docs a").count() >= 3
            images = guide.locator("img")
            if provider in {"Email", "WhatsApp", "Beeper"}:
                expect(images).to_have_count(0)
            else:
                assert images.count() >= 1, provider
            for image in images.all():
                image.scroll_into_view_if_needed()
                image.evaluate("image => image.decode()")
                assert image.evaluate("image => image.naturalWidth > 0"), provider
            if provider == "Email":
                page.locator('#editor input[name="username"]').fill("archive@gmail.com")
                page.locator('#editor input[name="username"]').press("Tab")
                expect(page.locator('#editor input[name="host"]')).to_have_value("imap.gmail.com")
                page.get_by_text("Advanced inbox settings", exact=True).click()
                expect(page.locator('#editor input[name="port"]')).to_have_value("993")
                expect(page.locator('#editor input[name="poll_seconds"]')).to_have_value("30")
                expect(page.locator('#editor input[name="max_message_mb"]')).to_have_value("25")
                expect(page.locator('#editor input[name="include_existing"]')).not_to_be_checked()
                page.locator('#editor select[name="tls_mode"]').select_option("starttls")
                expect(page.locator('#editor input[name="port"]')).to_have_value("143")
                expect(page.locator('#editor input[name="bot_token"]')).to_have_count(0)
            if provider == "WhatsApp":
                expect(guide).to_contain_text("Chat info → API key")
                assert guide.locator('a[href*="faq.whatsapp.com"]').count()
                page.locator('#editor select[name="transport"]').select_option("baileys")
                expect(guide).to_contain_text("Linked Devices")
                expect(guide).not_to_contain_text("Copy the agent's API key")
                assert guide.locator('a[href*="youtube.com"]').count() == 1
            if provider == "Messenger":
                callback = guide.locator(".guide-callback").inner_text()
                assert callback.endswith("/capture/webhook") and "/connections/" in callback
                page.locator('#editor select[name="transport"]').select_option("matrix")
                expect(guide).to_contain_text("Matrix account details")
                expect(guide.locator(".guide-callback")).to_have_count(0)
            page.locator("#close-editor").click()
        page.set_viewport_size({"width": 390, "height": 844})
        page.locator(".provider-card").filter(has=page.get_by_text("WhatsApp", exact=True)).click()
        assert page.locator("#editor").evaluate("e => e.scrollWidth <= e.clientWidth"), "Mobile dialog overflows"
        assert page.locator('#connection-form button[type="submit"]').evaluate(
            "e => { const r = e.getBoundingClientRect(); return r.top >= 0 && r.bottom <= innerHeight; }"
        ), "Save button is clipped"
        assert not errors, errors
        browser.close()
