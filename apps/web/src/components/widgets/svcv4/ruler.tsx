"use client";

import { useId, useRef } from "react";
import { type Anchor, TooltipPanel, useTooltip } from "@/components/ui/tooltip";
import type { Svcv4Classification } from "@/models/widgets";
import {
  bandAt,
  bandBounds,
  bandSpan,
  classLabel,
  domain,
  type Needle,
  needleLabels,
  needles,
  needlesAt,
  plain,
  position,
  type Segment,
  segmentAt,
  signedNumber,
  stacks,
} from "./model";
import { toneText } from "./sections";
import { useBox } from "./use-box";

// The points ruler: the class bands along one points axis, and below them the codes the tally
// counts, stacked outward from zero, benign to the left and pathogenic to the right, each named
// beneath its segment where there is room. A cap's cut shows hatched over the end of the stack it
// took points from. The tally is the one needle crossing the bands; each alternative the payload
// carries is a lettered marker in a lane above them, so no marker hides behind the tally or a band's
// name. A pointer anywhere over the lane or the bands reads the nearest total and everything at it,
// and a guide drops from that marker to the axis; over the stack it reads the segment under it or
// nearest, and lights that code's row.

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

// How far from a needle, in pixels, a pointer still reads it.
const SNAP_PX = 12;

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

