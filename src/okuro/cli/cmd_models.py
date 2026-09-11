# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: okuro models — registry + catalog search + acquisition.
# index:
#   imports
#   def models (group, lists when bare)
#   def _render_list
#   def models_search
#   def models_pull
# AGENT_HEADER_END -->
"""okuro models — list local/subscription models, search the OSS catalog, and
pull a model into the bundle store."""

import click

from .output import console, data_table, fail, ok


@click.group(invoke_without_command=True)
@click.option("--loaded", is_flag=True, help="Show only currently loaded models.")
@click.option("--scan", is_flag=True, help="Re-scan model directories.")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON.")
@click.pass_context
def models(ctx, loaded, scan, as_json):
    """List available AI models (bundles + local + subscription).

    Subcommands: `search` the OSS catalog, `pull` a model into the store.
    """
    if ctx.invoked_subcommand is not None:
        return
    _render_list(loaded, scan, as_json)


def _render_list(loaded, scan, as_json):
    from okuro.ai_models import list_models, scan_models

    result = scan_models() if scan else list_models()
    if loaded:
        result = [m for m in result if m.get("loaded")]

    if as_json:
        import json

        console.print(json.dumps(result, indent=2, default=str))
        return

    if not result:
        console.print("[dim]No models found[/dim]")
        return

    rows = []
    for m in result:
        rows.append([
            m.get("name", "?"),
            m.get("source", "?"),
            str(m.get("parameters") or m.get("params") or "?"),
            m.get("quantization", "") or "",
            "•" if m.get("loaded") else "",
        ])
    console.print(data_table(["NAME", "SOURCE", "PARAMS", "QUANT", "LOADED"], rows))


@models.command("search")
@click.argument("query")
@click.option("--modality", default="text",
              help="text | image | audio | embedding | video (default: text).")
@click.option("--limit", default=10, help="Max results to fetch per source.")
@click.option("--sort", default="popular", type=click.Choice(["popular", "new"]),
              help="popular = most-downloaded (default); new = recently published.")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON.")
def models_search(query, modality, limit, sort, as_json):
    """Search the OSS catalog (HuggingFace + Civitai), ranked for THIS box."""
    from okuro.ai_models import discover, rank
    from okuro.capability import capabilities

    entries = discover(query, modality=modality, limit=limit, sort=sort)
    detection = capabilities()
    ranked = rank(entries, detection, modality=modality)

    if as_json:
        import json

        console.print(json.dumps([e.to_dict() for e in ranked], indent=2, default=str))
        return

    if not ranked:
        console.print(f"[dim]No runnable {modality} models found for '{query}'.[/dim]")
        return

    rows = []
    for e in ranked:
        best = e.best_runnable_format
        rows.append([
            e.catalog_id,
            e.display_name,
            f"{e.min_vram_gb:.0f}GB" if best else "?",
            str(e.quality_signals.get("downloads", 0)),
            "yes" if e.license.get("commercial_use") else "no",
            "•" if e.gated else "",
        ])
    console.print(data_table(
        ["CATALOG_ID", "NAME", "VRAM", "DOWNLOADS", "COMMERCIAL", "GATED"], rows
    ))


@models.command("pull")
@click.argument("catalog_id")
@click.option("--force", is_flag=True, help="Proceed despite a VRAM-fit warning.")
@click.option("--yes", is_flag=True, help="Skip the confirmation prompt.")
def models_pull(catalog_id, force, yes):
    """Download a model into the bundle store (e.g. huggingface:Qwen/Qwen2.5-Coder-7B-Instruct)."""
    from okuro.ai_models import pull
    from okuro.ai_models.acquire import AcquisitionError, plan
    from okuro.ai_models.discovery import resolve_entry
    from okuro.capability import capabilities

    entry = resolve_entry(catalog_id)
    if entry is None:
        fail(f"Could not resolve '{catalog_id}'. Use '<source>:<ref>' or an HF repo id.")
        return

    try:
        acq = plan(entry)
    except AcquisitionError as exc:
        fail(str(exc))
        return

    console.print(
        f"[bold]{entry.display_name}[/bold] → bundle [cyan]{acq.bundle_id}[/cyan] "
        f"(~{acq.est_size_gb:.1f} GB, {acq.variant.format})"
    )
    if not yes and not click.confirm("Download now?", default=True):
        return

    try:
        bundle = pull(entry, detection=capabilities(), allow_oversize=force)
    except AcquisitionError as exc:
        fail(str(exc))
        return

    ok(f"Pulled {bundle.id} ({bundle.size_gb:.1f} GB) → {bundle.path}")


