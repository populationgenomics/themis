import type { ReactNode } from "react";
import {
  AssetError,
  payloadTypeName,
  readAny,
  regularFileProblem,
} from "@/widgets/asset";
import type { FileAtCommit } from "@/workspace-copy/copy";
import { unregisteredTypeReason } from "./placeholder";
import type { RegisteredWidget } from "./registry";
import type { WidgetContext } from "./revision";

// How an asset's bytes draw, decided without React state: what an `::embed` draws, and what the
// widget browser's server page reports for an example. Not a client module, so a server component
// can call it.

/** How an asset's bytes draw: bound to its component, or a placeholder's reason. */
export type Drawn =
  | { kind: "drawn"; draw: (context: WidgetContext) => ReactNode }
  | { kind: "placeholder"; reason: string };

/** How the file at an `::embed`'s path draws, `null` being no file there. A type registered before
 *  its component draws as a placeholder, once its payload has passed the checks a widget's would. */
export function drawAsset(
  file: FileAtCommit | null,
  widgets: ReadonlyMap<string, RegisteredWidget>,
): Drawn {
  if (file === null) {
    return {
      kind: "placeholder",
      reason: "there is no file at this path in this version",
    };
  }
  const notRegular = regularFileProblem(file.mode);
  if (notRegular !== null) return { kind: "placeholder", reason: notRegular };
  try {
    const wrapped = readAny(file.bytes);
    const name = payloadTypeName(wrapped);
    const entry = widgets.get(name);
    const draw = entry?.read(wrapped, file) ?? null;
    if (draw === null) {
      return { kind: "placeholder", reason: unregisteredTypeReason(name) };
    }
    return { kind: "drawn", draw };
  } catch (error) {
    if (error instanceof AssetError) {
      return { kind: "placeholder", reason: error.message };
    }
    throw error;
  }
}
