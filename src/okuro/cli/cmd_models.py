# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: okuro models — registry + catalog search + acquisition.
# index:
#   imports
#   def models (group, lists when bare)
#   def _render_list
#   def models_search
#   def models_pull  (P6: store-aware, --dry-run, --store, --to, --file)
#   def models_inventory
#   def models_swap (group: propose/list/show/tick/apply/reject)
# AGENT_HEADER_END -->
"""okuro models — list local/subscription models, search the OSS catalog, and
pull a model into the bundle store."""

import click

from .output import console, data_table, fail, kv_table, ok, warn


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
@click.option("--store", default=None,
              help="Which declared store to pull into (e.g. nvme, raid). "
                   "Default: the first HOT store.")
@click.option("--to", "to_rel", default=None,
              help="Destination RELATIVE to the store root, overriding the "
                   "modality bucket.")
@click.option("--file", "only_file", default=None,
              help="Exact filename or glob to fetch — the answer to a repo "
                   "that ships 27 quants (e.g. '*Q4_K_M*').")
@click.option("--dry-run", "dry_run", is_flag=True,
              help="Destination, headroom and the file list. Downloads nothing.")
@click.option("--bundle", "as_bundle", is_flag=True,
              help="Pull into okuro's own bundle store instead of a model "
                   "store (the pre-P6 behaviour).")
@click.option("--force", is_flag=True, help="Proceed despite a VRAM-fit warning.")
@click.option("--yes", is_flag=True, help="Skip the confirmation prompt.")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON.")
def models_pull(catalog_id, store, to_rel, only_file, dry_run, as_bundle,
                force, yes, as_json):
    """Download a model into a declared store (e.g. Qwen/Qwen3-8B-GGUF).

    Accepts a bare HuggingFace repo id, ``huggingface:Org/Name`` or
    ``civitai:<id>``. The destination is the first HOT store plus the bucket
    the model's modality belongs in, and a headroom shortfall WARNS with the
    numbers rather than refusing — there is no headroom floor.

    With no store declared (or ``--bundle``) this falls back to okuro's own
    bundle store, which is what it did before P6.
    """
    from okuro.ai_models import acquire
    from okuro.ai_models.acquire import AcquisitionError

    stores_declared = bool(acquire.pick_store()[0])
    if as_bundle or not stores_declared:
        if not stores_declared and not as_bundle:
            warn("no model stores declared — falling back to the bundle store. "
                 "Set conventions.ai_models.model_stores to pull into a store.")
        _pull_into_bundle_store(catalog_id, force, yes)
        return

    try:
        res = acquire.pull_model(catalog_id, store=store, to=to_rel,
                                 file=only_file, dry_run=True)
    except AcquisitionError as exc:
        fail(str(exc))
        return

    if as_json and dry_run:
        import json

        click.echo(json.dumps(res, indent=2, default=str))
        return

    if not res.get("ok"):
        fail(res.get("reason") or "could not plan this pull")
        return

    _render_pull_plan(res)

    if dry_run:
        return
    if not yes and not click.confirm("Download now?", default=True):
        return

    try:
        res = acquire.pull_model(catalog_id, store=store, to=to_rel,
                                 file=only_file, dry_run=False)
    except AcquisitionError as exc:
        fail(str(exc))
        return

    if as_json:
        import json

        click.echo(json.dumps(res, indent=2, default=str))
        return
    _render_pull_result(res)


def _render_pull_plan(res: dict) -> None:
    """Destination, headroom and the file list — the dry-run body."""
    d = res["destination"]
    f = res["files"]
    console.print(
        f"[bold]{res.get('display_name')}[/bold] "
        f"({res.get('format') or '?'}{'/' + res['quant'] if res.get('quant') else ''})")
    console.print(kv_table([
        ("destination", d["path"]),
        ("store", f"{d['store']} ({d['tier']})"),
        ("why", d["why"]),
        ("files", f"{len(f['files'])} file(s)"
                  + (f", {f['total_gb']} GB" if f.get("total_gb") else
                     ", size not published")),
        ("selection", f["why"]),
    ]))
    for row in f["files"][:20]:
        size = row.get("size_bytes")
        console.print(f"  [dim]{(str(round(size / 1024**3, 2)) + ' GB') if size else '     ?'}[/dim]  {row['name']}")
    if len(f["files"]) > 20:
        console.print(f"  [dim]… {len(f['files']) - 20} more[/dim]")

    h = res["headroom"]
    (warn if h["warn"] or not h["size_known"] else ok)(h["text"])


