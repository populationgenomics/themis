import { describe, expect, test } from "bun:test";
import { ProxyTracerProvider, trace } from "@opentelemetry/api";
import { registerTracing, SAMPLE_RATIO_VAR, sampleRatio } from "./register";

function delegate() {
  const provider = trace.getTracerProvider();
  return provider instanceof ProxyTracerProvider
    ? provider.getDelegate()
    : provider;
}

describe("the sample ratio", () => {
  test.each([
    ["0", 0],
    ["1", 1],
    ["0.25", 0.25],
  ])("%p reads as %p", (raw, ratio) => {
    expect(sampleRatio({ [SAMPLE_RATIO_VAR]: raw })).toBe(ratio);
  });

  test.each(["", " ", "one", "-0.1", "1.5", "NaN", "Infinity", "0x1", "0b1"])(
    "%p is refused",
    (raw) => {
      expect(() => sampleRatio({ [SAMPLE_RATIO_VAR]: raw })).toThrow(
        SAMPLE_RATIO_VAR,
      );
    },
  );

  test("unset is refused", () => {
    expect(() => sampleRatio({})).toThrow("required");
  });
});

describe("registration", () => {
  test("the fixture backend never reads the ratio and installs nothing", async () => {
    const before = delegate();
    await registerTracing({ THEMIS_BACKEND: "fixture" });
    expect(delegate()).toBe(before);
  });

  test("the live backend refuses to start without a ratio", async () => {
    await expect(registerTracing({ THEMIS_BACKEND: "live" })).rejects.toThrow(
      SAMPLE_RATIO_VAR,
    );
  });

  test("a live ratio of 0 installs nothing and reaches no cloud", async () => {
    // Nothing past the ratio check runs: no credentials are read, so this passes offline.
    const before = delegate();
    await registerTracing({
      THEMIS_BACKEND: "live",
      [SAMPLE_RATIO_VAR]: "0",
    });
    expect(delegate()).toBe(before);
  });
});
