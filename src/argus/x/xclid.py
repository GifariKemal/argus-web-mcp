"""``x-client-transaction-id`` generator for X web GraphQL calls.

Vendored from twscrape ``twscrape/xclid.py`` (commit 63f61a5, 2026-10-22), which ports
iSarabjitDhiman/XClientTransaction. Changes here: no HTTP client of its own (the caller
fetches pages through Argus's SSRF-guarded client and passes text in), no migrate-form POST,
key derivation split from page loading so it can be tested on fixed inputs.

MIT License. Copyright (c) 2023 vladkens (twscrape). Copyright (c) Sarabjit Dhiman
(XClientTransaction). Permission is hereby granted, free of charge, to any person obtaining
a copy of this software and associated documentation files (the "Software"), to deal in the
Software without restriction, including without limitation the rights to use, copy, modify,
merge, publish, distribute, sublicense, and/or sell copies of the Software, and to permit
persons to whom the Software is furnished to do so, subject to the following conditions:
The above copyright notice and this permission notice shall be included in all copies or
substantial portions of the Software. THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF
ANY KIND, EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF
MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN
AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE
SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.
"""

from __future__ import annotations

import base64
import hashlib
import math
import random
import re
import time

import bs4


class XClIdError(Exception): ...


def script_url(k: str, v: str) -> str:
    return f"https://abs.twimg.com/responsive-web/client-web/{k}.{v}.js"


# Current X web build (Vite): bundles are linked directly in the page HTML under
# https://abs.twimg.com/x-web/.../*.js; the legacy webpack build embeds chunk maps.
ASSET_URL_RE = re.compile(r"https://[\w.-]+/x-web/[\w./-]+\.js")
RESPONSIVE_WEB_URL_RE = re.compile(r"https://[\w.-]+/responsive-web/client-web/[\w./-]+\.js")
LEGACY_MAIN_RE = re.compile(r"/client-web/main\.([^.\"']+)\.js")
LOGGED_OUT_ENTRY_RE = re.compile(r"(?:^|/)entry-client-logged-out(?:[-.][^/?#]+)?\.js(?:[?#].*)?$")
CHALLENGE_SCRIPT_RE = re.compile(r"/cdn-cgi/challenge-platform/(?:scripts|h)/")
# The file holding the animation indices: legacy `ondemand.s.*.js`, current `sign.o-*.js`
# (dynamically imported from a bundle chunk). \b guards against `design.o-*.js`.
INDICES_FILE_RE = re.compile(r"(?:\.{0,2}/)?[\w./-]*?\b(?:ondemand\.s|sign\.o)[\w.-]*\.js")
INDICES_REGEX = re.compile(r"(\(\w{1}\[(\d{1,2})\],\s*16\))+", flags=(re.VERBOSE | re.MULTILINE))


def get_scripts_list(text: str) -> list[str]:
    """Every script URL the X page links or encodes, x-web bundles first. Raises XClIdError
    on a Cloudflare interstitial or a logged-out page (cookies not accepted)."""
    urls = ASSET_URL_RE.findall(text) + RESPONSIVE_WEB_URL_RE.findall(text)
    if main_match := LEGACY_MAIN_RE.search(text):
        urls.append(script_url("main", main_match.group(1)))
    if not urls and CHALLENGE_SCRIPT_RE.search(text):
        raise XClIdError("Cloudflare challenge page served instead of X web app")
    # Hash map values are 7 or 16 lowercase hex digits; name-map values are anything else.
    hash_map = {
        m.group(1): m.group(2) for m in re.finditer(r'(\d+):"([0-9a-f]{7}|[0-9a-f]{16})"', text)
    }
    name_map = {m.group(1): m.group(2) for m in re.finditer(r'(\d+):"([^"]+)"', text)
                if not re.fullmatch(r"[0-9a-f]{7}|[0-9a-f]{16}", m.group(2))}
    urls.extend(script_url(name_map.get(cid, cid), h + "a") for cid, h in hash_map.items())
    if not urls:
        raise XClIdError("X web scripts not found")
    urls = list(dict.fromkeys(urls))
    x_web = [u for u in urls if "/x-web/" in u]
    if any(LOGGED_OUT_ENTRY_RE.search(u) for u in x_web):
        raise XClIdError("logged-out X web app (cookies not accepted)")
    return x_web or urls


class Cubic:
    def __init__(self, curves: list[float]):
        self.curves = curves

    def get_value(self, time: float) -> float:
        start_gradient = end_gradient = start = mid = 0.0
        end = 1.0
        if time <= 0.0:
            if self.curves[0] > 0.0:
                start_gradient = self.curves[1] / self.curves[0]
            elif self.curves[1] == 0.0 and self.curves[2] > 0.0:
                start_gradient = self.curves[3] / self.curves[2]
            return start_gradient * time
        if time >= 1.0:
            if self.curves[2] < 1.0:
                end_gradient = (self.curves[3] - 1.0) / (self.curves[2] - 1.0)
            elif self.curves[2] == 1.0 and self.curves[0] < 1.0:
                end_gradient = (self.curves[1] - 1.0) / (self.curves[0] - 1.0)
            return 1.0 + end_gradient * (time - 1.0)
        while start < end:
            mid = (start + end) / 2
            x_est = self.calculate(self.curves[0], self.curves[2], mid)
            if abs(time - x_est) < 0.00001:
                return self.calculate(self.curves[1], self.curves[3], mid)
            if x_est < time:
                start = mid
            else:
                end = mid
        return self.calculate(self.curves[1], self.curves[3], mid)

    @staticmethod
    def calculate(a: float, b: float, m: float) -> float:
        return 3.0 * a * (1 - m) * (1 - m) * m + 3.0 * b * (1 - m) * m * m + m * m * m


