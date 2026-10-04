"use client";

import { useRef } from "react";
import type { Svcv4Classification } from "@/models/widgets";
import {
  bandBounds,
  bandSpan,
  classLabel,
  domain,
  needleLabels,
  needles,
  plain,
  position,
  type Segment,
  signedNumber,
  stacks,
} from "./model";
import { useBox } from "./use-box";

// The points ruler: the class bands along one points axis, and below them the codes the tally
// counts, stacked outward from zero, benign to the left and pathogenic to the right, each named
// beneath its segment where there is room. A cap's cut shows hatched over the end of the stack it
// took points from. A needle marks the tally's total, and each alternative the payload carries
// marks its own, so a reader sees how far each would move the variant and whether it crosses a
// band edge. Each alternative is lettered; the table below the codes says what each letter assumes.

const BAND_FILL: Record<string, string> = {
  B: "bg-svcv4-benign-band",
  LB: "bg-svcv4-benign-tint",
  "VUS-low": "bg-svcv4-vus-band",
  "VUS-mid": "bg-svcv4-vus-band-alt",
  "VUS-high": "bg-svcv4-vus-band",
  LP: "bg-svcv4-pathogenic-tint",
  P: "bg-svcv4-pathogenic-band",
};

/** A band this build has no fill for draws as the VUS neutral: the payload names its bands. */
function bandFill(name: string): string {
  return BAND_FILL[name] ?? "bg-svcv4-vus-band";
}

// Stripes of the surface over a segment: the points a cap took away.
const CUT_STRIPES =
  "repeating-linear-gradient(135deg, var(--color-svcv4-cut) 0 2px, transparent 2px 5px)";

// The room a label keeps clear beside it, in pixels. Until the ruler and a probe of its label type
// are measured, labels are laid out for a nominal width and advance.
const LABEL_PAD_PX = 4;
const NOMINAL_WIDTH_PX = 720;
const NOMINAL_CHAR_PX = 6;
const PROBE = "0000000000";

const SHADES = {
  benign: { base: "bg-svcv4-benign", alt: "bg-svcv4-benign-alt" },
  pathogenic: { base: "bg-svcv4-pathogenic", alt: "bg-svcv4-pathogenic-alt" },
};

interface Placed {
  segment: Segment;
  /** Neighbouring segments on one side alternate shades, so two codes side by side read as two. */
  shade: "base" | "alt";
  left: number;
  share: number;
}