def _render_pull_result(res: dict) -> None:
    """What landed, what okuro now knows about it, and where to see it."""
    if not res.get("ok"):
        fail(res.get("reason")
             or (res.get("registration") or {}).get("reason")
             or "the pull did not complete")
        return
    d = res["destination"]
    ok(f"pulled {res.get('display_name')} → {d['path']} "
       f"({res.get('downloaded_gb')} GB)")
    rows = []
    for u in res.get("units") or []:
        best = (u.get("best_fit") or {})
        rows.append([u["unit_id"], f"{u.get('size_gb', 0):,.1f}",
                     u.get("family") or "-", u.get("quant") or "-",
                     f"{best.get('mode', '-')}"
                     + (f" · {best['gpu']}" if best.get("gpu") else "")])
    if rows:
        console.print(data_table(["UNIT", "GB", "FAMILY", "QUANT",
                                  "BEST PLACEMENT"], rows))
    if res.get("discovery_marked_installed"):
        console.print("[dim]discovery row marked installed[/dim]")
    for r in res.get("results") or []:
        console.print(f"[dim]see results ({r['label']}):[/dim] {r['url']}")


def _pull_into_bundle_store(catalog_id, force, yes) -> None:
    """The pre-P6 path: acquire into okuro's own bundle store."""
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


@models.command("inventory")
@click.option("--store", default=None, help="Only this configured store (e.g. nvme, raid).")
@click.option("--format", "fmt", default=None,
              help="Only this weights format: gguf|safetensors|bin|pth|onnx|ckpt|other|mixed.")
@click.option("--twins", "show_twins", is_flag=True,
              help="Show units that exist on more than one store.")
@click.option("--broken", "only_broken", is_flag=True,
              help="Only units whose weights are stubs, runt shards or dangling links.")
@click.option("--refresh", "refresh_mode", default=None,
              type=click.Choice(["stat", "identity"]),
              help="Re-walk the stores first. stat = dir-stat only (never opens a "
                   "file); identity = also fingerprint each unit.")
@click.option("--limit", default=40, show_default=True, help="Rows to print.")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON.")
def models_inventory(store, fmt, show_twins, only_broken, refresh_mode, limit, as_json):
    """Inventory the host's configured model stores at unit granularity.

    Reads the cached table by default; `--refresh stat` re-walks the stores
    live and merges the deltas.
    """
    from okuro.ai_models import store_scan

    inv = store_scan.inventory(
        store=store, fmt=fmt,
        status="broken" if only_broken else None,
        refresh_mode=refresh_mode, want_twins=show_twins,
        limit=10_000 if as_json else max(limit, 1),
    )

    if as_json:
        import json

        # click.echo, not console.print: rich hard-wraps at the terminal width
        # and a wrapped path inside a JSON string is no longer parseable JSON.
        click.echo(json.dumps(inv, indent=2, default=str))
        return

    if not inv.get("configured"):
        warn(inv["reason"])
        return

    scan = inv.get("scan") or {}
    for res in scan.get("stores", []) or []:
        if res.get("skipped"):
            console.print(f"[dim]{res['store']}: {res['skipped']}[/dim]")
        elif "identified" in res:
            console.print(f"[dim]{res['store']}: fingerprinted {res['identified']} "
                          f"of {res['candidates']} unidentified unit(s)[/dim]")
        elif "units" in res:
            console.print(
                f"[dim]{res['store']}: {res['units']} units, {res['unit_gb']} GB "
                f"(+{res['added']} new, {res['missing']} now missing) · tree "
                f"{res['tree_gb']} GB in {res['tree_files']} files, "
                f"{res['non_unit_gb']} GB outside units[/dim]")
        for err in (res.get("errors") or [])[:3]:
            warn(err)

    console.print(data_table(
        ["STORE", "TIER", "UNITS", "GB", "BROKEN", "MISSING", "IDENT", "LAST SCAN"],
        [[s["store"],
          next((c["tier"] for c in inv["stores"] if c["name"] == s["store"]), "?"),
          str(s["units"]), f"{s['size_gb']:,.1f}", str(s["broken"]),
          str(s["missing"]), str(s["identified"]), s["last_scanned"] or "never"]
         for s in inv["summary"]],
        title="Model stores"))

    if show_twins:
        for how in ("identity", "size"):
            rows = inv["twins"][how]
            if not rows:
                console.print(f"[dim]no cross-store twins by {how}[/dim]")
                continue
            console.print(data_table(
                ["GB", "STORES", "EVIDENCE", "UNITS"],
                [[f"{t['size_gb']:,.1f}", t["stores"], t["evidence"],
                  (t["unit_ids"] or "")[:70]] for t in rows[:limit]],
                title=f"Cross-store twins by {how}"))
        return

    units = inv["units"]
    if not units:
        console.print("[dim]No units — run with --refresh stat to walk the stores.[/dim]")
        return
    console.print(data_table(
        ["STORE", "GB", "FMT", "ST", "TIER", "CONSUMERS", "UNIT"],
        [[u["store"], f"{u['size_gb']:,.1f}", u["format"],
          "✓" if u["status"] == "ok" else u["status"],
          u.get("tier") or "—",
          ", ".join(u.get("consumers") or [])[:28] or "—",
          u["rel_path"][:52]]
         for u in units[:limit]],
        title=f"Model units ({inv['totals']['units']} shown, "
              f"{inv['totals']['size_gb']:,.1f} GB)"))
    for u in units[:limit]:
        if u["status"] != "ok" and u.get("note"):
            warn(f"{u['rel_path']}: {u['note']}")


