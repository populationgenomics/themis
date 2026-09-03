"use client";

import { useMemo, useState } from "react";
import type { ResolvedAllele } from "../resolver";
import {
  VARIANT_STATUS_LABELS,
  VARIANT_STATUS_ORDER,
  type VariantProgress,
  type VariantStatus,
  variantProgress,
} from "../status";
import {
  type SortBy,
  SortControl,
  StatusFilter,
  VariantStatusTag,
} from "./status-tag";

// The manager's assignment surface. It shows progress, never answers: a manager who is themselves
// assigned to a variant must not read its other worksheets, and the simplest way to hold that is
// for this screen never to fetch one.

export interface VariantRow {
  id: string;
  gene: string;
  transcript: string;
  hgvsC: string;
  diseaseLabel: string;
  rows: {
    curatorEmail: string;
    draftCount: number;
    submittedAt: string | null;
  }[];
}

/** The identity fields, held here rather than left to the DOM so a retrieval can fill them. The
 *  disease entity is not among them: the registry does not state it. */
interface FormIdentity {
  clingenAlleleId: string;
  gene: string;
  transcript: string;
  hgvsC: string;
}

const BLANK_IDENTITY: FormIdentity = {
  clingenAlleleId: "",
  gene: "",
  transcript: "",
  hgvsC: "",
};

/** Register a variant to curate.
 *
 *  Identity may be retrieved from a ClinGen allele id or typed. Retrieved is a separate step ending
 *  in the manager confirming what came back, not a resolution folded into the submit: both curators
 *  of this variant answer whichever identity is registered, so a mistyped id registering a different
 *  variant unseen is the most expensive error this screen can make. */
