import { type DescFile, type DescMessage, getOption } from "@bufbuild/protobuf";
import { nestedTypes } from "@bufbuild/protobuf/reflect";
import {
  file_themis_widgets_models_checklist,
  file_themis_widgets_models_svcv4_classification,
  widget,
} from "@/models/widgets";

// Every widget payload schema this build knows, without the components that draw them: what the
// SharedWorker needs to read an asset whatever its type. payloads.test.ts fails a payload file
// generated under themis/widgets/models that is not listed here.

export const PAYLOAD_FILES: readonly DescFile[] = [
  file_themis_widgets_models_checklist,
  file_themis_widgets_models_svcv4_classification,
];

const PAYLOADS: ReadonlyMap<string, DescMessage> = new Map(
  PAYLOAD_FILES.flatMap((file) =>
    [...nestedTypes(file)].flatMap((type) =>
      type.kind === "message" && getOption(type, widget)
        ? [[type.typeName, type] as const]
        : [],
    ),
  ),
);

/** The schema of the marked payload type named `typeName`, or undefined for one this build does
 *  not know. */
export function payloadSchema(typeName: string): DescMessage | undefined {
  return PAYLOADS.get(typeName);
}