@models.command("consumers")
@click.option("--unit", default=None,
              help="Only references that resolve to this unit (id or substring).")
@click.option("--consumer", default=None, help="Only this consumer, by name.")
@click.option("--tier", default=None, type=click.Choice(["PROTECTED", "ACTIVE", "ARCHIVE"]),
              help="Only consumers in this protection tier.")
@click.option("--dead", "only_dead", is_flag=True,
              help="Only references that resolve to no unit on this host.")
@click.option("--unreachable", "only_unreachable", is_flag=True,
              help="Only references the consumer's own mount cannot reach.")
@click.option("--unreferenced", "show_unreferenced", is_flag=True,
              help="Also list units no configured consumer names.")
@click.option("--refresh", "do_refresh", is_flag=True,
              help="Re-walk the consumer roots first and merge the deltas.")
@click.option("--limit", default=40, show_default=True, help="Rows to print.")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON.")
def models_consumers(unit, consumer, tier, only_dead, only_unreachable,
                     show_unreferenced, do_refresh, limit, as_json):
    """Which tool on this host uses which model, with the file:line that proves it.

    Reads the cached table by default; `--refresh` re-walks the declared
    consumer roots. A reference that resolves to no unit is kept and shown as
    a DEAD ref — that is the finding, not an error.
    """
    from okuro.ai_models import consumers as consumer_scan

    res = consumer_scan.consumer_map(
        unit=unit, consumer=consumer, tier=tier, dead=only_dead,
        unreachable=only_unreachable, refresh=do_refresh,
        unreferenced=show_unreferenced,
        limit=10_000 if as_json else max(limit, 1),
    )

    if as_json:
        import json

        # click.echo, not console.print — rich hard-wraps at the terminal
        # width and a wrapped path inside a JSON string stops being JSON.
        click.echo(json.dumps(res, indent=2, default=str))
        return

    if not res.get("configured"):
        warn(res["reason"])
        return

    scan = res.get("scan") or {}
    if scan:
        console.print(f"[dim]scanned {scan.get('roots', 0)} root(s): "
                      f"{scan.get('rows', 0)} references over "
                      f"{scan.get('consumers', 0)} consumers "
                      f"(+{scan.get('added', 0)} new, {scan.get('removed', 0)} gone) · "
                      f"{scan.get('dead', 0)} dead, "
                      f"{scan.get('unreachable', 0)} unreachable[/dim]")
        for err in (scan.get("errors") or [])[:5]:
            warn(err)

    console.print(data_table(
        ["TIER", "CONSUMER", "RUN MODE", "STATE", "REFS", "UNITS", "DEAD", "UNREACH"],
        [[c["tier"], c["consumer"], c["run_mode"], c["state"], str(c["refs"]),
          str(c["units"]), str(c["dead"]), str(c["unreachable"])]
         for c in res["consumers"]],
        title="Model consumers"))

    rows = res["rows"]
    if rows:
        console.print(data_table(
            ["CONSUMER", "REF", "UNIT", "R", "LOCATION"],
            [[r["consumer"], r["model_ref"][-44:],
              (r["unit_id"] or "— DEAD REF")[-40:],
              "✓" if r["reachable"] else "✗",
              f"{r['config_path'].split('/')[-1]}:{r['line']}"]
             for r in rows[:limit]],
            title=f"References ({res['totals']['rows']} shown, "
                  f"{res['totals']['dead']} dead, "
                  f"{res['totals']['unreachable']} unreachable)"))
        for r in rows[:limit]:
            if r["note"] and r["note"] != "dead-ref":
                warn(f"{r['consumer']} · {r['model_ref']}: {r['note']}")
    else:
        console.print("[dim]No references — run with --refresh to walk the "
                      "consumer roots.[/dim]")

    if show_unreferenced:
        un = res.get("unreferenced") or []
        console.print(data_table(
            ["STORE", "GB", "UNIT"],
            [[u["store"], f"{u['size_gb']:,.1f}", u["rel_path"][:72]]
             for u in un[:limit]],
            title=f"Units no configured consumer names ({len(un)})"))
        console.print("[dim]Evidence for a conversation, never grounds for "
                      "deleting anything: a loader can build a path at runtime, "
                      "and such a model has no string reference anywhere.[/dim]")


