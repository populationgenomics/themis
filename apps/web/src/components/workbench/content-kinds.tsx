"use client";

import { File, FileText, Loader2, Paperclip } from "lucide-react";
import type { ReactNode } from "react";
import type { MenuItem } from "@/components/ui/dropdown-menu";
import { useWidgetsSaving } from "@/components/widgets/widget-state";
import { api, paperContent } from "@/lib/api";
import { Representation } from "@/models/literature";
import type { ConversationEvent } from "@/models/workbench";
import type { CopyClearing } from "./clear-copy";
import type { Citation } from "./markdown";
import {
  PaperMarkdownView,
  PaperPdfView,
  RepresentationToggle,
  SupplementaryView,
  WorkingDocumentView,
} from "./tab-views";
import { VersionDropdown } from "./version-dropdown";
import type { DocumentVersion, WorkingDocumentState } from "./working-document";
import {
  type DocumentPin,
  type OpenTabOpts,
  pinnedDocumentCommit,
  type Source,
  type Tab,
  WORKING_DOC_TAB_ID,
} from "./workspace-model";
import type { WorkingDocumentSignal } from "./workspace-sync";

// The content-kind registry: one entry per tab kind (working document, paper, supplementary), each
// owning that kind's traits and wiring — whether it is pinned, how its id is derived from open args,
// how it fetches its payload (`open`), and how it renders (icon, label, content body, header
// accessory). `Pane` and the workspace controller stay content-agnostic: they look an entry up by
// `kind` and delegate. The conversation is a region, not a tab kind, so it has no entry here. The
// reducer never imports this file; the dependency runs registry → controller only (the controller
// reads REGISTRY at call time, not at module load).

/** What a rendered tab needs from its window. Filled from live queries in the main window and from the
 *  broadcast snapshot in a popped window, so the self-fetching kinds render identically in either. */
export interface RenderContext {
  events: ConversationEvent[];
  /** The working document at the commit the window renders. */
  workingDocument: WorkingDocumentState;
  /** The workspace branch's tip — never the pinned commit; null until the Poll answers. */
  documentSignal: WorkingDocumentSignal | null;
  /** The versions the picker lists, newest first; null until the copy has been read. */
  documentVersions: DocumentVersion[] | null;
  /** Clearing the browser's copy of the Analysis; null until the Poll names one. */
  clearCopy: CopyClearing | null;
  /** The email the BFF verified for this window's page: who a widget edit is committed as. */
  curatorEmail: string;
  /** The active paper's highlight quote, or undefined. */
  highlight: string | undefined;
  /** Reveal a citation beside its source pane (the pane binds its own ids into the `Source`). */
  onCitation: (citation: Citation) => void;
  /** Merge a partial payload into the active tab (e.g. the paper representation toggle). */
  patch: (payload: Record<string, unknown>) => void;
}

export interface ContentKind<P, Args = void> {
  pinned: boolean;
  id(args: Args): string;
  /** Fetch the payload and build the tab. Absent for always-open kinds (conversation, working doc). */
  open?(args: Args): Promise<Tab<P>>;
  /** A synchronous placeholder tab shown immediately while `open` runs (a loading state `open` patches
   *  over). Absent ⇒ the tab appears only once `open` resolves. */
  placeholder?(args: Args): Tab<P>;
  /** Project the persistable open args back out of a payload — the inverse of `open`, so a rehydration
   *  re-fetches by descriptor rather than persisting the stale payload. Absent for a pinned singleton
   *  (the working document is reconstructed, never re-fetched). */
  openArgs?(payload: P): Args;
  icon: ReactNode;
  label(payload: P): string;
  render(payload: P, ctx: RenderContext): ReactNode;
  headerAccessory?(payload: P, ctx: RenderContext): ReactNode;
  /** Items the pane's menu adds for this kind while its tab is active. */
  menuItems?(payload: P, ctx: RenderContext): MenuItem[];
}

const ICON_CLASS = "size-[16px]";

/** A centred spinner for a tab whose content is still loading (the paper placeholder). */
function Loading(): ReactNode {
  return (
    <div className="flex flex-1 items-center justify-center">
      <Loader2
        className="size-[20px] animate-spin text-ink-faintest"
        aria-label="Loading"
      />
    </div>
  );
}

/** The paper's fetch-failed state: the fail-loud message plus a retry. `openTab` reuses an existing
 *  tab without re-fetching, so a tab stranded by a transient error (a cold-start 503, a dropped
 *  connection) has no other way back — re-clicking its citation just re-activates the dead tab. */
