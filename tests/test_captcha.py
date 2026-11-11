"""Капча hh: вводит человек; ссылка за «проверкой VPN» распознаётся."""

from __future__ import annotations

import io

from PIL import Image

from snpr.hh_tool import _nested_captcha_url, to_png

VPN_URL = (
    "https://hh.ru/vpncheeck?backUrl=%2Faccount%2Fcaptcha%3Fstate%3DABC%253D%253D"
    "%26backurl%3Dhh%253A%252F%252Fapplicant%252FcaptchaSuccessBackUrl"
)


def test_vpn_check_page_leads_to_captcha() -> None:
    url = _nested_captcha_url(VPN_URL)
    assert url is not None
    assert url.startswith("https://hh.ru/account/captcha?state=ABC")
    assert _nested_captcha_url("https://hh.ru/account/captcha?state=X") is None


def test_captcha_image_converted_to_png() -> None:
    buf = io.BytesIO()
    Image.new("RGB", (20, 10), "white").save(buf, format="JPEG")
    assert to_png(buf.getvalue()).startswith(b"\x89PNG")
    assert to_png(b"not an image") == b"not an image"