# --- P3: lineage + placement-space fit --------------------------------------


def _subject_flags(unit, release):
    """Exactly one of --unit / --release, or a clear error."""
    if unit and release:
        fail("pass --unit or --release, not both")
        return None
    if not unit and not release:
        fail("pass --unit <id-or-path> or --release <hf-id>")
        return None
    return ("unit", unit) if unit else ("release", release)


@models.command("fit")
@click.option("--unit", default=None,
              help="An installed unit, by unit_id, rel_path or name.")
@click.option("--release", default=None,
              help="A discovery candidate, by HuggingFace id or name.")
@click.option("--size-gb", default=None, type=float,
              help="Card size for a --release that carries none.")
@click.option("--now", "as_now", is_flag=True,
              help="Also decide against what is free on the GPUs right now.")
@click.option("--ctx", "ctx", default=None, type=int,
              help="Context length to size the KV cache at (default: the host's).")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON.")
def models_fit(unit, release, size_gb, as_now, ctx, as_json):
    """Where on this host's hardware a model could actually run.

    Not one GPU's verdict: the whole placement space — one card, both cards
    split, GPU plus system RAM, MoE expert offload, CPU alone — ranked fast to
    slow. "Runnable" is the OR over that set.
    """
    import json as _json

    from okuro.ai_models import fitting as F

    picked = _subject_flags(unit, release)
    if picked is None:
        return
    kind, value = picked
    hw = F.detect_hardware(now=as_now)
    if kind == "unit":
        res = F.fit_unit(value, hw, now=as_now, context=ctx)
    else:
        res = F.fit_release(value, hw, now=as_now, size_gb=size_gb, context=ctx)

    if as_json:
        click.echo(_json.dumps(res, indent=2))
        return

    if res.get("found") is False:
        fail(res.get("reason") or "not found")
        return
    if not res.get("placements"):
        warn(res.get("reason") or "nothing to place")
        return
    if res.get("warning"):
        warn(res["warning"])

    s, h = res["subject"], res["hardware"]
    cards = ", ".join(f"{g['name']} {g['total_gb']:g} GB" for g in h["gpus"])
    console.print(
        f"[bold]{s['name'] or s['subject_id']}[/bold] — {s['size_gb']:,.1f} GB "
        f"{s['quant'] or 'quant unknown'} · {s['modality']} · "
        f"{s['context']} ctx")
    console.print(f"[dim]{cards} · RAM {h['ram_available_gb']:g} of "
                  f"{h['ram_total_gb']:g} GB available · "
                  f"weights {res['weights_gb']:,.1f} GB "
                  f"({res['weights_basis']}) · KV {res['kv_gb']:,.1f} GB · "
                  f"basis {res['basis']}[/dim]")

    rows = []
    for p in res["placements"]:
        mark = "yes" if p["fits_idle"] else "no"
        if p["fits_idle"] and as_now:
            mark = "yes" if p["fits_now"] else "idle only"
        rows.append([
            p["mode"], p["gpu"] or "-",
            f"{p['est_vram_gb']:,.1f}" if p["est_vram_gb"] else "-",
            f"{p['est_ram_gb']:,.1f}" if p["est_ram_gb"] else "-",
            f"{p['offloaded_pct']:.0f}%" if p["offloaded_pct"] else "-",
            p["speed_class"], mark,
        ])
    console.print(data_table(
        ["MODE", "GPU", "VRAM GB", "RAM GB", "OFFLOAD", "SPEED",
         "FITS NOW" if as_now else "FITS"],
        rows, title="Placements, fastest first"))

    if res.get("placement_note"):
        warn(res["placement_note"])

    best = res.get("best")
    if best:
        ok(f"best when idle: {best['mode']}"
           + (f" on {best['gpu']}" if best["gpu"] else "")
           + f" — {best['speed_class']}")
        console.print(f"[dim]{best['why']}[/dim]")
    else:
        warn("no placement fits this host, even with CPU-only")
    if as_now:
        bn = res.get("best_now")
        if bn:
            ok(f"best right now: {bn['mode']}"
               + (f" on {bn['gpu']}" if bn["gpu"] else ""))
        elif res.get("runnable_now") is False:
            warn("nothing fits right now — the GPUs are busy; this is a queue, "
                 "not a no")