/** What the pointer over the lane and bands is reading: the needles at one total, or a band. */
type Reading =
  | { kind: "needles"; at: number; needles: Needle[] }
  | { kind: "band"; at: number; band: Svcv4Classification["bands"][number] };

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
  const reader = useRef<HTMLDivElement>(null);
  const ruler = useBox(figure);
  const type = useBox(probe);
  const readoutId = useId();
  const segmentReadoutId = useId();
  const stack = useRef<HTMLDivElement>(null);
  const tooltip = useTooltip<Reading>(sameReading);
  const segmentTip = useTooltip<Placed>((a, b) => a === b);
  // The code the segment row lit, which leaving the row unlights; the readout can close on its own
  // (Escape, a scroll), so its state does not say what the row lit.
  const lit = useRef<string | null>(null);
  const reading = tooltip.open?.value ?? null;
  const span = domain(payload);
  const all = needles(payload);
  const [tally, ...alternatives] = all;
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
  const active = new Set(
    reading?.kind === "needles"
      ? reading.needles.map((needle) => needle.mark)
      : [],
  );

  const read = (clientX: number) => {
    const area = reader.current;
    if (area === null) return;
    const box = area.getBoundingClientRect();
    if (box.width === 0) return;
    const share = ((clientX - box.left) / box.width) * 100;
    const reach = (SNAP_PX / box.width) * 100;
    const found = needlesAt(all, span, share, reach);
    let next: Reading | null = null;
    if (found.length > 0) {
      next = {
        kind: "needles",
        at: position(found[0].total, span),
        needles: found,
      };
    } else {
      const total = span[0] + (share / 100) * (span[1] - span[0]);
      const band = bandAt(payload, total);
      if (band !== undefined) {
        // Anchored at the band's middle, so a pointer moving within one band keeps one readout.
        const [lower, upper] = bandSpan(band, span);
        next = {
          kind: "band",
          at: (position(lower, span) + position(upper, span)) / 2,
          band,
        };
      }
    }
    if (next === null) {
      tooltip.hide();
      return;
    }
    const x = box.left + (next.at / 100) * box.width;
    const anchor: Anchor = {
      left: x,
      right: x,
      top: box.top,
      bottom: box.bottom,
    };
    tooltip.show(anchor, next);
  };

  // The segment row reads like the lane: the segment under the pointer, else the nearest within
  // reach, so a code a few pixels wide can still be pointed at, and the row it names lights up.
  const readSegment = (clientX: number) => {
    const area = stack.current;
    if (area === null) return;
    const box = area.getBoundingClientRect();
    if (box.width === 0) return;
    const share = ((clientX - box.left) / box.width) * 100;
    const found = segmentAt(placed, share, (SNAP_PX / box.width) * 100);
    const code = found?.segment.kind === "code" ? found.segment.label : null;
    if (code !== highlighted) {
      onHighlight(code);
      lit.current = code;
    }
    if (found === undefined) {
      segmentTip.hide();
      return;
    }
    const x = box.left + ((found.left + found.share / 2) / 100) * box.width;
    segmentTip.show(
      { left: x, right: x, top: box.top, bottom: box.bottom },
      found,
    );
  };

  return (
    <figure
      ref={figure}
      data-section="ruler"
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
      <div className="relative">
        {/* The pointer reads totals off the lane and bands; the table below the codes holds every
            value for keyboard and screen-reader users. */}
        <div
          ref={reader}
          className="relative"
          onPointerMove={(event) => {
            if (event.pointerType !== "touch") read(event.clientX);
          }}
          onPointerLeave={() => {
            tooltip.release();
            tooltip.hide();
          }}
        >
          {alternatives.length > 0 && (
            <div aria-hidden className="relative h-[21px]">
              {labels.map((label) => (
                <span
                  key={label.marks.join(" ")}
                  className="absolute top-0 -translate-x-1/2 whitespace-nowrap font-mono text-[10px] leading-[11px] text-ink-muted"
                  style={{ left: `${label.at}%` }}
                >
                  {label.marks.join(" ")}
                </span>
              ))}
              {alternatives.map((needle) => (
                <Marker
                  key={needle.mark}
                  needle={needle}
                  active={active.has(needle.mark)}
                  left={at(needle.total)}
                />
              ))}
            </div>
          )}
          <div className="relative h-[22px] overflow-hidden rounded-[5px]">
            {payload.bands.map((band, index) => {
              const [lower, upper] = bandSpan(band, span);
              if (upper <= lower) return null;
              return (
                <div
                  // biome-ignore lint/suspicious/noArrayIndexKey: bands are drawn in the payload's order, and a name may repeat
                  key={index}
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
        </div>
        {/* The table below the codes holds each code's points for keyboard and screen-reader users;
            focusing a code's row lights its segment here. */}
        <div
          ref={stack}
          className="relative mt-[5px] h-[14px]"
          onPointerMove={(event) => {
            if (event.pointerType !== "touch") readSegment(event.clientX);
          }}
          onPointerLeave={() => {
            if (lit.current !== null) {
              onHighlight(null);
              lit.current = null;
            }
            segmentTip.release();
            segmentTip.hide();
          }}
        >
          {placed.map(({ segment, shade, left, share }, index) => (
            <SegmentMark
              // biome-ignore lint/suspicious/noArrayIndexKey: segments are the stacks in order, and a label may repeat
              key={index}
              segment={segment}
              shade={shade}
              left={`${left}%`}
              width={`${share}%`}
              highlighted={highlighted === segment.label}
            />
          ))}
          <div
            className="pointer-events-none absolute inset-y-[-3px] w-px bg-ink-faint"
            style={{ left: at(0) }}
          />
        </div>
        <div
          className={`pointer-events-none absolute bottom-[-5px] w-[2px] -translate-x-1/2 rounded-full bg-ink-primary ${alternatives.length > 0 ? "top-[21px]" : "top-[-5px]"}`}
          style={{ left: at(tally.total) }}
        />
        {reading?.kind === "needles" &&
          reading.needles.some((needle) => needle.kind !== "tally") && (
            <div
              aria-hidden
              className="pointer-events-none absolute top-[12px] bottom-[-5px] w-0 -translate-x-1/2 border-l border-dashed border-ink-muted"
              style={{ left: `${reading.at}%` }}
            />
          )}
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
          Lettered markers are the tally under{" "}
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
            ", filled where the input is open"}
          .<span aria-hidden> Point at the ruler to read one.</span>
        </figcaption>
      )}
      <ul className="sr-only">
        {payload.bands.map((band) => (
          <li key={`${band.name} ${bandBounds(band)}`}>
            {band.name}: {bandBounds(band)}
          </li>
        ))}
      </ul>
      {segmentTip.open !== null && (
        <TooltipPanel
          id={segmentReadoutId}
          anchor={segmentTip.open.anchor}
          placement="below"
          onPointerEnter={segmentTip.hold}
          onPointerLeave={segmentTip.hide}
        >
          {segmentText(segmentTip.open.value.segment)}
        </TooltipPanel>
      )}
      {tooltip.open !== null && reading !== null && (
        <TooltipPanel
          id={readoutId}
          anchor={tooltip.open.anchor}
          onPointerEnter={tooltip.hold}
          onPointerLeave={tooltip.hide}
        >
          <Readout reading={reading} />
        </TooltipPanel>
      )}
    </figure>
  );
}