function FetchFailed({ onRetry }: { onRetry: () => void }): ReactNode {
  return (
    <div className="flex flex-1 flex-col items-center justify-center gap-[12px] px-[28px] text-center">
      <span className="text-[13px] text-ink-faintest">
        Couldn't open this paper — it may not be in the corpus.
      </span>
      <button
        type="button"
        onClick={onRetry}
        className="rounded-field border border-line-soft px-[10px] py-[4px] text-[12px] font-medium text-ink-faint hover:text-ink-primary"
      >
        Try again
      </button>
    </div>
  );
}

interface WorkingDocPayload {
  /** A view-only pin to a historical version; absent or null follows the latest. */
  pin?: DocumentPin | null;
}

const workingDoc: ContentKind<WorkingDocPayload> = {
  pinned: true,
  id: () => WORKING_DOC_TAB_ID,
  icon: <FileText className={ICON_CLASS} aria-hidden />,
  label: () => "Working document",
  render: (_payload, ctx) => (
    <WorkingDocumentView
      document={ctx.workingDocument}
      unavailable={ctx.documentSignal?.unavailable === true}
      clearing={ctx.clearCopy}
      signal={ctx.documentSignal}
      curatorEmail={ctx.curatorEmail}
      onCitation={ctx.onCitation}
    />
  ),
  // Not over a damaged workspace: the damage is in the repository, which clearing leaves as it is.
  menuItems: (_payload, ctx) =>
    ctx.clearCopy === null ||
    ctx.clearCopy.state.kind === "clearing" ||
    ctx.workingDocument.kind === "damaged"
      ? []
      : [
          {
            key: "clear-copy",
            label: "Clear cache and reload",
            description:
              "Clears this workspace from your browser's cache and loads it fresh. Everything saved stays as it is.",
            onSelect: ctx.clearCopy.clear,
          },
        ],
  headerAccessory: (payload, ctx) => {
    const signal = ctx.documentSignal;
    if (
      signal?.tip?.kind !== "commit" ||
      signal.damaged ||
      ctx.documentVersions === null
    ) {
      return null;
    }
    const { analysisId } = signal;
    const tip = signal.tip.commit;
    const pinned = pinnedDocumentCommit(payload, analysisId);
    return (
      <>
        {pinned === null ? (
          <TipSaveState analysisId={analysisId} />
        ) : (
          <DocumentState text="Earlier version" />
        )}
        <VersionDropdown
          versions={ctx.documentVersions}
          selected={pinned ?? tip}
          onSelect={(commit) =>
            ctx.patch({
              pin: commit === null ? null : { analysisId, commit },
            })
          }
        />
      </>
    );
  },
};

/** The tip's save state: saving while a change made in any of the Analysis's widgets still waits on
 *  its publish, and saved otherwise. */
function TipSaveState({ analysisId }: { analysisId: string }): ReactNode {
  const saving = useWidgetsSaving(analysisId);
  return <DocumentState text={saving ? "Saving…" : "Saved"} />;
}

function DocumentState({ text }: { text: string }): ReactNode {
  return <span className="text-[11.5px] text-ink-faintest">{text}</span>;
}

interface PaperPayload {
  docId: string;
  /** True between the reveal and `getPaper` resolving: the tab shows immediately with a loading state;
   *  the other fields are placeholders until `open` patches them in. */
  loading: boolean;
  /** True when the fetch failed (e.g. a well-formed but unknown `doc_id` the BFF 404s): the tab stays,
   *  showing the failure, rather than silently vanishing. */
  error?: boolean;
  title: string;
  hasMarkdown: boolean;
  hasPdf: boolean;
  representation: Representation;
}

