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
@pytest.mark.parametrize("height", [280, 360])
def test_video_controls_do_not_overlap_timer_or_each_other(
    width, park_enabled, keypad_open, height, tmp_path
):
    """All actions share the bottom call bar without covering the timer or video."""
    playwright = pytest.importorskip("playwright.sync_api")
    with playwright.sync_playwright() as runner:
        browser = runner.chromium.launch(
            executable_path=shutil.which("chromium"), headless=True
        )
        try:
            page = browser.new_page(
                viewport={"width": width + 40, "height": 500}, has_touch=True
            )

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
                """async ({width, park, keypad, height}) => {
                document.body.style.cssText='margin:10px;font-family:Arial';
                const host=document.createElement('div');
                host.style.cssText=`width:${width}px;height:${height}px`;
                document.body.append(host);
                await import('/voip-stack-card.js');
                const prototype=customElements.get('voip-stack-card').prototype;
                const owner={shadowRoot:host.attachShadow({mode:'open'}),
                    _attachEventHandlers(){
                        this._els.keypadBtn.onclick=()=>{
                            this._els.keypadPanel.hidden=!this._els.keypadPanel.hidden;
                        };
                    },
                    _measureLayout(){prototype._measureLayout.call(this);},
                    _observeLayout(){prototype._observeLayout.call(this);}};
                owner._resizeObserver=new ResizeObserver(()=>owner._measureLayout());
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
                e.keypadPanel.hidden=true;e.keypadInput.hidden=true;
                // HA supplies these icon boxes; the fixture preserves their sizes.
                for(const icon of owner.shadowRoot.querySelectorAll('ha-icon'))
                    icon.style.cssText='display:inline-block;width:24px;height:24px';

            }""",
                {
                    "width": width,
                    "park": park_enabled,
                    "keypad": keypad_open,
                    "height": height,
                },
            )
            page.wait_for_function("elements.card.style.getPropertyValue('--voip-video-actions-space') !== ''")
            if keypad_open:
                page.get_by_role("button", name="Keypad", exact=True).click()
                page.wait_for_function("elements.card.style.getPropertyValue('--voip-video-keypad-height') !== ''")
            boxes = page.evaluate("""() => {
                const e=elements;
                return Object.fromEntries(['card','hangupBtn','hangupDuration','keypadBtn',
                    'settingsBtn','parkBtn','recordBtn','voicemailBtn']
                    .filter(key=>!e[key].hidden)
                    .map(key=>[key,e[key].getBoundingClientRect().toJSON()]));
            }""")
            if keypad_open and height == 280:
                panel = page.evaluate("""() => ({
                    ...elements.keypadPanel.getBoundingClientRect().toJSON(),
                    scrollHeight: elements.keypadPanel.scrollHeight,
                    clientHeight: elements.keypadPanel.clientHeight,
                })""")
                assert panel["bottom"] <= boxes["hangupBtn"]["top"] + 1
                assert panel["scrollHeight"] > panel["clientHeight"]
                await_scroll = "elements.keypadPanel.scrollTop > 0"
                page.mouse.move(
                    panel["left"] + panel["width"] / 2,
                    panel["top"] + panel["height"] / 2,
                )
                page.mouse.wheel(0, 150)
                page.wait_for_function(await_scroll)
                assert page.evaluate("elements.card.scrollTop") == 0
                after = page.evaluate(
                    "elements.hangupBtn.getBoundingClientRect().toJSON()"
                )
                assert abs(after["top"] - boxes["hangupBtn"]["top"]) <= 1
                assert abs(after["bottom"] - boxes["hangupBtn"]["bottom"]) <= 1
                if width == 360 and park_enabled:
                    page.evaluate("elements.keypadPanel.scrollTop = 0")
                    cdp = page.context.new_cdp_session(page)
                    x = panel["left"] + panel["width"] / 2
                    y = panel["bottom"] - 10
                    cdp.send(
                        "Input.dispatchTouchEvent",
                        {"type": "touchStart", "touchPoints": [{"x": x, "y": y}]},
                    )
                    for offset in (20, 40, 60, 80):
                        cdp.send(
                            "Input.dispatchTouchEvent",
                            {
                                "type": "touchMove",
                                "touchPoints": [{"x": x, "y": y - offset}],
                            },
                        )
                        page.wait_for_timeout(16)
                    cdp.send(
                        "Input.dispatchTouchEvent",
                        {"type": "touchEnd", "touchPoints": []},
                    )
                    page.wait_for_function(await_scroll)
                    cdp.detach()

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