/** An alternative's marker in the lane: filled for an open value, hollow for a sensitivity row. */
function Marker({
  needle,
  active,
  left,
}: {
  needle: Needle;
  active: boolean;
  left: string;
}): React.ReactElement {
  const filled = needle.kind === "open";
  return (
    <svg
      aria-hidden="true"
      width="9"
      height="7"
      viewBox="0 0 9 7"
      className="absolute bottom-[1px] -translate-x-1/2 overflow-visible"
      style={{ left }}
    >
      <path
        d="M0.75 0.75 H8.25 L4.5 6.25 Z"
        strokeWidth={active ? 1.6 : 1.1}
        strokeLinejoin="round"
        className={`${filled ? "fill-ink-label" : "fill-white"} ${active ? "stroke-ink-primary" : filled ? "stroke-ink-label" : "stroke-ink-muted"}`}
      />
    </svg>
  );
}

/** Whether two readings say the same thing, so a pointer moving within one keeps its readout. */
function sameReading(a: Reading, b: Reading): boolean {
  if (a.kind === "band" || b.kind === "band") {
    return a.kind === b.kind && a.at === b.at;
  }
  // What the readout prints of each needle, so new text at the same place is read anew.
  const printed = (needle: Needle) =>
    [needle.mark, needle.total, needle.classification, needle.assumption].join(
      "\u0000",
    );
  return (
    a.at === b.at &&
    a.needles.map(printed).join("\u0001") ===
      b.needles.map(printed).join("\u0001")
  );
}

/** The tooltip's text for what the pointer reads: each needle at the total, value first. */
function Readout({ reading }: { reading: Reading }): React.ReactElement {
  if (reading.kind === "band") {
    return (
      <span>
        <span className="font-mono font-medium text-ink-primary">
          {reading.band.name}
        </span>{" "}
        <span className="text-ink-muted">{bandBounds(reading.band)}</span>
      </span>
    );
  }
  return (
    <ul className="m-0 flex list-none flex-col gap-[4px] p-0">
      {reading.needles.map((needle) => {
        const label = classLabel(needle.classification);
        return (
          <li key={needle.mark === "" ? "tally" : needle.mark}>
            <span className="font-mono font-medium text-ink-primary">
              {signedNumber(needle.total)}
            </span>{" "}
            <span className={toneText(label.tone)}>{label.short}</span>{" "}
            <span className="font-mono text-ink-muted">
              {needle.kind === "tally" ? "tally" : needle.mark}
            </span>
            {needle.kind === "open" && (
              <span className="text-amber-uncertainty-heading"> open</span>
            )}
            {needle.assumption !== "" && (
              <div className="text-ink-body">{needle.assumption}</div>
            )}
          </li>
        );
      })}
    </ul>
  );
}

/** What the segment row's readout says of a segment. */
function segmentText(segment: Segment): string {
  const points = segment.to - segment.from;
  return segment.kind === "cut"
    ? `${segment.label}: ${signedNumber(-Math.abs(points))} not counted`
    : `${segment.label} ${signedNumber(points)}`;
}

function SegmentMark({
  segment,
  shade,
  left,
  width,
  highlighted,
}: {
  segment: Segment;
  shade: "base" | "alt";
  left: string;
  width: string;
  highlighted: boolean;
}): React.ReactElement {
  const points = segment.to - segment.from;
  if (segment.kind === "cut") {
    return (
      <div
        className="pointer-events-none absolute inset-y-0"
        style={{ left, width, backgroundImage: CUT_STRIPES }}
      />
    );
  }
  const side = points < 0 ? "benign" : "pathogenic";
  return (
    <div
      className={`pointer-events-none absolute inset-y-0 border-x border-white ${SHADES[side][shade]} ${highlighted ? "ring-2 ring-ink-primary ring-offset-1" : ""}`}
      style={{ left, width }}
    />
  );
}
