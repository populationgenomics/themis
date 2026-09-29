import type { DescMessage, MessageShape } from "@bufbuild/protobuf";
import type { Any } from "@bufbuild/protobuf/wkt";
import type { ComponentType, ReactNode } from "react";
import { ChecklistSchema } from "@/models/widgets";
import { readPayload } from "@/widgets/asset";
import type { FileAtCommit } from "@/workspace-copy/copy";
import { ChecklistWidget } from "./checklist";
import type { WidgetContext } from "./revision";

// The widget types this build draws, each payload type mapped to the component that draws it. Written
// by hand: behind each entry is a component somebody wrote, so the mapping is a decision, not a naming
// convention (docs/design/document-widgets.md, "Declaring a widget type"). registry.test.ts fails a
// message the `widget` option marks that has no entry here.

/** A payload type and how its asset is drawn. */
export interface RegisteredWidget {
  schema: DescMessage;
  /** Parse and validate the payload `wrapped`, read from the file `asset`, carries, and bind it to
   *  its component. Raises `AssetError` when the payload does not parse or fails its rules. */
  read(
    wrapped: Any,
    asset: FileAtCommit,
  ): (context: WidgetContext) => ReactNode;
}

/** Props of a widget component: its payload, the asset's bytes and tree mode it was read from, from
 *  which an edit makes the asset's new bytes, and where it was read from. */
export type WidgetProps<Payload> = WidgetContext & {
  payload: Payload;
  asset: FileAtCommit;
};

function widget<Desc extends DescMessage>(
  schema: Desc,
  Component: ComponentType<WidgetProps<MessageShape<Desc>>>,
): RegisteredWidget {
  return {
    schema,
    read: (wrapped, asset) => {
      const payload = readPayload(wrapped, schema);
      return (context) => (
        <Component
          key={context.path}
          {...context}
          payload={payload}
          asset={asset}
        />
      );
    },
  };
}

/** Every widget type this build draws, by payload type name. */
export const WIDGETS: ReadonlyMap<string, RegisteredWidget> = new Map(
  [widget(ChecklistSchema, ChecklistWidget)].map((entry) => [
    entry.schema.typeName,
    entry,
  ]),
);
