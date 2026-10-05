import { describe, expect, test } from "bun:test";
import { readdirSync } from "node:fs";
import { join, relative } from "node:path";
import ts from "typescript";

// Hover text goes through `Tooltip` (tooltip.tsx), never a `title` attribute (.claude/rules/nextjs.md,
// "Hover text"). The browser's own title opens late, only over the painted pixels, and in its own style.
// This type-checks every source file and names each `title` that reaches the DOM: the attribute on an
// HTML or SVG element, or on a component whose `title` prop is React's DOM one (`next/link`, a
// component taking a DOM element's props), a spread carrying one, React's `createElement` with one,
// and an SVG `<title>` child, which the browser shows as a native hover too. An `iframe`'s title is
// its accessible name rather than hover text, so it stays; a component's own `title` prop, declared
// by the component, is its business. `.ts` files are read too, for React's `createElement`; the
// DOM's own `document.createElement` takes no props and is left alone.

const WEB = join(import.meta.dir, "..", "..", "..");
const SRC = join(WEB, "src");
const NAMED_BY_TITLE = new Set(["iframe"]);
const SAMPLE = join(SRC, "components", "ui", "no-title-hover.sample.tsx");

// Each line is one case; the expectation below names the lines that must be flagged.
const SAMPLE_SOURCE = [
  'import Link from "next/link";',
  'import { createElement, type ComponentProps } from "react";',
  "function Card(props: { title: string }) { return <h2>{props.title}</h2>; }",
  'function Chip(props: ComponentProps<"span">) { return <span {...props} />; }',
  "const y = 'why';",
  "const dom = { title: 'x' };",
  "export const a = <span title='x'>a</span>;",
  "export const b = <button title={y} type='button' />;",
  "export const c = <iframe title='frame' src='x' />;",
  "export const d = <Card title='heading' />;",
  "export const e = <svg><title>named</title></svg>;",
  "export const f = <Link href='/' title='x'>go</Link>;",
  "export const g = <Chip title='x' />;",
  "export const h = <span {...dom}>a</span>;",
  "export const i = createElement('span', { title: 'x' });",
  "export const j = createElement(Card, { title: 'heading' });",
  "export const k = document.createElement('span');",
].join("\n");

function components(dir: string): string[] {
  return readdirSync(dir, { withFileTypes: true }).flatMap((entry) => {
    const path = join(dir, entry.name);
    if (entry.isDirectory())
      return entry.name === "gen" ? [] : components(path);
    return /\.tsx?$/.test(entry.name) &&
      !entry.name.endsWith(".d.ts") &&
      !entry.name.includes(".test")
      ? [path]
      : [];
  });
}

/** One program over every component and the sample, under the app's own compiler options. */
function program(): ts.Program {
  const config = ts.readConfigFile(join(WEB, "tsconfig.json"), ts.sys.readFile);
  const { options } = ts.parseJsonConfigFileContent(config.config, ts.sys, WEB);
  const host = ts.createCompilerHost({ ...options, incremental: false });
  const read = host.getSourceFile.bind(host);
  host.getSourceFile = (name, version, ...rest) =>
    name === SAMPLE
      ? ts.createSourceFile(
          name,
          SAMPLE_SOURCE,
          version,
          true,
          ts.ScriptKind.TSX,
        )
      : read(name, version, ...rest);
  const exists = host.fileExists.bind(host);
  host.fileExists = (name) => name === SAMPLE || exists(name);
  return ts.createProgram({
    rootNames: [...components(SRC), SAMPLE],
    options: { ...options, incremental: false, noEmit: true },
    host,
  });
}

/** Whether `symbol` is React's DOM `title`, as @types/react declares it for HTML and SVG
 *  elements, rather than a prop a component declares for itself. */
function isDomTitle(symbol: ts.Symbol | undefined): boolean {
  return declaredByReact(symbol);
}

/** Whether `symbol` is declared by @types/react. */
function declaredByReact(symbol: ts.Symbol | undefined): boolean {
  return (symbol?.declarations ?? []).some((declaration) =>
    declaration.getSourceFile().fileName.includes("/@types/react/"),
  );
}

/** The name a call is made through: `createElement` in `createElement(...)` and `React.createElement(...)`. */
function callee(expression: ts.Expression): ts.Node {
  return ts.isPropertyAccessExpression(expression)
    ? expression.name
    : expression;
}