@models.command("lineage")
@click.option("--unit", default=None,
              help="An installed unit, by unit_id, rel_path or name.")
@click.option("--release", default=None,
              help="A candidate, by HuggingFace id or name.")
@click.option("--all", "show_all", is_flag=True,
              help="Also list units this candidate is unrelated to.")
@click.option("--limit", default=20, show_default=True, help="Rows to print.")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON.")
def models_lineage(unit, release, show_all, limit, as_json):
    """What line a model is from, and how it relates to what is installed.

    For a --release this answers the question the discovery feed could not:
    is this an update to something here, another quant of it, a variant, or
    nothing to do with it.
    """
    import json as _json

    from okuro.ai_models import lineage as L

    picked = _subject_flags(unit, release)
    if picked is None:
        return
    kind, value = picked
    res = (L.unit_lineage(value) if kind == "unit"
           else L.relate_release(value, include_unrelated=show_all))

    if as_json:
        click.echo(_json.dumps(res, indent=2))
        return

    if kind == "unit":
        if not res.get("found"):
            fail(res.get("reason") or "not found")
            return
        u, p = res["unit"], res["lineage"]
        console.print(f"[bold]{u['rel_path']}[/bold] — {u['size_gb']:,.1f} GB "
                      f"on {u['store']}")
        console.print(
            f"[dim]family {p['family_label'] or 'unknown'} · "
            f"{_params(p)} · quant {p['quant'] or '-'} · "
            f"tags {', '.join(p['variant_tags']) or '-'} · "
            f"author {p['author'] or '-'} · confidence {p['confidence']}[/dim]")
        for n in p.get("notes") or []:
            console.print(f"[dim]  note: {n}[/dim]")
        rel = res.get("siblings") or []
        title = f"Other units in family {p['family'] or '?'} ({len(rel)})"
    else:
        c = res["candidate"]
        console.print(f"[bold]{c['raw'] if 'raw' in c else value}[/bold]")
        console.print(
            f"[dim]family {c['family_label'] or 'unknown'} · "
            f"{_params(c)} · quant {c['quant'] or '-'} · "
            f"tags {', '.join(c['variant_tags']) or '-'}[/dim]")
        if res.get("reason"):
            warn(res["reason"])
            return
        rel = res.get("relations") or []
        title = f"Relation to installed units ({len(rel)})"

    if not rel:
        console.print("[dim]No installed unit of this family — nothing to "
                      "relate it to. That is not the same as unrelated.[/dim]")
        return
    console.print(data_table(
        ["RELATION", "CONF", "STORE", "GB", "QUANT", "UNIT"],
        [[r["relation"], f"{r['confidence']:.2f}",
          r["unit"].get("store", "-"),
          f"{r['unit'].get('size_gb', 0):,.1f}",
          r["unit"].get("quant") or "-",
          (r["unit"].get("rel_path") or r["unit"].get("unit_id") or "")[:52]]
         for r in rel[:limit]],
        title=title))
    for r in rel[:limit]:
        console.print(f"[dim]{r['relation']}: {r['why']}[/dim]")


