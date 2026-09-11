# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: provides function to export SVG animations as WebM videos using Playwright
# index: imports | def write_webm
# AGENT_HEADER_END -->
"""WebM export via playwright record_video. SMIL animation runs natively."""
from __future__ import annotations
import shutil
import time
from pathlib import Path

from .scene import Scene
from .render import scene_to_svg


def write_webm(scene: Scene, out_path: str | Path,
               duration_s: float = 6.0,
               size: tuple[int, int] | None = None) -> Path:
    """Record N seconds of the animated SVG as a WebM file.

    Chromium's record_video_dir produces .webm directly. The animation
    is the SMIL animation embedded in the SVG -- nothing extra needed.
    """
    from playwright.sync_api import sync_playwright

    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    w, h = size or (scene.canvas.width, scene.canvas.height)

    # Enable per-primitive timeline animations (marey, sankey) for the webm
    # path. PNG output leaves this False so SVGs aren't frozen at frame 0.
    try:
        scene.animate = True
    except Exception:
        pass
    svg = scene_to_svg(scene)
    html = (
        '<!doctype html><html><head><style>'
        'html,body{margin:0;padding:0;background:#000;overflow:hidden;}'
        'svg{display:block;width:100vw;height:100vh;}'
        '</style></head><body>' + svg + '</body></html>'
    )

    tmp_dir = out.parent / f"_video_tmp_{int(time.time()*1000)}"
    tmp_dir.mkdir(exist_ok=True)
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(args=["--no-sandbox"])
            context = browser.new_context(
                viewport={"width": w, "height": h},
                record_video_dir=str(tmp_dir),
                record_video_size={"width": w, "height": h},
            )
            page = context.new_page()
            page.set_content(html, wait_until="load")
            page.wait_for_timeout(int(duration_s * 1000))
            video = page.video
            page.close()
            context.close()
            browser.close()
            if video is None:
                raise RuntimeError("playwright did not record a video")
            recorded = Path(video.path())
            shutil.move(str(recorded), str(out))
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
    return out
