"use client";

import { create } from "@bufbuild/protobuf";
import { ArrowUp } from "lucide-react";
import { useRouter } from "next/navigation";
import { useState } from "react";
import { Eyebrow } from "@/components/eyebrow";
import { useCreateAnalysis } from "@/lib/queries";
import { errorMessage } from "@/lib/rpc";
import { splitVariant } from "@/lib/scenario";
import { cn } from "@/lib/utils";
import { type AnalysisInputs, AnalysisInputsSchema } from "@/models/workbench";

// The one client island on the Project page: state a scenario's inputs and go to the Analysis they
// start. The created Analysis is the composer's only outcome, so this never renders a run in
// progress. Each scenario contributes its own form and its own `AnalysisInputs`; the agent's kickoff
// text is rendered server-side from those (docs/design/analysis-scenarios.md).

type Scenario = "variantClassification" | "freeForm";

const SCENARIOS: ReadonlyArray<{ value: Scenario; label: string }> = [
  { value: "variantClassification", label: "Variant classification" },
  { value: "freeForm", label: "Free-form" },
];

export function Composer({ projectId }: { projectId: string }) {
  const [scenario, setScenario] = useState<Scenario>("variantClassification");
  const [variant, setVariant] = useState("");
  const [clinicalContext, setClinicalContext] = useState("");
  const [prompt, setPrompt] = useState("");
  // Latched rather than `isPending`: the mutation settles when the id comes back, but the navigation
  // it triggers is still in flight, and a second create would start a second Analysis.
  const [submitted, setSubmitted] = useState(false);
  const createAnalysis = useCreateAnalysis();
  const router = useRouter();

  const inputs = buildInputs(scenario, variant, clinicalContext, prompt);

  const submit = () => {
    if (inputs === null || submitted) return;
    setSubmitted(true);
    createAnalysis.mutate(
      { projectId, inputs },
      {
        onSuccess: (res) => {
          // Invalidates the client router cache, so this Project's list is re-read when the
          // curator comes back to it rather than served without the Analysis they just created.
          router.refresh();
          router.push(`/analysis/${res.id}`);
        },
        onError: () => setSubmitted(false),
      },
    );
  };

  const onKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) submit();
  };

  return (
    <section className="flex flex-col gap-[14px] rounded-card border border-line-primary bg-white px-[20px] py-[18px]">
      <div className="flex items-center gap-[14px]">
        <Eyebrow className="text-[10px]">New analysis</Eyebrow>
        <div className="flex items-center gap-[2px] rounded-field border border-line-primary p-[2px]">
          {SCENARIOS.map(({ value, label }) => (
            <button
              key={value}
              type="button"
              aria-pressed={scenario === value}
              onClick={() => setScenario(value)}
              className={cn(
                "h-[26px] rounded-[5px] px-[11px] text-[12px] font-medium",
                scenario === value
                  ? "bg-primary text-primary-foreground"
                  : "text-ink-muted hover:bg-surface-idle hover:text-ink-primary",
              )}
            >
              {label}
            </button>
          ))}
        </div>
      </div>

      {scenario === "variantClassification" ? (
        <div className="flex flex-col gap-[12px]">
          <Field
            htmlFor="variant"
            label="Variant"
            hint="transcript and coding change"
          >
            <input
              id="variant"
              value={variant}
              onChange={(e) => setVariant(e.target.value)}
              onKeyDown={onKeyDown}
              placeholder="NM_001382309.1:c.332del"
              className="h-[40px] w-full rounded-card border border-line-input bg-white px-[14px] font-mono text-[13px] text-ink-body outline-none placeholder:font-mono placeholder:text-ink-faintest focus:shadow-focus-ring"
            />
          </Field>
          <Field
            htmlFor="clinical-context"
            label="Clinical context"
            hint="phenotype, inheritance, what else was excluded"
          >
            <textarea
              id="clinical-context"
              value={clinicalContext}
              onChange={(e) => setClinicalContext(e.target.value)}
              onKeyDown={onKeyDown}
              rows={5}
              // Composed for this placeholder — no case, record, or participant is behind it.
              placeholder="Proband with global developmental delay and hypotonia; no other candidate variant; de novo, parental relationships confirmed."
              className="tscroll min-h-[110px] w-full resize-y rounded-card border border-line-input bg-white px-[14px] py-[10px] text-[13.5px] leading-[1.55] text-ink-body outline-none placeholder:text-ink-faintest focus:shadow-focus-ring"
            />
          </Field>
        </div>
      ) : (
        <textarea
          value={prompt}
          onChange={(e) => setPrompt(e.target.value)}
          onKeyDown={onKeyDown}
          rows={7}
          placeholder="Describe the task to run…"
          aria-label="Analysis prompt"
          className="tscroll min-h-[150px] w-full resize-y rounded-card border border-line-input bg-white px-[14px] py-[10px] text-[13.5px] leading-[1.55] text-ink-body outline-none placeholder:text-ink-faintest focus:shadow-focus-ring"
        />
      )}

      <div className="flex items-center justify-end gap-[14px]">
        <span className="text-[11.5px] text-ink-faintest">⌘↵ to create</span>
        <button
          type="button"
          onClick={submit}
          disabled={inputs === null || submitted}
          className="flex h-[40px] shrink-0 items-center gap-[7px] rounded-field bg-primary px-[18px] text-[13.5px] font-semibold text-primary-foreground shadow-[0_1px_2px_rgba(0,0,0,0.06)] disabled:opacity-50"
        >
          <ArrowUp className="size-[16px]" strokeWidth={2.4} aria-hidden />
          Create
        </button>
      </div>

      {createAnalysis.isError && (
        <p role="alert" className="text-[12.5px] text-error-text">
          Could not create the analysis: {errorMessage(createAnalysis.error)}
        </p>
      )}
    </section>
  );
}

// The boundary's own bounds (workbench.proto). Mirrored so an over-long field disables Create
// rather than being sent and refused.
const MAX_PROSE = 10000;
const MAX_IDENTIFIER = 255;

/** The scenario's inputs, or null while they are incomplete or over-long — which is what disables
 *  Create, so a request the boundary would reject is never sent. */
function buildInputs(
  scenario: Scenario,
  variant: string,
  clinicalContext: string,
  prompt: string,
): AnalysisInputs | null {
  if (scenario === "freeForm") {
    return prompt.trim().length === 0 || prompt.trim().length > MAX_PROSE
      ? null
      : create(AnalysisInputsSchema, {
          scenario: { case: "freeForm", value: { prompt: prompt.trim() } },
        });
  }
  const { transcript, hgvsC } = splitVariant(variant);
  const context = clinicalContext.trim();
  if (!transcript || !hgvsC || context.length === 0) return null;
  if (
    transcript.length > MAX_IDENTIFIER ||
    hgvsC.length > MAX_IDENTIFIER ||
    context.length > MAX_PROSE
  ) {
    return null;
  }
  return create(AnalysisInputsSchema, {
    scenario: {
      case: "variantClassification",
      value: { transcript, hgvsC, clinicalContext: context },
    },
  });
}

function Field({
  htmlFor,
  label,
  hint,
  children,
}: {
  htmlFor: string;
  label: string;
  hint: string;
  children: React.ReactNode;
}) {
  return (
    <div className="flex flex-col gap-[6px]">
      <label
        htmlFor={htmlFor}
        className="flex items-baseline gap-[8px] text-[12.5px] font-medium text-ink-label"
      >
        {label}
        <span className="font-normal text-[11.5px] text-ink-faintest">
          {hint}
        </span>
      </label>
      {children}
    </div>
  );
}
