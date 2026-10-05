"use client";

import { createContext, useContext, useMemo } from "react";
import { RenderBoundary } from "@/components/render-boundary";
import type { Citation } from "@/components/workbench/citation";
import { useWorkspaceFile } from "@/lib/queries";
import { assetPathProblem } from "@/widgets/asset";
import type { FileAtCommit } from "@/workspace-copy/copy";
import { drawAsset } from "./draw";
import { EmbedPlaceholder } from "./placeholder";
import { WIDGETS } from "./registry";
import type { WidgetRevision } from "./revision";

// One `::embed[<path>]` in a working document: the asset at the path in the revision's tree, drawn by
// the component the registry maps its payload type to. Anything that cannot be drawn — a path outside
// the character set, no file there, bytes that are not an Any, a type this build does not draw, a
// payload failing its rules — draws a placeholder naming the path and the reason, and the rest of the
// document renders (docs/design/document-widgets.md, "Drawability is checked where the file is, and
// tolerated where it is shown").

/** What a surface drawing widgets gives every embed in its document. */
export interface WidgetSurfaceValue {
  revision: WidgetRevision;
  onCitation: (citation: Citation) => void;
}

const WidgetSurfaceContext = createContext<WidgetSurfaceValue | null>(null);

/** Provides the revision and citation handler to every `Embed` below it. */
export const WidgetSurface = WidgetSurfaceContext.Provider;

export function Embed({
  path,
  problem,
}: {
  /** The directive's label as written. */
  path: string;
  /** What is wrong with the directive itself, or null. */
  problem: string | null;
}): React.ReactElement {
  const surface = useContext(WidgetSurfaceContext);
  if (surface === null) {
    throw new Error(`::embed[${path}] is drawn outside a widget surface`);
  }
  const { revision, onCitation } = surface;
  const refused = problem ?? assetPathProblem(path);
  const file = useWorkspaceFile(
    refused === null
      ? {
          analysisId: revision.analysisId,
          tip: revision.tip,
          commit: revision.commit,
          path,
        }
      : null,
  );
  if (refused !== null)
    return <EmbedPlaceholder path={path} reason={refused} />;
  if (file.isError) {
    return (
      <EmbedPlaceholder
        path={path}
        reason={`the file could not be read: ${file.error.message}`}
      />
    );
  }
  if (file.data === undefined) {
    return (
      <div className="my-[12px] font-mono text-[12px] text-ink-faintest">
        Loading {path}…
      </div>
    );
  }
  return (
    <RenderBoundary
      resetKey={file.data.commit}
      fallback={(error) => (
        <EmbedPlaceholder
          path={path}
          reason={`the widget failed to draw: ${error.message}`}
        />
      )}
    >
      <DrawnFile
        path={path}
        file={file.data.value}
        drawnAt={file.data.commit}
        revision={revision}
        onCitation={onCitation}
      />
    </RenderBoundary>
  );
}

/** The file at an `::embed`'s path as read at `drawnAt`, drawn by its widget or as a placeholder. */
function DrawnFile({
  path,
  file,
  drawnAt,
  revision,
  onCitation,
}: {
  path: string;
  file: FileAtCommit | null;
  drawnAt: string;
  revision: WidgetRevision;
  onCitation: (citation: Citation) => void;
}): React.ReactElement {
  const drawn = useMemo(() => drawAsset(file, WIDGETS), [file]);
  if (drawn.kind === "placeholder") {
    return <EmbedPlaceholder path={path} reason={drawn.reason} />;
  }
  return (
    <div className="my-[12px]">
      {drawn.draw({
        path,
        drawnAt,
        revision,
        current: drawnAt === revision.commit,
        onCitation,
      })}
    </div>
  );
}