export function NewVariantForm() {
  const [open, setOpen] = useState(false);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [retrieving, setRetrieving] = useState(false);
  const [resolved, setResolved] = useState<ResolvedAllele | null>(null);
  const [identity, setIdentity] = useState<FormIdentity>(BLANK_IDENTITY);

  async function retrieve(caid: string): Promise<void> {
    setRetrieving(true);
    setError("");
    setResolved(null);
    // `finally`, or a rejected fetch — offline, DNS, a reset connection — leaves the button reading
    // "Retrieving…" and disabled for the life of the page, with nothing on screen saying why.
    try {
      const res = await fetch(
        `/api/curation/alleles/${encodeURIComponent(caid.trim())}`,
      );
      if (!res.ok) {
        const body = (await res.json().catch(() => null)) as {
          error?: { message?: string };
        } | null;
        setError(body?.error?.message ?? `Could not retrieve ${caid}.`);
        return;
      }
      const allele = (await res.json()) as ResolvedAllele;
      setResolved(allele);
      setIdentity({
        clingenAlleleId: allele.clingenAlleleId,
        gene: allele.gene,
        transcript: allele.transcript,
        hgvsC: allele.hgvsC,
      });
    } catch {
      setError(
        `Could not reach the registry to retrieve ${caid}. Try again, or type the identity below.`,
      );
    } finally {
      setRetrieving(false);
    }
  }

  /** Edit an identity field. Any edit drops what the registry answered: the panel is there for the
   *  manager to confirm the fields against, and one still asserting the registry's values beside fields
   *  that now say something else is the opposite of a confirmation. */
  function editIdentity(next: Partial<FormIdentity>): void {
    setResolved(null);
    setIdentity({ ...identity, ...next });
  }
  if (!open) {
    return (
      <button
        type="button"
        onClick={() => setOpen(true)}
        className="framework-voice rounded-sm border border-line-input bg-white px-3 py-1.5 text-[13px] text-ink-body hover:border-ink-ghost"
      >
        Add a variant
      </button>
    );
  }
  return (
    <form
      className="rounded-md border border-line-primary bg-white p-5"
      onSubmit={async (e) => {
        e.preventDefault();
        setBusy(true);
        setError("");
        const data = new FormData(e.currentTarget);
        try {
          const res = await fetch("/api/curation/variants", {
            method: "POST",
            headers: { "content-type": "application/json" },
            body: JSON.stringify(Object.fromEntries(data.entries())),
          });
          if (res.ok) {
            window.location.reload();
            return;
          }
          const body = (await res.json().catch(() => null)) as {
            error?: { message?: string };
          } | null;
          setError(body?.error?.message ?? "Could not add the variant.");
        } catch {
          setError("Could not reach the server to add the variant. Try again.");
        }
        // Not a `finally`: the reload path leaves with the button still disabled, so nothing invites
        // a second add while the page is on its way out.
        setBusy(false);
      }}
    >
      <h2 className="framework-voice mb-1 font-medium text-[15px] text-ink-primary">
        Add a variant
      </h2>
      <p className="framework-voice mb-4 text-[13px] text-ink-muted">
        One variant against one disease entity. Both curators of it answer this
        same question.
      </p>
      <div className="mb-4 rounded-sm border border-line-row bg-surface-warm-panel p-3">
        <div className="flex flex-wrap items-end gap-2">
          <div className="min-w-48 flex-1">
            <Field
              name="clingenAlleleId"
              label="ClinGen Allele ID"
              placeholder="CA016924"
              value={identity.clingenAlleleId}
              onValueChange={(clingenAlleleId) =>
                editIdentity({ clingenAlleleId })
              }
            />
          </div>
          <button
            type="button"
            disabled={retrieving || identity.clingenAlleleId.trim() === ""}
            onClick={() => void retrieve(identity.clingenAlleleId)}
            className="framework-voice rounded-sm border border-line-input bg-white px-3 py-1.5 text-[13px] text-ink-body enabled:hover:border-ink-ghost disabled:opacity-40"
          >
            {retrieving ? "Retrieving…" : "Retrieve"}
          </button>
        </div>
        <p className="framework-voice mt-2 text-[12.5px] text-ink-faint">
          Optional. With an allele id, the gene, transcript and HGVS c. below
          are the registry's; without one, type them.
        </p>
        {resolved ? <ResolvedIdentity allele={resolved} /> : null}
      </div>
      <div className="grid gap-3 sm:grid-cols-2">
        <Field
          name="gene"
          label="Gene"
          placeholder="MYH7"
          required
          value={identity.gene}
          onValueChange={(gene) => editIdentity({ gene })}
        />
        <Field
          name="transcript"
          label="Transcript"
          placeholder="NM_000257.4"
          required
          value={identity.transcript}
          onValueChange={(transcript) => editIdentity({ transcript })}
        />
        <Field
          name="hgvsC"
          label="HGVS c."
          placeholder="c.1988G>A"
          required
          value={identity.hgvsC}
          onValueChange={(hgvsC) => editIdentity({ hgvsC })}
        />
        <Field
          name="diseaseLabel"
          label="Disease entity"
          placeholder="hypertrophic cardiomyopathy"
          required
        />
        <Field name="mondoId" label="MONDO ID" placeholder="MONDO:0005045" />
      </div>
      <div className="mt-4 flex items-center gap-2">
        <button
          type="submit"
          disabled={busy}
          className="framework-voice rounded-sm bg-primary px-3.5 py-1.5 text-[13.5px] text-primary-foreground disabled:opacity-40"
        >
          {busy ? "Adding…" : "Add variant"}
        </button>
        <button
          type="button"
          onClick={() => setOpen(false)}
          className="framework-voice rounded-sm border border-line-input bg-white px-3 py-1.5 text-[13px] text-ink-body"
        >
          Cancel
        </button>
        {error ? (
          <span className="framework-voice text-[12.5px] text-destructive">
            {error}
          </span>
        ) : null}
      </div>
    </form>
  );
}