@models.command("suggest")
@click.option("--modality", default="text", help="text | image | audio | ... (default: text).")
@click.option("-k", "k", default=5, help="How many top fit models to research.")
@click.option("--no-publish", is_flag=True, help="Research + print only; don't file signals.")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON.")
def models_suggest(modality, k, no_publish, as_json):
    """Research box-fit OSS models against your goals and file them as suggestions."""
    from okuro.ai_models.suggest import suggest_models, publish, _profile_goals
    from okuro.ai_models.edition import detect_edition, effective_detection, local_inference_enabled
    from okuro.capability import capabilities

    detection = capabilities()
    if not local_inference_enabled(detection):
        fail(f"Local inference is off on okuro-{detect_edition(detection)} (no local GPU). "
             f"Model suggestions need okuro-advanced or okuro-pro.")
        return

    sugs = suggest_models(effective_detection(detection), _profile_goals(), modality=modality, k=k)

    if as_json:
        import json

        console.print(json.dumps([s.to_evidence() for s in sugs], indent=2))
    else:
        rows = [[s.display_name[:36], f"{s.min_vram_gb:.0f}GB", s.fit_gpu,
                 "LLM" if s.researched else "auto", s.rationale[:60]] for s in sugs]
        console.print(data_table(["NAME", "VRAM", "FITS", "SRC", "WHY"], rows))

    if no_publish:
        return
    created = publish(sugs)
    ok(f"Filed {len(created)} new suggestion(s) → signals (Now page / inbox / digest)")


@models.command("gpu")
def models_gpu():
    """Show GPU tenants — okuro's leased models + external holders (tm-inference / gaso / comfyui)."""
    from okuro.inference.broker import Broker

    report = Broker().tenants_report()
    if not report:
        console.print("[dim]No GPUs detected.[/dim]")
        return
    for idx, g in report.items():
        free = g["smi_free_gb"]
        console.print(
            f"[bold]GPU {idx}[/bold]  total {g['total_gb']:.0f}GB · "
            f"free {free:.1f}GB" if free is not None else f"[bold]GPU {idx}[/bold]  total {g['total_gb']:.0f}GB"
        )
        for lease in g["okuro_leases"]:
            console.print(f"  [green]okuro[/green]  {lease['model_id']} "
                          f"({lease['tier']}, {lease['vram_gb']:.1f}GB)")
        for ext in g["external"]:
            console.print(f"  [yellow]{ext['tenant']}[/yellow]  {ext['name']} "
                          f"pid={ext['pid']} ({ext['used_mb']}MB)")
        if not g["okuro_leases"] and not g["external"]:
            console.print("  [dim]idle[/dim]")


@models.command("probe")
@click.argument("bundle_id")
@click.option("--gpu", "gpu_index", default=0, help="GPU index to probe on (default 0).")
@click.option("--ctx", "ctx", default=None, type=int, help="Context length to verify (default: bundle's).")
@click.option("--yes", is_flag=True, help="Skip the confirmation prompt.")
def models_probe(bundle_id, gpu_index, ctx, yes):
    """Load-probe a bundle: actually run it, MEASURE VRAM, stamp qualification.

    Manual only — this spawns an engine and uses GPU/VRAM on this box. okuro
    never probes automatically (the analytic KV-aware estimate is the default
    fit signal; this is the measured confirmation, on demand).
    """
    from okuro.ai_models.bundle import bundles_root, load_bundle, write_bundle
    from okuro.ai_models.edition import detect_edition, local_inference_enabled
    from okuro.capability import capabilities

    detection = capabilities()
    if not local_inference_enabled(detection):
        fail(f"Local inference is off on okuro-{detect_edition(detection)} (no local GPU).")
        return

    bundle = load_bundle(bundles_root() / bundle_id)
    if bundle is None:
        fail(f"Unknown bundle: {bundle_id}")
        return

    target = ctx or bundle.context_length or 8192
    console.print(
        f"[bold]{bundle.id}[/bold] — will load on GPU {gpu_index} @ {target} ctx "
        f"(analytic estimate [cyan]{bundle.vram_gb:.1f}GB[/cyan]) and measure real VRAM."
    )
    if not yes and not click.confirm("Run the load-probe now? (uses GPU)", default=False):
        return

    from okuro.inference.probe import probe_bundle

    res = probe_bundle(bundle, gpu_index=gpu_index, context_length=target)
    if res.get("serving_ready"):
        write_bundle(bundle)  # persist the qualification scorecard
        ok(f"serving-ready ✓  measured {res['measured_vram_gb']}GB @ {res['max_context_verified']} ctx "
           f"(latency {res['latency_s']}s) — qualification saved")
    else:
        fail(f"probe failed: {res.get('error')}")