export function Ruler({
  payload,
  highlighted,
  onHighlight,
  alternativesId,
}: {
  payload: Svcv4Classification;
  highlighted: string | null;
  onHighlight: (code: string | null) => void;
  /** The id of the table listing the alternatives, which the caption links to. */
  alternativesId: string;
}): React.ReactElement {
  const figure = useRef<HTMLElement>(null);
  const probe = useRef<HTMLSpanElement>(null);
  const ruler = useBox(figure);
  const type = useBox(probe);
  const span = domain(payload);
  const [tally, ...alternatives] = needles(payload);
  const edges = new Set<number>([0]);
  for (const band of payload.bands) {
    if (band.lower !== "") edges.add(Number(band.lower));
  }
  const at = (value: number) => `${position(value, span)}%`;
  const width = (from: number, to: number) =>
    `${Math.abs(position(to, span) - position(from, span))}%`;
  const count = { benign: 0, pathogenic: 0 };
  const placed: Placed[] = stacks(payload).map((segment) => {
    const side = segment.to - segment.from < 0 ? "benign" : "pathogenic";
    const shade = count[side] % 2 === 0 ? "base" : "alt";
    if (segment.kind === "code") count[side] += 1;
    return {
      segment,
      shade,
      left: position(Math.min(segment.from, segment.to), span),
      share: Math.abs(
        position(segment.to, span) - position(segment.from, span),
      ),
    };
  });
  // Label geometry in percent of the ruler, the unit everything on it is placed in.
  const pixelShare = 100 / (ruler?.width ?? NOMINAL_WIDTH_PX);
  const charPx = type === null ? NOMINAL_CHAR_PX : type.width / PROBE.length;
  const textShare = (text: string) => text.length * charPx * pixelShare;
  const named = placed.filter(
    ({ segment, share }) =>
      segment.kind === "code" &&
      share >= textShare(segment.label) + LABEL_PAD_PX * pixelShare,
  );
  const labels = needleLabels(
    alternatives.map((needle) => ({
      mark: needle.mark,
      at: position(needle.total, span),
    })),
    (marks) => textShare(marks.join(" ")),
    LABEL_PAD_PX * pixelShare,
    [0, 100],
  );
  return (
    <figure
      ref={figure}
      className="relative m-0"
      aria-label={`Points ruler: total ${signedNumber(tally.total)}, ${classLabel(tally.classification).label}`}
    >
      <span
        ref={probe}
        aria-hidden
        className="invisible absolute top-0 left-0 whitespace-pre font-mono text-[10px]"
      >
        {PROBE}
      </span>
      <div
        className={alternatives.length > 0 ? "relative mt-[14px]" : "relative"}
      >
        <div className="relative h-[22px] overflow-hidden rounded-[5px]">
          {payload.bands.map((band, index) => {
            const [lower, upper] = bandSpan(band, span);
            if (upper <= lower) return null;
            return (
              <div
                // biome-ignore lint/suspicious/noArrayIndexKey: bands are drawn in the payload's order, and a name may repeat
                key={index}
                title={`${band.name}: ${bandBounds(band)}`}
                className={`absolute inset-y-0 flex items-center justify-center overflow-hidden border-l border-white first:border-l-0 ${bandFill(band.name)}`}
                style={{ left: at(lower), width: width(lower, upper) }}
              >
                <span className="truncate px-[3px] font-mono text-[10.5px] text-ink-muted">
                  {band.name}
                </span>
              </div>
            );
          })}
        </div>
        <div className="relative mt-[5px] h-[14px]">
          {placed.map(({ segment, shade, left, share }, index) => (
            <SegmentMark
              // biome-ignore lint/suspicious/noArrayIndexKey: segments are the stacks in order, and a label may repeat
              key={index}
              segment={segment}
              shade={shade}
              left={`${left}%`}
              width={`${share}%`}
              highlighted={highlighted === segment.label}
              onHighlight={onHighlight}
            />
          ))}
        </div>
        <div
          className="pointer-events-none absolute top-[24px] bottom-[-3px] w-px bg-ink-faint"
          style={{ left: at(0) }}
        />
        {alternatives.map((needle) => (
          <div
            key={needle.mark}
            title={`${needle.mark}: ${needle.assumption}: ${signedNumber(needle.total)}, ${classLabel(needle.classification).label}`}
            className={`absolute inset-y-[-3px] -translate-x-1/2 ${needle.kind === "open" ? "w-[2px] rounded-full bg-ink-label" : "w-0 border-l border-dashed border-ink-faint"}`}
            style={{ left: at(needle.total) }}
          />
        ))}
        {labels.map((label) => (
          <span
            key={label.marks.join(" ")}
            aria-hidden
            className="absolute bottom-full mb-[5px] -translate-x-1/2 whitespace-nowrap font-mono text-[10px] leading-none text-ink-muted"
            style={{ left: `${label.at}%` }}
          >
            {label.marks.join(" ")}
          </span>
        ))}
        <div
          className="absolute inset-y-[-5px] w-[2px] -translate-x-1/2 rounded-full bg-ink-primary"
          style={{ left: at(tally.total) }}
        />
      </div>
      {/* Drawn wherever a code is stacked, so measuring the ruler never adds or drops the row. */}
      {placed.some(({ segment }) => segment.kind === "code") && (
        <div aria-hidden className="relative mt-[2px] h-[13px]">
          {named.map(({ segment, left, share }) => (
            <span
              key={segment.label}
              className="absolute truncate text-center font-mono text-[10px] leading-[13px] text-ink-muted"
              style={{ left: `${left}%`, width: `${share}%` }}
            >
              {segment.label}
            </span>
          ))}
        </div>
      )}
      <div className="relative mt-[2px] h-[14px] font-mono text-[10px] text-ink-faint">
        {[...edges]
          .sort((a, b) => a - b)
          .map((edge) => (
            <span
              key={edge}
              className="absolute -translate-x-1/2"
              style={{ left: at(edge) }}
            >
              {plain(edge)}
            </span>
          ))}
      </div>
      {alternatives.length > 0 && (
        <figcaption className="mt-[4px] text-[12px] text-ink-muted">
          Lettered needles mark the tally under{" "}
          <a
            href={`#${alternativesId}`}
            // Scrolled to in place, so a jump down the document leaves no entry in the history.
            onClick={(event) => {
              const target = document.getElementById(alternativesId);
              if (target === null) return;
              event.preventDefault();
              target.scrollIntoView({ block: "start" });
            }}
            className="underline decoration-line-input underline-offset-2 hover:text-ink-primary"
          >
            other values of the judgement inputs
          </a>
          {alternatives.some((needle) => needle.kind === "open") &&
            ", solid where the input is open"}
          .
        </figcaption>
      )}
    </figure>
  );
}

function SegmentMark({
  segment,
  shade,
  left,
  width,
  highlighted,
  onHighlight,
}: {
  segment: Segment;
  shade: "base" | "alt";
  left: string;
  width: string;
  highlighted: boolean;
  onHighlight: (code: string | null) => void;
}): React.ReactElement {
  const points = segment.to - segment.from;
  if (segment.kind === "cut") {
    return (
      <div
        title={`${segment.label}: ${signedNumber(-Math.abs(points))} not counted`}
        className="absolute inset-y-0"
        style={{ left, width, backgroundImage: CUT_STRIPES }}
      />
    );
  }
  const side = points < 0 ? "benign" : "pathogenic";
  return (
    // biome-ignore lint/a11y/noStaticElementInteractions: hovering a segment highlights its row; focusing the row's button highlights the segment for the keyboard
    <div
      title={`${segment.label} ${signedNumber(points)}`}
      onMouseEnter={() => onHighlight(segment.label)}
      onMouseLeave={() => onHighlight(null)}
      className={`absolute inset-y-0 border-x border-white ${SHADES[side][shade]} ${highlighted ? "ring-2 ring-ink-primary ring-offset-1" : ""}`}
      style={{ left, width }}
    />
  );
}