const paper: ContentKind<PaperPayload, { docId: string }> = {
  pinned: false,
  id: ({ docId }) => `paper:${docId}`,
  openArgs: (payload) => ({ docId: payload.docId }),
  // Show the tab at once with a loading state; `open` fills the real fields in (patched onto this).
  placeholder: ({ docId }) => ({
    id: `paper:${docId}`,
    kind: "paper",
    pinned: false,
    payload: {
      docId,
      loading: true,
      title: "",
      hasMarkdown: false,
      hasPdf: false,
      representation: Representation.MARKDOWN,
    },
  }),
  open: async ({ docId }) => {
    const info = await api.getPaper(docId);
    if (!info.hasMarkdown && !info.hasPdf) {
      // Neither representation exists — the servicer models this as REPRESENTATION_UNSPECIFIED (a
      // manifest entry whose rendering failed and whose PDF was never cached). Open in the fail-loud
      // error state, not on a view for a rendering the paper lacks (which would 404 with no toggle out).
      return {
        id: `paper:${docId}`,
        kind: "paper",
        pinned: false,
        payload: {
          docId,
          loading: false,
          error: true,
          title: info.title,
          hasMarkdown: false,
          hasPdf: false,
          representation: Representation.MARKDOWN,
        },
      };
    }
    return {
      id: `paper:${docId}`,
      kind: "paper",
      pinned: false,
      payload: {
        docId,
        loading: false,
        title: info.title,
        hasMarkdown: info.hasMarkdown,
        hasPdf: info.hasPdf,
        // Clamp to a representation the paper actually has: the toggle only shows when both exist, so
        // honouring a default that names an absent one would strand the tab on a 404'd view with no
        // way to switch. Prefer the default when available, else the other.
        representation:
          info.defaultRepresentation === Representation.PDF && info.hasPdf
            ? Representation.PDF
            : info.hasMarkdown
              ? Representation.MARKDOWN
              : Representation.PDF,
      },
    };
  },
  icon: <File className={ICON_CLASS} aria-hidden />,
  label: (payload) =>
    payload.error
      ? "Unavailable"
      : payload.loading
        ? "Loading…"
        : payload.title,
  render: (payload, ctx) => {
    if (payload.error)
      return (
        <FetchFailed
          onRetry={() => {
            ctx.patch({ loading: true, error: false });
            paper
              .open?.({ docId: payload.docId })
              .then((tab) =>
                ctx.patch(tab.payload as unknown as Record<string, unknown>),
              )
              .catch(() => ctx.patch({ loading: false, error: true }));
          }}
        />
      );
    if (payload.loading) return <Loading />;
    const quote = ctx.highlight ?? null;
    return payload.representation === Representation.PDF ? (
      <PaperPdfView
        url={paperContent.pdf(payload.docId)}
        docId={payload.docId}
        quote={quote}
      />
    ) : (
      <PaperMarkdownView docId={payload.docId} quote={quote} />
    );
  },
  headerAccessory: (payload, ctx) =>
    !payload.loading &&
    !payload.error &&
    payload.hasMarkdown &&
    payload.hasPdf ? (
      <RepresentationToggle
        representation={payload.representation}
        onChange={(representation) => ctx.patch({ representation })}
      />
    ) : null,
};

interface SupplementaryPayload {
  docId: string;
  name: string;
  mediaType: string;
}

const supplementary: ContentKind<
  SupplementaryPayload,
  { docId: string; name: string; mediaType: string }
> = {
  pinned: false,
  id: ({ docId, name }) => `supp:${docId}:${name}`,
  openArgs: (payload) => ({
    docId: payload.docId,
    name: payload.name,
    mediaType: payload.mediaType,
  }),
  open: async ({ docId, name, mediaType }) => ({
    id: `supp:${docId}:${name}`,
    kind: "supplementary",
    pinned: false,
    payload: { docId, name, mediaType },
  }),
  icon: <Paperclip className={ICON_CLASS} aria-hidden />,
  label: (payload) => payload.name,
  render: (payload) => (
    <SupplementaryView docId={payload.docId} name={payload.name} />
  ),
};

/** Keyed by tab kind. Payload/args are opaque (`unknown`) at the lookup boundary; each entry is
 *  precisely typed at its own definition, so the wiring stays type-checked. */
export const REGISTRY: Record<string, ContentKind<unknown, unknown>> = {
  "working-doc": workingDoc,
  paper,
  supplementary,
} as unknown as Record<string, ContentKind<unknown, unknown>>;

/** Resolve a `kind` + open `args` against the registry and open the tab beside `src`. The controller
 *  stays content-agnostic — it receives the derived id, a create-thunk, and the `{kind, args}` intent
 *  (which a mirror controller reposts as a command), never the registry — so the dependency runs
 *  registry → controller only. */
export async function openViaRegistry(
  controller: {
    openTab: (
      src: Source,
      id: string,
      create: () => Promise<Tab>,
      opts?: OpenTabOpts,
    ) => Promise<void>;
  },
  spec: { kind: string; args: unknown; src: Source },
  opts?: OpenTabOpts,
): Promise<void> {
  const kind = REGISTRY[spec.kind];
  if (!kind?.open) {
    // `async` so an unknown kind (a mirror on an older build reposting a since-removed kind) rejects
    // rather than throwing synchronously — the channel-handler call sites guard it with `.catch`.
    throw new Error(`content kind ${spec.kind} is not openable`);
  }
  const open = kind.open;
  return controller.openTab(
    spec.src,
    kind.id(spec.args),
    () => open(spec.args),
    {
      ...opts,
      intent: { kind: spec.kind, args: spec.args },
      placeholder: kind.placeholder?.(spec.args),
    },
  );
}
