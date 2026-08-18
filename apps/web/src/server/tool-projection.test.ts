import { describe, expect, test } from "bun:test";
import { ToolLanguage } from "@/models/workbench";
import { projectToolCall } from "./tool-projection";

// The derivation rules, which are the whole of per-tool presentation: what a row labels
// a call, what it reveals on expand, and in what language that text is read.

const label = (name: string, input: Record<string, unknown>) =>
  projectToolCall(name, input).intent;

/** The text body a row reveals, refusing a call that carries a replacement instead. */
function bodyText(input: Record<string, unknown>): {
  text: string;
  language: ToolLanguage;
} {
  const call = projectToolCall("t", input);
  if (call.diff.length > 0) throw new Error("expected a text body");
  return { text: call.command, language: call.language };
}

describe("the one-line label", () => {
  test("a model-stated intent wins", () => {
    expect(
      label("shell", { command: "ls", intent: "list the workspace" }),
    ).toBe("list the workspace");
  });

  test("a blank intent falls through to the target", () => {
    expect(label("read", { intent: "  ", file_path: "/a.md" })).toBe("/a.md");
  });

  test("a search is labelled by what it searched for, not where", () => {
    // `pattern` is the required field of glob/grep and `path` the optional root, so a
    // search under a directory must not read as the directory.
    expect(label("grep", { pattern: "TODO", path: "/workspace" })).toBe("TODO");
    expect(label("web_search", { query: "ACMG SVCv4" })).toBe("ACMG SVCv4");
  });

  test("a tool with no known target is labelled by its own name", () => {
    expect(label("mystery", { some: "field" })).toBe("mystery");
    // A target of whitespace is no target: it would otherwise label the row with a blank.
    expect(label("read", { file_path: "   " })).toBe("read");
  });
});

describe("the body a row reveals", () => {
  test("a write reveals what was written, not the path it already labels", () => {
    const input = {
      file_path: "/workspace/document.md",
      content: "# Title\n\nBody.",
    };
    expect(label("write", input)).toBe("/workspace/document.md");
    expect(bodyText(input).text).toBe("# Title\n\nBody.");
  });

  test("written content is read in the language of the file it went to", () => {
    const written = (path: string) =>
      bodyText({ file_path: path, content: "x" }).language;
    expect(written("/a.md")).toBe(ToolLanguage.MARKDOWN);
    expect(written("/a.MD")).toBe(ToolLanguage.MARKDOWN);
    expect(written("/a.py")).toBe(ToolLanguage.PYTHON);
    expect(written("/a.json")).toBe(ToolLanguage.JSON);
    expect(written("/a.tsx")).toBe(ToolLanguage.TYPESCRIPT);
    expect(written("/a.sh")).toBe(ToolLanguage.SHELL);
    // An extension the map does not name, and a file with none at all.
    expect(written("/a.rs")).toBe(ToolLanguage.UNSPECIFIED);
    expect(written("/Makefile")).toBe(ToolLanguage.UNSPECIFIED);
  });

  test("a command that splices a heredoc payload is no one language", () => {
    const heredocs = [
      "python3 - <<'EOF'\nprint('hi')\nEOF",
      "cat <<EOF\ntext\nEOF",
      "cat <<-EOF\n\ttext\nEOF",
      "cat <<\\EOF\ntext\nEOF",
      "cat <<   EOF\ntext\nEOF",
      "<<EOF cat\ntext\nEOF", // the opener leads the command
    ];
    for (const command of heredocs) {
      expect([command, bodyText({ command }).language]).toEqual([
        command,
        ToolLanguage.UNSPECIFIED,
      ]);
    }
  });

  test("a left shift reads as an opener, and the command goes unlit", () => {
    // The detector is a regex, not a shell lexer, so it over-refuses rather than
    // mis-lexing: an arithmetic shift and a `<<` inside a quoted string both go
    // unspecified.
    for (const command of ["echo $((1 << 3))", 'git log --grep="a<<b"']) {
      expect([command, bodyText({ command }).language]).toEqual([
        command,
        ToolLanguage.UNSPECIFIED,
      ]);
    }
  });

  test("a here-string is not a heredoc: it stays one shell command", () => {
    // `<<<` retries one character in as `<<`, so the detector has to refuse it explicitly
    // rather than by the quoting of whatever follows.
    const shell = [
      "ls -la",
      "bc <<< 2+2",
      "cat <<<hello",
      "cat <<<'lit'",
      'grep x <<<"$body"',
    ];
    for (const command of shell) {
      expect([command, bodyText({ command }).language]).toEqual([
        command,
        ToolLanguage.SHELL,
      ]);
    }
  });

  test("a tool carrying no body reveals its whole input, as JSON", () => {
    // A read's line range and a grep's root live in the input and nowhere else, so
    // showing only the target would hide half the call.
    const body = bodyText({ file_path: "/a.md", view_range: [1, 40] });
    expect(body.language).toBe(ToolLanguage.JSON);
    expect(body.text).toContain("view_range");
    expect(body.text).toContain("\n"); // pretty-printed, not one line
  });

  test("an edit reveals both sides of its replacement", () => {
    const call = projectToolCall("edit", {
      file_path: "/a.md",
      old_string: "was",
      new_string: "is",
    });
    expect(call.diff.map((line) => line.text)).toEqual(["was", "is"]);
  });
});

describe("projecting a whole call", () => {
  test("no call carries two bodies", () => {
    // `command` and `diff` are separate fields, so nothing but this function stops both
    // being set — including on the inputs that fill neither branch cleanly.
    const inputs: Record<string, unknown>[] = [
      { file_path: "/a.md", old_string: "a", new_string: "b" },
      { command: "ls", intent: "look" },
      { file_path: "/a.md", content: "hello" },
      { file_path: "/a.md", view_range: [1, 2] },
      { file_path: "/a.md", old_string: "", new_string: "" },
      { file_path: "/a.md", content: "" },
    ];
    for (const input of inputs) {
      const call = projectToolCall("t", input);
      expect([input, call.command !== "" && call.diff.length > 0]).toEqual([
        input,
        false,
      ]);
    }
  });

  test("a replacement of nothing by nothing reveals the input instead", () => {
    // The diff branch would otherwise leave the row an empty bordered box: no lines to
    // draw, and a `command` emptied on the way out.
    const call = projectToolCall("edit", {
      file_path: "/a.md",
      old_string: "",
      new_string: "",
    });
    expect(call.diff).toEqual([]);
    expect(call.command).toContain("/a.md");
    expect(call.language).toBe(ToolLanguage.JSON);
  });

  test("a write that empties a file shows the emptiness it wrote", () => {
    const call = projectToolCall("write", { file_path: "/a.md", content: "" });
    expect(call.intent).toBe("/a.md");
    expect(call.command).toBe("");
    expect(call.diff).toEqual([]);
  });
});