function Field({
  name,
  label,
  placeholder,
  required,
  value,
  onValueChange,
}: {
  name: string;
  label: string;
  placeholder: string;
  required?: boolean;
  /** Controlled where a retrieval fills the field; left to the DOM otherwise. */
  value?: string;
  onValueChange?: (next: string) => void;
}) {
  return (
    <label className="block">
      <span className="field-eyebrow text-ink-label">{label}</span>
      <input
        name={name}
        required={required}
        placeholder={placeholder}
        {...(onValueChange
          ? {
              value: value ?? "",
              onChange: (e) => onValueChange(e.target.value),
            }
          : {})}
        className="framework-voice mt-1 w-full rounded-sm border border-line-input bg-white px-2.5 py-1.5 font-mono text-[13px] text-ink-body placeholder:font-sans placeholder:text-ink-faintest focus:border-ink-ghost focus:outline-none"
      />
    </label>
  );
}

/** What the registry answered, for the manager to check before committing. The protein and genomic
 *  forms are shown and not stored: the transcript, the HGVS c. and the allele id determine them. */
function ResolvedIdentity({ allele }: { allele: ResolvedAllele }) {
  const rows: [string, string][] = [
    ["Gene", allele.gene],
    ["MANE Select", `${allele.transcript}:${allele.hgvsC}`],
    ["Protein", allele.hgvsP],
    ["GRCh38", allele.hgvsG],
  ];
  return (
    <dl className="mt-3 space-y-1 border-line-row border-t pt-2">
      {rows
        .filter(([, value]) => value !== "")
        .map(([label, value]) => (
          <div key={label} className="flex gap-3 text-[12.5px]">
            <dt className="framework-voice w-24 shrink-0 text-ink-faint">
              {label}
            </dt>
            <dd className="min-w-0 break-all font-mono text-ink-body">
              {value}
            </dd>
          </div>
        ))}
    </dl>
  );
}

export function AssignPanel({
  variants,
  curators,
}: {
  variants: VariantRow[];
  curators: string[];
}) {
  const [status, setStatus] = useState<VariantStatus | null>(null);
  const [sort, setSort] = useState<SortBy>("recent");

  const withProgress = useMemo(
    () =>
      variants.map((variant) => ({
        variant,
        progress: variantProgress(variant.rows),
      })),
    [variants],
  );

  const counts = useMemo(() => {
    const out: Record<VariantStatus, number> = {
      unassigned: 0,
      pending: 0,
      in_progress: 0,
      part_submitted: 0,
      complete: 0,
    };
    for (const { progress } of withProgress) out[progress.status] += 1;
    return out;
  }, [withProgress]);

  const shown = useMemo(() => {
    const kept = withProgress.filter(
      (entry) => status === null || entry.progress.status === status,
    );
    if (sort === "recent") return kept;
    // `variants` arrives newest-registered first, so a stable sort keeps that as the tiebreak.
    return [...kept].sort(
      (a, b) =>
        VARIANT_STATUS_ORDER.indexOf(a.progress.status) -
        VARIANT_STATUS_ORDER.indexOf(b.progress.status),
    );
  }, [withProgress, status, sort]);

  if (variants.length === 0) {
    return (
      <p className="framework-voice rounded-md border border-line-primary border-dashed bg-white px-5 py-10 text-center text-[13.5px] text-ink-muted">
        No variants registered yet.
      </p>
    );
  }

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <StatusFilter
          order={VARIANT_STATUS_ORDER}
          labels={VARIANT_STATUS_LABELS}
          counts={counts}
          selected={status}
          onSelect={setStatus}
        />
        <SortControl
          value={sort}
          onChange={setSort}
          recentLabel="Newest registered"
        />
      </div>
      {shown.length === 0 ? (
        <p className="framework-voice rounded-md border border-line-primary border-dashed bg-white px-5 py-8 text-center text-[13.5px] text-ink-muted">
          No variant is{" "}
          {VARIANT_STATUS_LABELS[status ?? "pending"].toLowerCase()}.
        </p>
      ) : (
        shown.map(({ variant, progress }) => (
          <VariantCard
            key={variant.id}
            variant={variant}
            progress={progress}
            curators={curators}
          />
        ))
      )}
    </div>
  );
}

