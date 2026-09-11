import { ChevronRight } from "lucide-react";
import { useOnboardingPage } from "./shell";

/**
 * Splash *content* — rendered inside page 0 of the shell. The big pulse is
 * already present (owned by the shell as a shared element), so this component
 * only provides the h1 "okuro", the one-line description, and the Start
 * button. Clicking Start advances to page 1 via the shell's page context,
 * which also morphs the pulse into its header pose.
 */
export function OnboardingSplashContent({
  description = "Personal AI agent infrastructure — wires your AI CLIs into a shared memory, tool, and context layer.",
}: {
  description?: string;
}) {
  const { next } = useOnboardingPage();

  return (
    <div className="flex flex-col items-center text-center">
      <h1 className="text-4xl font-light tracking-[0.45em] uppercase text-fg-primary select-none">
        okuro
      </h1>

      <p className="mt-4 max-w-md text-sm text-fg-tertiary leading-relaxed">
        {description}
      </p>

      <button
        type="button"
        onClick={next}
        className="mt-10 flex items-center gap-2 rounded bg-accent px-6 py-3 text-sm font-medium text-fg-inverse hover:bg-accent-hover focus:outline-none focus-visible:ring-2 focus-visible:ring-accent/50 transition-colors"
      >
        Start onboarding
        <ChevronRight size={16} />
      </button>
    </div>
  );
}
