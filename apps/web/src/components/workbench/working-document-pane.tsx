import { Markdown } from "@/components/workbench/markdown";
import type { WorkingDocument } from "@/models/workbench";

// The right working-document pane (design-spec §2.4.2). A null document is the loud
// "not produced yet" state, held until a poll reports a working-document version.

// The path in the sandbox workspace the agent writes the document to.
const DOCUMENT_PATH = "/workspace/document.md";

export function WorkingDocumentPane({
  document,
}: {
  document: WorkingDocument | null;
}) {
  if (!document) {
    return (
      <div className="relative flex w-[600px] shrink-0 flex-col bg-surface-doc-pane">
        <div className="flex h-[42px] shrink-0 items-center border-b border-line-soft px-[24px]">
          <span className="text-[13.5px] font-semibold text-ink-primary">
            Working document
          </span>
        </div>
        <div className="flex flex-1 items-center justify-center px-[28px] text-center text-[13px] text-ink-faintest">
          The agent has not written the working document yet.
        </div>
      </div>
    );
  }
  return (
    <div className="relative flex w-[600px] shrink-0 flex-col bg-surface-doc-pane">
      <div className="flex h-[42px] shrink-0 items-center justify-between border-b border-line-soft px-[24px]">
        <span className="text-[13.5px] font-semibold text-ink-primary">
          Working document
        </span>
        <span className="text-[11.5px] text-ink-faintest">
          Saved · v{document.version}
        </span>
      </div>

      <div className="tscroll flex-1 overflow-auto px-[28px] pt-[24px] pb-[30px]">
        <div className="mb-[16px] font-mono text-[12px] text-ink-faint">
          {DOCUMENT_PATH}
        </div>
        <Markdown text={document.markdown} />
      </div>
    </div>
  );
}