function VariantCard({
  variant,
  progress,
  curators,
}: {
  variant: VariantRow;
  progress: VariantProgress;
  curators: string[];
}) {
  const assigned = new Set(variant.rows.map((r) => r.curatorEmail));
  const available = curators.filter((c) => !assigned.has(c));
  const [choice, setChoice] = useState(available[0] ?? "");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  return (
    <section className="rounded-md border border-line-primary bg-white p-5">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <div>
          <p className="font-mono text-[13px] text-ink-primary">
            {variant.transcript}:{variant.hgvsC}
          </p>
          <p className="framework-voice mt-0.5 text-[13px] text-ink-muted">
            {variant.gene} · {variant.diseaseLabel}
          </p>
        </div>
        <span className="framework-voice flex items-baseline gap-3 text-[12.5px] text-ink-faint">
          <VariantStatusTag progress={progress} />
          {variant.rows.length} assigned
          {variant.rows.filter((r) => r.submittedAt !== null).length >= 2 ? (
            <a
              href={`/curation/compare/${variant.id}`}
              className="text-ink-body underline decoration-line-input underline-offset-2 hover:decoration-ink-ghost"
            >
              Read the divergence
            </a>
          ) : null}
        </span>
      </div>
      <ul className="mt-3 divide-y divide-line-row border-line-row border-t">
        {variant.rows.map((row) => (
          <li
            key={row.curatorEmail}
            className="flex items-center justify-between gap-3 py-2"
          >
            <span className="framework-voice text-[13px] text-ink-body">
              {row.curatorEmail}
            </span>
            <span className="framework-voice text-[12.5px] text-ink-faint">
              {row.submittedAt
                ? `submitted ${row.submittedAt.slice(0, 10)}`
                : row.draftCount === 0
                  ? "not started"
                  : `${row.draftCount} workflow${row.draftCount === 1 ? "" : "s"} in progress`}
            </span>
          </li>
        ))}
      </ul>
      {available.length > 0 ? (
        <div className="mt-3 flex flex-wrap items-center gap-2">
          <select
            aria-label={`Assign a curator to ${variant.gene} ${variant.hgvsC}`}
            value={choice}
            onChange={(e) => setChoice(e.target.value)}
            className="framework-voice rounded-sm border border-line-input bg-white px-2.5 py-1.5 text-[13px] text-ink-body"
          >
            {available.map((email) => (
              <option key={email} value={email}>
                {email}
              </option>
            ))}
          </select>
          <button
            type="button"
            disabled={busy}
            onClick={async () => {
              setBusy(true);
              setError("");
              try {
                const res = await fetch(
                  `/api/curation/variants/${variant.id}/assign`,
                  {
                    method: "POST",
                    headers: { "content-type": "application/json" },
                    body: JSON.stringify({ curatorEmail: choice }),
                  },
                );
                if (res.ok) {
                  window.location.reload();
                  return;
                }
                const body = (await res.json().catch(() => null)) as {
                  error?: { message?: string };
                } | null;
                setError(body?.error?.message ?? "Could not assign.");
              } catch {
                setError("Could not reach the server to assign. Try again.");
              }
              // Not a `finally`: the reload path leaves with the button still disabled.
              setBusy(false);
            }}
            className="framework-voice rounded-sm bg-primary px-3 py-1.5 text-[13px] text-primary-foreground disabled:opacity-40"
          >
            {busy ? "Assigning…" : "Assign"}
          </button>
          {error ? (
            <span className="framework-voice text-[12.5px] text-destructive">
              {error}
            </span>
          ) : null}
        </div>
      ) : null}
    </section>
  );
}