def _params(p: dict) -> str:
    t, a = p.get("params_total_b"), p.get("params_active_b")
    if t and a:
        return f"{t:g}B total / {a:g}B active (MoE)"
    if t:
        return f"{t:g}B"
    return "size unknown"


@models.command("discoveries")
@click.option("--category", default=None,
              help="One wanted bucket: text | text-abliterated | image | "
                   "image-nsfw | video | video-nsfw | music | vision | 3d | "
                   "embedding.")
@click.option("--variants", type=click.Choice(["hide", "show"]), default="hide",
              show_default=True,
              help="Rows tagged abliterated / uncensored / nsfw / roleplay are "
                   "TAGGED, never dropped — this is whether they are printed.")
@click.option("--interesting", "only_interesting", is_flag=True,
              help="Only rows the scan judged worth a look.")
@click.option("--status", default=None,
              type=click.Choice(["new", "acknowledged", "installed", "dismissed"]))
@click.option("--limit", default=40, show_default=True, help="Rows to print.")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON.")
def models_discoveries(category, variants, only_interesting, status, limit, as_json):
    """What the weekly release scan found, per wanted category.

    A category that found nothing is PRINTED as a zero-result row rather than
    left out: an empty list is ambiguous between "nothing was released" and
    "this scanner is broken", and those need different actions.
    """
    import json as _json

    from okuro.ai_models import list_category_scans, list_discoveries

    rows = list_discoveries(category=category, variants=variants,
                            interesting_only=only_interesting,
                            status=status, limit=limit)
    ledger = [r for r in list_category_scans()
              if not category or r["category"] == category]

    if as_json:
        click.echo(_json.dumps({"discoveries": rows, "categories": ledger},
                               indent=2, default=str))
        return

    if ledger:
        console.print(data_table(
            ["CATEGORY", "SOURCE", "FETCHED", "GATE", "PUBLISHED", "INTERESTING", "STATE"],
            [[r["category"], r["source"], str(r["fetched"]), str(r["passed_gate"]),
              str(r["published"]), str(r["interesting"]),
              "[yellow]no reputable release[/yellow]" if r["zero_result"] else "ok"]
             for r in ledger],
            title=f"Last scan per category ({len(ledger)})"))

    if not rows:
        console.print("[dim]No discoveries match. `--variants show` includes "
                      "the tagged identity variants.[/dim]")
        return
    console.print(data_table(
        ["NAME", "CAT", "GB", "PLACEMENT", "RELATION", "TAGS", "WHY"],
        [[(r.get("display_name") or r["catalog_id"])[:30],
          r.get("category") or "-",
          f"{(r.get('min_vram_gb') or 0):.1f}",
          _best_placement(r),
          (r.get("relation") or "-")[:26],
          ",".join(r.get("identity_variants") or []) or "-",
          ("★ " if r.get("interesting") else "") + (r.get("interesting_why") or "")[:44]]
         for r in rows],
        title=f"Discoveries ({len(rows)}, variants={variants})"))


def _best_placement(row: dict) -> str:
    """The stored best placement for a candidate, from migration 152's table."""
    try:
        from okuro.ai_models.fitting import best_fits

        got = best_fits([row["catalog_id"]], subject_kind="release") or {}
        best = got.get(row["catalog_id"]) or {}
        return (best.get("mode") or "-")[:22]
    except Exception:
        return "-"


@models.command("scan")
@click.option("--task", default="model-suggestions", show_default=True,
              type=click.Choice(["model-suggestions", "model-store-scan"]),
              help="Which daemon task handler to run once, in this process.")
@click.option("--max-research", default=None, type=int,
              help="Cap the LLM research calls for this run (model-suggestions "
                   "only). 0 = none; the rationale degrades to the "
                   "deterministic purpose summary.")
