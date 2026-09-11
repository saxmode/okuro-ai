import { useEffect } from "react";
import { useNavigate } from "react-router";
import { registerPageAction } from "@/lib/page-context";
import { startFlowDraw } from "@/lib/flow-stream";
import { queueSlideGen, getOpenDeckId } from "@/lib/slide-gen-queue";
import { api } from "@/lib/api";

/**
 * ChatCapabilities — registers the GLOBAL chat actions that work from any
 * view, so the pulse chat always knows okuro can do them (context tier 3:
 * available capabilities, independent of the current page).
 *
 * - navigate: jump to any okuro view by route.
 * - draw: turn a description into an okuro·flow diagram (creates/edits it
 *   server-side, then opens /flow on the result). Works from anywhere — the
 *   chat no longer needs to be ON the flow page to draw.
 *
 * Rendered once inside AppShell (within the Router) so useNavigate is valid.
 */
export function ChatCapabilities() {
  const navigate = useNavigate();

  useEffect(() => {
    const unNav = registerPageAction(
      "navigate",
      "go to an okuro view by route, e.g. /flow, /brain, /cortex, /agents, /work",
      (route) => {
        const r = route.trim();
        navigate(r.startsWith("/") ? r : `/${r}`);
        return true;
      },
    );

    const unDraw = registerPageAction(
      "draw",
      "draw an okuro·flow diagram / flow chart from a natural-language " +
        "description; opens the flow view and renders it live, node by node",
      (instruction, _onActivity, params) => {
        let prompt = instruction.trim();
        const arr = params?.arrangement as string | undefined;
        if (arr === "vertical") prompt += " Lay the flow out top-to-bottom (vertical).";
        else if (arr === "horizontal") prompt += " Lay the flow out left-to-right (horizontal).";
        startFlowDraw(prompt, navigate);
        return true;
      },
      {
        contextKey: "description",
        title: "Draw a flow",
        submitLabel: "Draw",
        params: [
          { key: "description", label: "What to draw", kind: "text", placeholder: "describe the flow…" },
          { key: "arrangement", label: "Layout", kind: "segmented", default: "horizontal", choices: [
            { value: "horizontal", label: "Horizontal" }, { value: "vertical", label: "Vertical" },
          ] },
        ],
      },
    );

    const unSlides = registerPageAction(
      "slides",
      "build a recipient-tailored okuro·slides deck from a topic (optionally name a " +
        "recipient); opens /slides and generates it",
      (instruction, _onActivity, params) => {
        let mode = (params?.mode as "fast" | "quality") || "fast";
        let topic = ((params?.topic as string) || instruction).trim();
        // back-compat: tolerate a "<mode> :: <topic>" prefix
        const m = topic.match(/^\s*(fast|quality)\s*::\s*([\s\S]+)$/i);
        if (m) { mode = m[1]!.toLowerCase() as "fast" | "quality"; topic = m[2]!.trim(); }
        const recipient = (params?.recipient as string) || undefined;
        const brand = (params?.brand as string) || undefined;
        queueSlideGen(topic, recipient, mode, brand);
        if (window.location.pathname.startsWith("/slides")) {
          window.dispatchEvent(new CustomEvent("okuro:slides-generate", { detail: { topic, mode, recipient, brand } }));
        } else {
          navigate("/slides");
        }
        return true;
      },
      {
        contextKey: "topic",
        title: "Build a deck",
        submitLabel: "Generate",
        params: [
          { key: "topic", label: "Topic", kind: "text", placeholder: "what's the deck about?", required: true },
          { key: "mode", label: "Mode", kind: "segmented", default: "fast", choices: [
            { value: "fast", label: "Fast (~10s)" }, { value: "quality", label: "Quality (~60s)" },
          ] },
          { key: "recipient", label: "Recipient", kind: "person" },
          { key: "brand", label: "Brand", kind: "brand" },
        ],
      },
    );

    const unSlidesEdit = registerPageAction(
      "slides_edit",
      "edit or review the currently-open okuro·slides deck — add/remove/rewrite " +
        "elements or slides, restyle, or improve it. Describe the change.",
      (instruction, onActivity) => {
        const deckId = getOpenDeckId();
        if (!deckId) {
          onActivity?.("no deck open — opening slides");
          navigate("/slides");
          return false;
        }
        onActivity?.("editing the open deck…");
        void api("/api/slides/edit", { method: "POST", body: JSON.stringify({ deck_id: deckId, instruction }) })
          .then(() => {
            onActivity?.("applied — refreshing canvas");
            window.dispatchEvent(new CustomEvent("okuro:slides-edited", { detail: { deckId } }));
            if (!window.location.pathname.startsWith("/slides")) navigate("/slides");
          })
          .catch((e) => onActivity?.(`edit failed: ${(e as Error).message}`));
        return true;
      },
    );

    return () => {
      unNav();
      unDraw();
      unSlides();
      unSlidesEdit();
    };
  }, [navigate]);

  return null;
}
