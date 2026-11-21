"""A screenshot of a screen that is on its side (PHO-149).

A landscape app, or a phone that has been turned, gives a landscape
screenshot, and taps are read in that screenshot's orientation. The size the
API reports beside it can still be the portrait one the session started with:
phone-host records the size once, when the session becomes ready. The line
that tells a model how to turn a point in the image into a tap has to follow
the image, or every tap taken from a landscape screenshot misses.
"""
import re

import pytest

from fake_api import BOUNDS, CAPTURE_SIZE
from phone_harness_mcp import screenshots
from support import connected, start, stdio_server, text_of

pytestmark = pytest.mark.anyio

NOTE = re.compile(r"Screenshot is (\d+)x(\d+)\. Screen coordinates are (\d+)x(\d+)\. To tap something you see in "
                  r"this screenshot, multiply its x by ([\d.]+) and y by ([\d.]+)\.")


async def shot(session, sid, **args):
    r = await session.call_tool("phone_take_screenshot", {"device": sid, **args})
    assert not r.is_error, text_of(r)
    m = NOTE.match(r.content[0].text)
    assert m, r.content[0].text
    return [float(g) for g in m.groups()]


async def test_a_portrait_screenshot_is_described_as_before(api):
    async with connected(stdio_server(api)) as s:
        sid = await start(s)
        iw, ih, sw, sh, fx, fy = await shot(s, sid)
    assert (sw, sh) == (BOUNDS["w"], BOUNDS["h"]) and ih > iw
    assert abs(fx - sw / iw) < 0.002 and abs(fy - sh / ih) < 0.002


@pytest.mark.parametrize("stale", [False, True])
async def test_a_landscape_screenshot_maps_onto_a_landscape_screen(api, stale):
    """`stale` is phone-host as it is: the size it reports is still the portrait one."""
    api.stale_bounds = stale
    async with connected(stdio_server(api)) as s:
        sid = await start(s)
        api.turned.add(sid)
        iw, ih, sw, sh, fx, fy = await shot(s, sid)
    assert iw > ih                                              # the image is on its side
    assert (sw, sh) == (BOUNDS["h"], BOUNDS["w"])               # and so are the coordinates it is mapped onto
    assert abs(fx - sw / iw) < 0.002 and abs(fy - sh / ih) < 0.002
    # The bottom right corner of the image is the bottom right corner of the screen,
    # not a point far below a portrait one.
    assert round(iw * fx) == BOUNDS["h"] and round(ih * fy) == BOUNDS["w"]


@pytest.mark.parametrize("stale", [False, True])
async def test_an_unshrunk_landscape_screenshot_is_half_scale_both_ways(api, stale):
    api.stale_bounds = stale
    async with connected(stdio_server(api)) as s:
        sid = await start(s)
        api.turned.add(sid)
        iw, ih, sw, sh, fx, fy = await shot(s, sid, max_size=4000)
    assert (iw, ih) == (CAPTURE_SIZE[1], CAPTURE_SIZE[0]) and (sw, sh) == (BOUNDS["h"], BOUNDS["w"])
    assert (fx, fy) == (0.5, 0.5)


async def test_back_upright_the_mapping_is_upright_again(api):
    api.stale_bounds = True
    async with connected(stdio_server(api)) as s:
        sid = await start(s)
        api.turned.add(sid)
        wide = await shot(s, sid)
        api.turned.discard(sid)
        tall = await shot(s, sid)
    assert wide[2:4] == [BOUNDS["h"], BOUNDS["w"]] and tall[2:4] == [BOUNDS["w"], BOUNDS["h"]]


@pytest.mark.parametrize("image,bounds,screen", [
    ((780, 1688), {"w": 390, "h": 844}, (390, 844)),            # both upright: the bounds
    ((1688, 780), {"w": 844, "h": 390}, (844, 390)),            # both on their side: the bounds
    ((1688, 780), {"w": 390, "h": 844}, (844, 390)),            # the image turned and the bounds did not
    ((780, 1688), {"w": 844, "h": 390}, (390, 844)),            # the session started on its side and is upright now
    ((1334, 750), {"w": 750, "h": 1334}, (1334, 750)),          # an iPhone SE, whose bounds are its pixels
    ((1000, 1000), {"w": 390, "h": 844}, (390, 844)),           # a square image says nothing about the way up
    ((1688, 780), {"w": 500, "h": 500}, (500, 500)),
    ((1688, 780), {"x": 0, "y": 0, "w": 390.0, "h": 844.4}, (844, 390)),
])
def test_the_screen_is_the_bounds_turned_the_way_the_image_is(image, bounds, screen):
    assert screenshots.screen_size(image, bounds) == screen


@pytest.mark.parametrize("bounds", [None, {}, {"w": 0, "h": 0}, {"w": 390}, {"w": "390", "h": "844"},
                                    {"w": True, "h": 844}, {"w": -390, "h": 844}])
def test_with_no_usable_bounds_the_image_is_the_screen(bounds):
    assert screenshots.screen_size((1688, 780), bounds) == (1688, 780)
