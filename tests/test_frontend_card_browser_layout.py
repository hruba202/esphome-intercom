"""Browser geometry checks for the production call controls."""

from pathlib import Path
import shutil

import pytest


ROOT = Path(__file__).resolve().parents[1]
FRONTEND = ROOT / "custom_components/voip_stack/frontend"
pytestmark = pytest.mark.js_runtime


@pytest.mark.skipif(shutil.which("chromium") is None, reason="Chromium is unavailable")
@pytest.mark.parametrize("width", [280, 360, 600])
@pytest.mark.parametrize("park_enabled", [False, True])
@pytest.mark.parametrize("keypad_open", [False, True])
def test_video_controls_do_not_overlap_timer_or_each_other(
    width, park_enabled, keypad_open, tmp_path
):
    """All actions share the bottom call bar without covering the timer or video."""
    playwright = pytest.importorskip("playwright.sync_api")
    with playwright.sync_playwright() as runner:
        browser = runner.chromium.launch(
            executable_path=shutil.which("chromium"), headless=True
        )
        try:
            page = browser.new_page(viewport={"width": width + 40, "height": 500})

            def serve(route):
                name = route.request.url.split("/")[-1].split("?")[0]
                path = FRONTEND / name
                if name.endswith(".js") and path.is_file():
                    route.fulfill(content_type="text/javascript", body=path.read_text())
                else:
                    route.fulfill(
                        content_type="text/html", body="<html><body></body></html>"
                    )

            page.route("http://card.test/**", serve)
            page.goto("http://card.test/")
            page.evaluate(
                """async ({width, park, keypad}) => {
                document.body.style.cssText='margin:10px;font-family:Arial';
                const host=document.createElement('div');
                host.style.cssText=`width:${width}px;height:360px`;
                document.body.append(host);
                const owner={shadowRoot:host.attachShadow({mode:'open'}),
                    _attachEventHandlers(){}, _observeLayout(){}};
                const {buildMainCardSkeleton}=await import('/voip-stack-card-view.js');
                buildMainCardSkeleton.call(owner,'layout-test');
                const e=owner._els; window.elements=e;
                e.card.classList.add('video-active','keypad-available');
                for(const key of ['callBtn','answerBtn','declineBtn','placeholderBtn']) e[key].hidden=true;
                e.hangupBtn.hidden=false;
                e.hangupPeer.textContent='Office phone with a long display name';
                e.hangupDuration.textContent='01:23:45';
                e.parkBtn.hidden=!park;
                e.parkBtn.querySelector('.settings-label').textContent='Anruf parken';
                e.recordBtn.hidden=true;
                e.voicemailBtn.hidden=true;
                e.keypadPanel.hidden=!keypad;e.keypadInput.hidden=true;
                // HA supplies these icon boxes; the fixture preserves their sizes.
                for(const icon of owner.shadowRoot.querySelectorAll('ha-icon'))
                    icon.style.cssText='display:inline-block;width:24px;height:24px';
                await import('/voip-stack-card.js');
                customElements.get('voip-stack-card').prototype._measureLayout.call(owner);
            }""",
                {"width": width, "park": park_enabled, "keypad": keypad_open},
            )
            boxes = page.evaluate("""() => {
                const e=elements;
                return Object.fromEntries(['card','hangupBtn','hangupDuration','keypadBtn',
                    'settingsBtn','parkBtn','recordBtn','voicemailBtn']
                    .filter(key=>!e[key].hidden)
                    .map(key=>[key,e[key].getBoundingClientRect().toJSON()]));
            }""")
            page.screenshot(path=str(tmp_path / "call-controls.png"))
            card = boxes.pop("card")
            hangup = boxes.pop("hangupBtn")
            assert abs(hangup["bottom"] - card["bottom"]) <= 1
            for name, box in boxes.items():
                assert box["width"] > 0 and box["height"] > 0, name
                assert (
                    card["left"] <= box["left"] < box["right"] <= card["right"] + 1
                ), name
                if name != "hangupDuration":
                    assert box["top"] >= hangup["top"] - 1, name
                    assert box["bottom"] <= hangup["bottom"] + 1, name
            items = list(boxes.items())
            for index, (name, box) in enumerate(items):
                for other_name, other in items[index + 1 :]:
                    overlap = min(box["right"], other["right"]) - max(
                        box["left"], other["left"]
                    )
                    vertical = min(box["bottom"], other["bottom"]) - max(
                        box["top"], other["top"]
                    )
                    assert overlap <= 0 or vertical <= 0, (name, other_name)
        finally:
            browser.close()