/** The symbol `node` names, through any import alias to its declaration. */
function resolved(
  checker: ts.TypeChecker,
  node: ts.Node,
): ts.Symbol | undefined {
  const symbol = checker.getSymbolAtLocation(node);
  return symbol !== undefined && symbol.flags & ts.SymbolFlags.Alias
    ? checker.getAliasedSymbol(symbol)
    : symbol;
}

/** Each `title` in `file` that reaches the DOM, as `file:line <what>`. */
function domTitles(checker: ts.TypeChecker, file: ts.SourceFile): string[] {
  const found: string[] = [];
  const at = (node: ts.Node, what: string) => {
    const { line } = file.getLineAndCharacterOfPosition(node.getStart());
    found.push(`${relative(SRC, file.fileName)}:${line + 1} ${what}`);
  };
  const titleOf = (type: ts.Type) => checker.getPropertyOfType(type, "title");
  const visit = (node: ts.Node): void => {
    // An SVG's `<title>` child: the browser shows it on hover as it would a title attribute.
    if (
      (ts.isJsxOpeningElement(node) || ts.isJsxSelfClosingElement(node)) &&
      node.tagName.getText(file) === "title"
    ) {
      at(node, "<title> in an SVG");
    }
    if (ts.isJsxOpeningElement(node) || ts.isJsxSelfClosingElement(node)) {
      const tag = node.tagName.getText(file);
      const exempt = NAMED_BY_TITLE.has(tag);
      for (const attribute of node.attributes.properties) {
        if (ts.isJsxAttribute(attribute)) {
          const name = attribute.name.getText(file);
          if (name !== "title" && name !== "xlink:title") continue;
          const props = checker.getContextualType(node.attributes);
          const symbol = props === undefined ? undefined : titleOf(props);
          if (!exempt && isDomTitle(symbol)) at(attribute, `<${tag} ${name}>`);
        } else {
          // A spread supplies a title only where its type requires one; a forwarding component's
          // own `{...props}`, typed with an element's optional DOM props, supplies what its caller
          // passed, and that call site is named instead.
          const spread = titleOf(
            checker.getTypeAtLocation(attribute.expression),
          );
          const props = checker.getContextualType(node.attributes);
          const required =
            spread !== undefined &&
            (spread.flags & ts.SymbolFlags.Optional) === 0;
          if (
            !exempt &&
            required &&
            props !== undefined &&
            isDomTitle(titleOf(props))
          ) {
            at(attribute, `<${tag} {...spread with title}>`);
          }
        }
      }
    }
    if (
      ts.isCallExpression(node) &&
      declaredByReact(resolved(checker, callee(node.expression))) &&
      node.arguments.length >= 2 &&
      ts.isStringLiteralLike(node.arguments[0]) &&
      !NAMED_BY_TITLE.has(node.arguments[0].text)
    ) {
      const props = checker.getTypeAtLocation(node.arguments[1]);
      if (titleOf(props) !== undefined) {
        at(node, `createElement("${node.arguments[0].text}", { title })`);
      }
    }
    ts.forEachChild(node, visit);
  };
  visit(file);
  return found;
}

const built = program();
const checker = built.getTypeChecker();

function titlesIn(path: string): string[] {
  const file = built.getSourceFile(path);
  if (file === undefined) throw new Error(`the program holds no ${path}`);
  return domTitles(checker, file);
}

describe("hover text", () => {
  test("names each title that reaches the DOM, and nothing else", () => {
    expect(titlesIn(SAMPLE)).toEqual([
      "components/ui/no-title-hover.sample.tsx:7 <span title>",
      "components/ui/no-title-hover.sample.tsx:8 <button title>",
      "components/ui/no-title-hover.sample.tsx:11 <title> in an SVG",
      "components/ui/no-title-hover.sample.tsx:12 <Link title>",
      "components/ui/no-title-hover.sample.tsx:13 <Chip title>",
      "components/ui/no-title-hover.sample.tsx:14 <span {...spread with title}>",
      'components/ui/no-title-hover.sample.tsx:15 createElement("span", { title })',
    ]);
  });

  test("never rides on a title attribute", () => {
    const offenders = components(SRC).flatMap(titlesIn);
    expect(offenders).toEqual([]);
  });
});
