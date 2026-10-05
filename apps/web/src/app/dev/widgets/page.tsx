import { headers } from "next/headers";
import { notFound } from "next/navigation";
import { AppBar } from "@/components/app-bar";
import { BackLink } from "@/components/back-link";
import { Eyebrow } from "@/components/eyebrow";
import { IdentifierTag } from "@/components/identifier-tag";
import { drawAsset } from "@/components/widgets/draw";
import { WIDGETS } from "@/components/widgets/registry";
import { selectedBackend } from "@/server/backend";
import { userContext } from "@/server/context";
import { listExamples, type WidgetExample } from "@/server/widget-examples";
import { payloadTypeNames } from "@/widgets/payloads";
import { openExamples } from "./actions";

// The widget browser: every example under src/widgets/examples, grouped as the files are, each
// opened in the real workbench as an Analysis the fixture backend seeds, so a widget is drawn by its
// own component in a working document and its ticks and notes publish to the fixture's in-memory
// repository. Laid out as the Project page lays out its analyses. Served by the fixture backend only
// (docs/runbooks/widget-browser.md).

export const dynamic = "force-dynamic";

export default async function WidgetBrowserPage() {
  if (selectedBackend() !== "fixture") notFound();
  const { userEmail } = await userContext(await headers());
  const examples = listExamples();
  const groups = new Map<string, WidgetExample[]>();
  for (const example of examples) {
    groups.set(example.group, [...(groups.get(example.group) ?? []), example]);
  }
  const covered = new Set(examples.map((example) => example.typeName));
  const uncovered = payloadTypeNames().filter((type) => !covered.has(type));
  return (
    <div className="flex h-svh flex-col overflow-hidden bg-surface-warm-panel">
      <AppBar
        userEmail={userEmail}
        left={
          <>
            <BackLink href="/">Projects</BackLink>
            <span className="truncate text-[13px] font-semibold text-ink-primary">
              Widget examples
            </span>
          </>
        }
      />
      <main className="tscroll flex-1 overflow-auto px-[56px] py-[40px]">
        <div className="mx-auto flex max-w-[1100px] flex-col gap-[32px]">
          <div className="flex flex-col gap-[6px]">
            <h1 className="text-[22px] font-semibold tracking-[-0.01em] text-ink-primary">
              Widget examples
            </h1>
            <p className="max-w-[760px] text-[13px] leading-[1.55] text-ink-muted">
              Open an example to see its widget in a working document. Ticks and
              notes work there and are kept only while this local server runs.
              After editing an example&apos;s file, open it again for a fresh
              copy. Examples are read from files named{" "}
              <span className="font-mono text-[12px]">
                src/widgets/examples/&lt;widget&gt;/&lt;name&gt;.txtpb
              </span>
              ; files anywhere else are not listed.
            </p>
          </div>
          {groups.size === 0 && (
            <p className="rounded-card border border-dashed border-line-dashed bg-white px-[20px] py-[28px] text-[13px] text-ink-faintest">
              No example yet. Add one under apps/web/src/widgets/examples.
            </p>
          )}
          {[...groups].map(([group, members]) => (
            <section key={group} className="flex flex-col gap-[14px]">
              <div className="flex flex-wrap items-center gap-[8px]">
                <Eyebrow className="text-[10px]">{group}</Eyebrow>
                <span className="font-mono text-[11px] text-ink-faintest">
                  {members.length}
                </span>
                {[...new Set(members.map((example) => example.typeName))]
                  .filter((type) => type !== "")
                  .map((type) => (
                    <IdentifierTag
                      key={type}
                      className="rounded-tag px-[6px] py-[1.5px] text-[10.5px]"
                    >
                      {type}
                    </IdentifierTag>
                  ))}
                <span className="flex-1" />
                <form action={openExamples}>
                  <input type="hidden" name="group" value={group} />
                  <button
                    type="submit"
                    data-open-all={group}
                    className="rounded-button border border-line-primary bg-white px-[10px] py-[4px] text-[12.5px] text-ink-label hover:bg-surface-idle"
                  >
                    Open all {members.length}
                  </button>
                </form>
              </div>
              <ul className="grid grid-cols-[repeat(auto-fill,minmax(340px,1fr))] gap-[14px]">
                {members.map((example) => (
                  <li key={example.name}>
                    <ExampleCard example={example} />
                  </li>
                ))}
              </ul>
            </section>
          ))}
          {uncovered.length > 0 && (
            <section className="flex flex-col gap-[14px]">
              <Eyebrow className="text-[10px]">Without an example</Eyebrow>
              <p className="rounded-card border border-dashed border-line-dashed bg-white px-[20px] py-[20px] text-[13px] text-ink-faintest">
                No example yet for{" "}
                <span className="font-mono">{uncovered.join(", ")}</span>.
              </p>
            </section>
          )}
        </div>
      </main>
    </div>
  );
}

/** Why an example does not draw: the text is not a payload, or its asset draws as a placeholder by
 *  the path an embed draws it by. Undefined for one that draws. */
function problem(example: WidgetExample): string | undefined {
  if (example.asset === undefined) return `Does not read: ${example.error}`;
  const drawn = drawAsset({ bytes: example.asset, mode: "100644" }, WIDGETS);
  return drawn.kind === "placeholder"
    ? `Does not draw: ${drawn.reason}`
    : undefined;
}

/** One example as a card that opens it, drawn as the Project page draws an Analysis. */
function ExampleCard({ example }: { example: WidgetExample }) {
  const why = problem(example);
  const handle = `${example.group}/${example.name}`;
  // Group and name are asset-path segments, so the pair makes a valid id.
  const described = `example-${example.group}-${example.name}`;
  return (
    <form action={openExamples} className="h-full">
      <input type="hidden" name="group" value={example.group} />
      <input type="hidden" name="name" value={example.name} />
      <button
        type="submit"
        data-example={handle}
        aria-label={`Open the example ${handle}`}
        aria-describedby={described}
        className="flex h-full w-full flex-col gap-[10px] rounded-card border border-line-primary bg-white px-[18px] py-[16px] text-left hover:border-line-input hover:shadow-[0_1px_3px_rgba(0,0,0,0.05)]"
      >
        <span className="truncate font-mono text-[13px] font-medium text-ink-primary">
          {example.name}
        </span>
        <span id={described} className="contents">
          {example.description !== "" && (
            <span className="line-clamp-3 text-[12.5px] leading-[1.55] text-ink-muted">
              {example.description}
            </span>
          )}
          {why !== undefined && (
            <span className="text-[12.5px] leading-[1.5] text-amber-uncertainty-heading">
              {why}
            </span>
          )}
        </span>
        <span className="mt-auto pt-[2px] font-mono text-[10.5px] text-ink-faintest">
          examples/{example.group}/{example.name}.txtpb
        </span>
      </button>
    </form>
  );
}
