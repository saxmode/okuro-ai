# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: provides CLI for generating and rendering animated SVG infographics
# index:
#   imports
#   def main
#   def gen
#   def render
#   def list_templates
#   def _slug
#   def batch
# AGENT_HEADER_END -->
"""tm-ill: topic -> animated SVG infographic."""
from __future__ import annotations
import json
from pathlib import Path

import click

from .compose import compose_via_bridge, compose_from_file
from .render import write_svg, write_png, render_all_sizes
from .brief import load_or_compose_scene
from . import three_render


@click.group()
def main():
    """tm-illustrator: topic -> infographic."""


@main.command("gen")
@click.argument("topic")
@click.option("--category", default=None, help="optional content category")
@click.option("--out", default="./out", help="output directory")
@click.option("--basename", default=None, help="output basename (default: slug of topic)")
@click.option("--all-sizes/--svg-only", default=True,
              help="emit PNGs at 1200x600, 1920x1080, 1080x1080")
@click.option("--seed", type=int, default=None)
def gen(topic: str, category: str | None, out: str, basename: str | None,
        all_sizes: bool, seed: int | None):
    """Generate from a topic via LLM."""
    scene = compose_via_bridge(topic, category=category, seed=seed)
    base = basename or _slug(topic)
    if all_sizes:
        results = render_all_sizes(scene, out, base)
        for k, p in results.items():
            click.echo(f"{k:12} -> {p}")
    else:
        p = write_svg(scene, Path(out) / f"{base}.svg")
        click.echo(f"svg -> {p}")
    # always also dump the scene.json for reproducibility
    Path(out, f"{base}.scene.json").write_text(scene.model_dump_json(indent=2))


@main.command("render")
@click.argument("scene_path", type=click.Path(exists=True))
@click.option("--out", default="./out")
@click.option("--basename", default=None)
@click.option("--all-sizes/--svg-only", default=True)
def render(scene_path: str, out: str, basename: str | None, all_sizes: bool):
    """Render an existing scene.json file (skip LLM)."""
    scene = compose_from_file(scene_path)
    base = basename or Path(scene_path).stem
    if all_sizes:
        results = render_all_sizes(scene, out, base)
        for k, p in results.items():
            click.echo(f"{k:12} -> {p}")
    else:
        p = write_svg(scene, Path(out) / f"{base}.svg")
        click.echo(f"svg -> {p}")


_TEMPLATE_BLURBS = {
    "transformation_bipartite": ("encryption / hashing / before-after",
                                  "left_label, right_label, center_label, motif, n"),
    "central_radial":            ("zero trust / atom / scope / agent loop",
                                  "motif (orbit_rings|iso|constellation), rings, center_label"),
    "axis_flow":                 ("pipeline / supply chain / sequence",
                                  "parts (list of stage names)"),
    "stratified":                ("memory hierarchy / OSI / abstraction layers",
                                  "parts (list of layer names)"),
    "field_with_focus":          ("attention / network / routing",
                                  "n, edges, center_label"),
    "contrast_pair":             ("signal/noise / raw/processed",
                                  "pane_left, pane_right, left_label, right_label"),
    "wave_stack":                "physics / oscillation / frequency / signal",
    "sankey":                    ("data lineage / traffic split / energy budget",
                                  "sankey_left, sankey_right, sankey_flows"),
    "tree":                      ("taxonomies / dependency trees / decision trees",
                                  "tree_nodes, tree_parents"),
    "volume_3d":                 ("voxel grid / embedding cloud / rotation matrix",
                                  "volume_kind, n_3d, clusters, highlight_cell, vector_to"),
    "annotated_specimen":        ("anatomy of X / model card / benchmark profile",
                                  "motif, callouts (4-8), center_label"),
    "small_multiples":           ("A/B / per-country / per-week / model benchmarks",
                                  "multiples (list of [title,stat,values]), highlight_cell_idx"),
    "marey_grid":                ("training pipelines / transit / version evolution",
                                  "stations, trajectories, trajectory_labels, focal_idx"),
    "ridgeline_stack":           ("model comparisons / signal stacks / distributions",
                                  "ridges (list of [name,values]), focal_idx"),
    "sorted_matrix":             ("confusion matrix / attention map / co-occurrence",
                                  "matrix_rows, matrix_cols, matrix, cluster_box"),
    "flow_with_focal_thread":    ("'where does X actually go?' / lineage / budget",
                                  "sankey_*, focal_idx (which flow is the thread)"),
}