def interpolate(from_list: list[float], to_list: list[float], f: float) -> list[float]:
    if len(from_list) != len(to_list):
        raise XClIdError("mismatched interpolation args")
    return [a * (1 - f) + b * f for a, b in zip(from_list, to_list, strict=True)]


def get_rotation_matrix(rotation: float) -> list[float]:
    rad = math.radians(rotation)
    return [math.cos(rad), -math.sin(rad), math.sin(rad), math.cos(rad)]


def solve(value: float, min_val: float, max_val: float, rounding: bool) -> float:
    result = value * (max_val - min_val) / 255 + min_val
    return math.floor(result) if rounding else round(result, 2)


def float_to_hex(x: float) -> str:
    result: list[str] = []
    quotient = int(x)
    fraction = x - quotient
    while quotient > 0:
        quotient = int(x / 16)
        remainder = int(x - (float(quotient) * 16))
        result.insert(0, chr(remainder + 55) if remainder > 9 else str(remainder))
        x = float(quotient)
    if fraction == 0:
        return "".join(result)
    result.append(".")
    while fraction > 0:
        fraction *= 16
        integer = int(fraction)
        fraction -= float(integer)
        result.append(chr(integer + 55) if integer > 9 else str(integer))
    return "".join(result)


def calc_anim_key(frames: list[float], target_time: float) -> str:
    from_color = [*frames[:3], 1]
    to_color = [*frames[3:6], 1]
    from_rotation = [0.0]
    to_rotation = [solve(frames[6], 60.0, 360.0, True)]
    frames = frames[7:]
    curves = [solve(x, -1.0 if i % 2 else 0.0, 1.0, False) for i, x in enumerate(frames)]
    val = Cubic(curves).get_value(target_time)
    color = [max(0, min(255, v)) for v in interpolate(from_color, to_color, val)]
    rotation = interpolate(from_rotation, to_rotation, val)
    str_arr = [format(round(v), "x") for v in color[:-1]]
    for value in get_rotation_matrix(rotation[0]):
        rounded = abs(round(value, 2))
        hex_value = float_to_hex(rounded)
        str_arr.append(f"0{hex_value}".lower() if hex_value.startswith(".")
                       else hex_value if hex_value else "0")
    str_arr.extend(["0", "0"])
    return re.sub(r"[.-]", "", "".join(str_arr))


def parse_vk_bytes(soup: bs4.BeautifulSoup) -> list[int]:
    el = soup.find("meta", {"name": "twitter-site-verification", "content": True})
    content = str(el.get("content")) if isinstance(el, bs4.Tag) else None
    if not content:
        raise XClIdError("X verification key not found")
    try:
        return list(base64.b64decode(bytes(content, "utf-8"), validate=True))
    except ValueError as e:
        raise XClIdError("invalid X verification key") from e


def parse_anim_idx(indices_js: str) -> list[int]:
    items = [int(m.group(2)) for m in INDICES_REGEX.finditer(indices_js)]
    if not items:
        raise XClIdError("signing indices not found")
    return items


def parse_anim_arr(soup: bs4.BeautifulSoup, vk_bytes: list[int]) -> list[list[float]]:
    els = [str(x.get("d") or "").strip() for x in
           soup.select("svg[id^='loading-x-anim'] g:first-child path:nth-child(2)")]
    if not els:
        raise XClIdError("animation data not found")
    dat = els[vk_bytes[5] % len(els)][9:].split("C")
    try:
        return [list(map(float, re.sub(r"[^\d]+", " ", x).split())) for x in dat]
    except (IndexError, ValueError) as e:
        raise XClIdError("invalid animation data") from e


class XClIdGen:
    """Built once per page load (``from_page``), then ``calc`` per request."""

    def __init__(self, vk_bytes: list[int], anim_key: str):
        self.vk_bytes = vk_bytes
        self.anim_key = anim_key

    @classmethod
    def from_page(cls, page_html: str, indices_js: str) -> XClIdGen:
        """Keys from an authenticated x.com page and its signing script. Raises XClIdError
        (also for unexpected shapes: X changed the page, not a bug to surface raw)."""
        try:
            soup = bs4.BeautifulSoup(page_html, "html.parser")
            anim_idx = parse_anim_idx(indices_js)
            vk_bytes = parse_vk_bytes(soup)
            anim_arr = parse_anim_arr(soup, vk_bytes)
            frame_time = 1
            for x in anim_idx[1:]:
                frame_time *= vk_bytes[x] % 16
            frame_time = math.floor(frame_time / 10 + 0.5) * 10  # JS Math.round to nearest 10
            frame_row = anim_arr[vk_bytes[anim_idx[0]] % 16]
            return cls(vk_bytes, calc_anim_key(frame_row, float(frame_time) / 4096))
        except XClIdError:
            raise
        except (IndexError, ValueError, TypeError, ZeroDivisionError) as e:
            raise XClIdError(f"unexpected page shape: {type(e).__name__}") from e

    def calc(self, method: str, path: str, *, now: float | None = None,
             salt: int | None = None) -> str:
        ts = math.floor(((now if now is not None else time.time()) * 1000
                         - 1682924400 * 1000) / 1000)
        ts_bytes = [(ts >> (i * 8)) & 0xFF for i in range(4)]
        dkw, drn = "obfiowerehiring", 3  # default keyword and random number
        digest = hashlib.sha256(f"{method.upper()}!{path}!{ts}{dkw}{self.anim_key}".encode())
        pld = [*self.vk_bytes, *ts_bytes, *list(digest.digest())[:16], drn]
        num = random.randint(0, 255) if salt is None else salt  # noqa: S311 - XOR mask, not a secret
        return base64.b64encode(bytearray([num, *[x ^ num for x in pld]])).decode().strip("=")
