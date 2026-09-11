import type { Deck } from "./scene";
import { deckSize, imageAlt } from "./scene";
import { elementStyle } from "./slides-editor";
import { flattenElements } from "./flatten";
import { renderRich } from "./rich-text";
import { tokenizeApiSrc } from "@/lib/slides-api";

/**
 * SlideThumb — a static, non-interactive mini render of one slide for the
 * thumbnail rail. Reuses the editor's element styling at a small scale.
 */
export function SlideThumb({ deck, slideIndex, width = 150 }: { deck: Deck; slideIndex: number; width?: number }) {
  const { w: canvasW, h: canvasH } = deckSize(deck);
  const scale = width / canvasW;
  const slide = deck.slides[slideIndex];
  return (
    <div
      className="relative overflow-hidden rounded"
      style={{ width, height: canvasH * scale, background: deck.background ?? "#0b0f0c" }}
    >
      <div
        className="absolute left-0 top-0 origin-top-left"
        style={{ width: canvasW, height: canvasH, transform: `scale(${scale})`, fontFamily: deck.font }}
      >
        {flattenElements(slide?.elements ?? [], deck.font).map((el) => (
          <div
            key={el.id}
            className="absolute"
            style={{ left: el.x, top: el.y, width: el.w, height: el.h, zIndex: el.z ?? 0, ...elementStyle(el, false) }}
          >
            {el.kind === "image" && el.src ? (
              <img src={tokenizeApiSrc(el.src)} alt={imageAlt(el)} className="h-full w-full object-cover" draggable={false} />
            ) : el.kind === "video" && el.src ? (
              <video src={tokenizeApiSrc(el.src)} muted className="h-full w-full object-cover" />
            ) : el.kind === "flow" ? (
              <div className="flex h-full w-full items-center justify-center text-accent">⚙ flow</div>
            ) : (
              renderRich(el.text)
            )}
          </div>
        ))}
      </div>
    </div>
  );
}