@click.option("-k", "k", default=3, show_default=True,
              help="Candidates per category to publish (model-suggestions).")
@click.option("--category", default=None,
              help="Run one category only (model-suggestions).")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON.")
def models_scan(task, max_research, k, category, as_json):
    """Run a model daemon task ONCE, now, in this process.

    The gap this closes: every phase that adds a pass to a scheduled task
    leaves existing rows empty until the schedule next fires, and there was no
    trigger but waiting for the cron. Same handler, same code path — this is
    not a second implementation of the scan.
    """
    import json as _json

    if task == "model-store-scan":
        from okuro.ai_models.store_scan import scan_task

        result = scan_task()
    else:
        from okuro.ai_models.suggest import run_suggestions

        result = run_suggestions(k=k, max_research=max_research,
                                 only_category=category)

    if as_json:
        click.echo(_json.dumps(result, indent=2, default=str))
        return
    if isinstance(result, str):
        ok(result)
        return
    console.print(_json.dumps(result, indent=2, default=str))


# --- P5: swap plans ---------------------------------------------------------


@models.group("swap")
def models_swap():
    """Plan a model replacement — and never apply one.

    A swap plan is a set of PROPOSED line edits. okuro shows the exact
    `file:line` and the diff; a person makes the edit. `swap apply` records
    that it was applied and prints the patch — it writes no consumer file.
    """


def _render_plan(plan: dict, *, show_diff: bool = True, limit: int = 40):
    """One plan, rendered the same way by `show`, `tick`, `apply` and `reject`."""
    t = plan["totals"]
    console.print(kv_table([
        ("plan", plan["plan_group"]),
        ("consumer", f"{plan['consumer']}  [{plan['tier']}]"),
        ("mode", plan["mode"]),
        ("state", plan["state"]),
        ("old", plan["unit_old"]),
        ("new", f"{plan['new_ref']}  ({plan['new_kind']})"),
        ("relation", f"{plan['relation'] or '—'}"
                     f"{' · ' + plan['relation_why'] if plan['relation_why'] else ''}"),
        ("lines", f"{t['rows']} over {t['files']} file(s) — "
                  f"{t['rewritable']} rewritable, {t['unrewritable']} not"),
        ("gate", f"{t['checklist_done']}/{t['checklist_total']} ticked · "
                 f"{t['blocking']} blocker(s), {t['warnings']} warning(s)"),
    ], title="Swap plan"))

    console.print(data_table(
        ["#", "DONE", "CHECKLIST ITEM"],
        [[str(i), "✓" if c["done"] else " ", c["item"]]
         for i, c in enumerate(plan["checklist"], 1)],
        title=f"Gate — `okuro models swap tick {plan['plan_group']} N`"))

    for b in plan["blockers"]:
        (warn if b["severity"] == "warn" else fail)(f"[{b['severity']}] {b['text']}")

    console.print(data_table(
        ["LOCATION", "KIND", "RW", "NEW REFERENCE"],
        [[f"{r['config_path'].split('/')[-1]}:{r['line']}", r["match_kind"] or "—",
          "✓" if r["rewritable"] else "✗",
          (r["new_ref_written"] or "—")[-58:]]
         for r in plan["rows"][:limit]],
        title=f"Lines this swap would change ({t['rows']})"))
    for r in plan["rows"][:limit]:
        if r["rewrite_note"]:
            warn(f"{r['config_path']}:{r['line']} — {r['rewrite_note']}")

    if show_diff:
        for r in plan["rows"][:limit]:
            if r["diff"]:
                console.print(r["diff"].rstrip("\n"), highlight=False)


def _emit(res: dict, as_json: bool, *, show_diff: bool = True) -> None:
    import json as _json

    if as_json:
        click.echo(_json.dumps(res, indent=2, default=str))
        return
    if res.get("configured") is False:
        warn(res["reason"])
        return
    if not res.get("ok") and res.get("plan") is None:
        fail(res.get("reason") or "refused")
        return
    if not res.get("ok"):
        fail(res.get("reason") or "refused")
        console.print("")
    _render_plan(res["plan"], show_diff=show_diff)


