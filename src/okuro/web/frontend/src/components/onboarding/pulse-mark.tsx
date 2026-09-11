import { useEffect, useRef } from "react";
import { PulseEngine } from "@/lib/pulse-engine";

/**
 * Brand pulse — SIMPLEX mode, no chrome. Fills its parent; the parent
 * controls size. That lets an outer wrapper animate width/height and the
 * engine's ResizeObserver redraws at whatever final dimensions settle.
 *
 * The wordmark (`okuro`) is rendered by the shell for the compact header
 * treatment. The splash screen renders its own larger h1 separately.
 */
export function OnboardingPulseMark({ className = "" }: { className?: string }) {
  const containerRef = useRef<HTMLDivElement>(null);
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const engineRef = useRef<PulseEngine | null>(null);

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const engine = new PulseEngine({ canvas, transparent: true });
    engine.setMode(1); // SIMPLEX
    engine.sizeImmediate();
    engine.start();
    engine.updateActivity({ calls: 0, live: 1, rate: 0, intensity: 0.55 });
    engineRef.current = engine;
    return () => {
      engine.destroy();
      engineRef.current = null;
    };
  }, []);

  useEffect(() => {
    const container = containerRef.current;
    if (!container) return;
    const ro = new ResizeObserver(() => {
      engineRef.current?.resize();
    });
    ro.observe(container);
    return () => ro.disconnect();
  }, []);

  return (
    <div
      ref={containerRef}
      className={`relative h-full w-full overflow-hidden rounded-full bg-transparent ${className}`}
    >
      <canvas ref={canvasRef} className="absolute inset-0 block" />
    </div>
  );
}