@main.command("list-templates")
@click.option("--detail/--names", default=True,
              help="show slot list per template (default) or just names")
def list_templates(detail: bool):
    """List all available templates with their primary slots."""
    from .templates import TEMPLATES
    for name in TEMPLATES.keys():
        info = _TEMPLATE_BLURBS.get(name, ("", ""))
        if isinstance(info, tuple):
            use, slots = info
        else:
            use, slots = info, ""
        if detail:
            click.echo(f"  {name:28} {use}")
            if slots:
                click.echo(f"  {'':28} slots: {slots}")
        else:
            click.echo(name)
    click.echo(f"\n{len(TEMPLATES)} templates total")


def _slug(s: str) -> str:
    return "".join(c.lower() if c.isalnum() else "-" for c in s).strip("-")


@main.command("batch")
@click.argument("topics_file", type=click.Path(exists=True))
@click.option("--out", default="./out_batch", help="output directory")
@click.option("--size", type=click.Choice(["wide", "hd", "square"]),
              default="wide")
@click.option("--accent", default=None,
              help="force one accent across the batch (else LLM picks)")
@click.option("--verbosity", type=click.Choice(["lean", "rich"]),
              default=None, help="force verbosity")
@click.option("--force/--cache", default=False,
              help="force LLM recomposition (else use cache)")
def batch(topics_file: str, out: str, size: str,
          accent: str | None, verbosity: str | None, force: bool):
    """Render a batch of topics from a text file (one topic per line).

    Each line is either:
      - "topic" — uses defaults
      - "topic | category" — sets category
    Topics starting with `#` are skipped.
    Output: <out>/<slug>.png + <out>/<slug>.brief.json per topic.
    """
    from .style import CANVAS_PRESETS
    from .render import write_png
    out_dir = Path(out)
    out_dir.mkdir(parents=True, exist_ok=True)
    w, h = CANVAS_PRESETS[size]
    n_ok = 0
    n_err = 0
    for raw_line in Path(topics_file).read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "|" in line:
            topic, cat = [s.strip() for s in line.split("|", 1)]
        else:
            topic, cat = line, None
        slug = _slug(topic)[:64]
        try:
            scene, hit = load_or_compose_scene(topic, category=cat, force=force)
            # apply per-batch overrides
            if accent:
                scene.accent = accent  # type: ignore[assignment]
            if verbosity:
                # cannot mutate Brief here; verbosity is a Brief slot, not a
                # Scene slot. Skip for now or warn.
                click.echo(f"  warn: --verbosity not yet wired to live scenes")
            scene.canvas.width = w
            scene.canvas.height = h
            png_path = out_dir / f"{slug}.png"
            if scene.render_mode == "3d" and scene.volume is not None:
                v = scene.volume
                three_render.render_volume(
                    kind=v.kind, out_path=png_path, width=w, height=h,
                    eyebrow=v.eyebrow, title=v.title, meta=v.meta,
                    center_label=v.center_label, measure_label=v.measure_label,
                    n=v.n, clusters=v.clusters,
                    highlight=tuple(v.highlight) if v.highlight else None,
                    vector_to=tuple(v.vector_to), cam=tuple(v.cam),
                    seed=scene.seed,
                )
            else:
                write_png(scene, png_path, size=(w, h))
            (out_dir / f"{slug}.scene.json").write_text(
                scene.model_dump_json(indent=2)
            )
            cache_tag = "cache" if hit else "fresh"
            click.echo(f"  ok   [{cache_tag:5}] {topic} -> {png_path.name}")
            n_ok += 1
        except Exception as e:
            click.echo(f"  err   {topic}: {e}")
            n_err += 1
    click.echo(f"\nbatch done: {n_ok} ok, {n_err} errors -> {out_dir}")


if __name__ == "__main__":
    main()