@models_swap.command("propose")
@click.option("--consumer", required=True, help="The tool whose config would change.")
@click.option("--old", "unit_old", required=True,
              help="The unit being replaced (unit_id, rel_path or name).")
@click.option("--new", "new_ref", required=True,
              help="The replacement — an installed unit, or a HuggingFace id "
                   "for a release that is not downloaded yet.")
@click.option("--side-by-side", "side_by_side", is_flag=True,
              help="Keep both models. Nothing is rewritten; the diff still "
                   "shows what a replace would change, and the headroom "
                   "warning still applies because the new bytes are added.")
@click.option("--note", default=None, help="Free text stored with the plan.")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON.")
def models_swap_propose(consumer, unit_old, new_ref, side_by_side, note, as_json):
    """Propose a swap. Writes a plan; writes no consumer file."""
    from okuro.ai_models import swap

    _emit(swap.propose(consumer, unit_old, new_ref,
                       mode="side-by-side" if side_by_side else "replace",
                       note=note), as_json)


@models_swap.command("list")
@click.option("--consumer", default=None, help="Only this consumer.")
@click.option("--state", default=None,
              type=click.Choice(["proposed", "testing", "applied", "rejected"]))
@click.option("--unit", default=None, help="Only plans replacing this unit.")
@click.option("--limit", default=50, show_default=True)
@click.option("--json", "as_json", is_flag=True, help="Output as JSON.")
def models_swap_list(consumer, state, unit, limit, as_json):
    """Every swap plan, newest first."""
    import json as _json

    from okuro.ai_models import swap

    res = swap.list_plans(consumer=consumer, state=state, unit=unit, limit=limit)
    if as_json:
        click.echo(_json.dumps(res, indent=2, default=str))
        return
    if not res.get("configured"):
        warn(res["reason"])
        return
    if not res["plans"]:
        console.print("[dim]No swap plans — `okuro models swap propose "
                      "--consumer C --old U --new N`.[/dim]")
        return
    console.print(data_table(
        ["PLAN", "STATE", "CONSUMER", "TIER", "MODE", "GATE", "LINES", "NEW"],
        [[p["plan_group"], p["state"], p["consumer"], p["tier"], p["mode"],
          f"{p['totals']['checklist_done']}/{p['totals']['checklist_total']}",
          f"{p['totals']['rewritable']}/{p['totals']['rows']}",
          p["new_ref"][-34:]]
         for p in res["plans"]],
        title=f"Swap plans ({res['totals']['plans']})"))


@models_swap.command("show")
@click.argument("plan")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON.")
def models_swap_show(plan, as_json):
    """One plan, with its checklist, its blockers and every diff."""
    from okuro.ai_models import swap

    _emit(swap.show(plan), as_json)


@models_swap.command("tick")
@click.argument("plan")
@click.argument("item", type=int)
@click.option("--json", "as_json", is_flag=True, help="Output as JSON.")
def models_swap_tick(plan, item, as_json):
    """Tick checklist ITEM (1-based). The first tick opens `testing`."""
    from okuro.ai_models import swap

    _emit(swap.tick(plan, item), as_json, show_diff=False)


@models_swap.command("apply")
@click.argument("plan")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON.")
def models_swap_apply(plan, as_json):
    """Mark a plan applied and PRINT the patch. It edits no consumer file.

    Refused while any checklist item is unticked or any blocker is unresolved.
    A headroom warning is not a blocker (ruling 8) and never refuses.
    """
    import json as _json

    from okuro.ai_models import swap

    res = swap.apply(plan)
    if as_json:
        click.echo(_json.dumps(res, indent=2, default=str))
        return
    if not res.get("ok"):
        fail(res.get("reason") or "refused")
        if res.get("plan"):
            console.print("")
            _render_plan(res["plan"], show_diff=False)
        return
    ok(f"plan {res['plan']['plan_group']} marked applied — "
       f"okuro has NOT edited any file. Apply this by hand:")
    console.print(res["apply_by_hand"], highlight=False)


@models_swap.command("reject")
@click.argument("plan")
@click.option("--why", required=True, help="Why this swap is not happening.")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON.")
def models_swap_reject(plan, why, as_json):
    """Close a plan without applying it. The reason is the point."""
    from okuro.ai_models import swap

    _emit(swap.reject(plan, why), as_json, show_diff=False)
