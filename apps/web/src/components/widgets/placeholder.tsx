// What an `::embed` that cannot be drawn shows in the widget's place: the path and the reason
// (docs/design/document-widgets.md, "Drawability is checked where the file is, and tolerated where it
// is shown").

/** The reason an asset holding `typeName` draws as a placeholder when no component draws the type. */
export function unregisteredTypeReason(typeName: string): string {
  return `the file holds ${typeName}, which is not a widget type this build draws`;
}

export function EmbedPlaceholder({
  path,
  reason,
}: {
  path: string;
  reason: string;
}): React.ReactElement {
  return (
    <div
      role="note"
      className="my-[12px] rounded-[8px] border border-dashed border-line-dashed bg-surface-inset px-[13px] py-[10px] text-[12.5px] leading-[1.5] text-ink-muted"
    >
      <div className="font-medium text-ink-label">
        Not drawn:{" "}
        <span className="font-mono text-[12px]">{path || "::embed"}</span>
      </div>
      <div>{reason}.</div>
    </div>
  );
}
