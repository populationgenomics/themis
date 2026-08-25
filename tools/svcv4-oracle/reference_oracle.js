// Reference oracle: fetch the ClinGen SVCv4 pilot calculator's scoring bundle, evaluate it in a vm
// sandbox, and answer probe questions using its own cap tables and combining functions. Emits one
// JSON document on stdout; knows nothing about themis (oracle.py drives it and does the diff).
//
// The bundle (calc-phase3.js) is fetched and evaluated in memory, never written to disk: it is
// © Baylor BRL and carries no open-source license.
//
// Reads a probe request as JSON on stdin: { banding_points, max_path_pairs, clamp_probes }.

const vm = require('node:vm');

const SOURCE_URL = 'https://calculator.clinicalgenome.org/v4/pilot/ui/javascripts/calc/calc-phase3.js';
const FETCH_TIMEOUT_MS = 30_000;
const EVAL_TIMEOUT_MS = 5_000;
const MAX_BUNDLE_BYTES = 4 * 1024 * 1024;

// Symbols lifted out of the bundle's top-level scope. A rename in the bundle makes the generated
// `return { ... }` throw ReferenceError, which we surface as a shape-change error.
const CAP_TABLES = ['EVIDENCE_CODE_CAP', 'EVIDENCE_CONCEPT_CAP', 'EVIDENCE_CATEGORY_CAP', 'EVIDENCE_CONCEPT_TO_CODES'];
const FUNCTIONS = ['getMaxOrMin', 'calClassification', 'applyConstraint'];
const EXPORTS = ['NA', ...CAP_TABLES, ...FUNCTIONS];

// Bare identifiers a genuine bundle must contain; a login-gated HTML redirect or a wholly
// restructured bundle fails here with a clear message. Matched on the identifier, not a
// `function foo` declaration form, so a const/arrow rewrite of a function does not false-negative.
const MARKERS = ['EVIDENCE_CODE_CAP', 'getMaxOrMin', 'calClassification', 'applyConstraint'];

async function fetchBundle() {
  let res;
  try {
    res = await fetch(SOURCE_URL, { signal: AbortSignal.timeout(FETCH_TIMEOUT_MS) });
  } catch (e) {
    if (e.name === 'TimeoutError') {
      throw new Error(`fetch ${SOURCE_URL} timed out after ${FETCH_TIMEOUT_MS} ms`);
    }
    throw new Error(`could not reach ${SOURCE_URL}: ${e.message}`);
  }
  if (res.status !== 200) {
    throw new Error(`fetch ${SOURCE_URL} returned HTTP ${res.status}; expected 200 (cookie-free public asset)`);
  }
  const declared = Number(res.headers.get('content-length'));
  if (Number.isFinite(declared) && declared > MAX_BUNDLE_BYTES) {
    throw new Error(`bundle Content-Length ${declared} exceeds the ${MAX_BUNDLE_BYTES}-byte cap`);
  }
  const src = await res.text();
  const bytes = Buffer.byteLength(src);
  if (bytes > MAX_BUNDLE_BYTES) {
    throw new Error(`bundle is ${bytes} bytes, over the ${MAX_BUNDLE_BYTES}-byte cap; refusing to evaluate`);
  }
  for (const marker of MARKERS) {
    if (!src.includes(marker)) {
      throw new Error(`bundle shape changed: identifier ${JSON.stringify(marker)} absent from ${SOURCE_URL}`);
    }
  }
  return { src, bytes };
}

function extractReference(src) {
  // Evaluate the bundle in a fresh vm context whose global exposes only inert DOM stubs and a quiet
  // console — no process/Buffer/fetch/timers, and no importModuleDynamically, so a compromised
  // origin's dynamic import() throws. This is NOT a hard security boundary (a determined bundle can
  // reach the outer realm through the passed-in global's constructor chain); it removes the ambient
  // Node globals that otherwise make a compromised origin a one-line RCE. Residual trust: the
  // ClinGen origin and its TLS.
  //
  // Wrapped in an IIFE returning the export object: the bundle's top-level const/function
  // declarations stay in the IIFE scope (const cap tables never become context globals), and the
  // injected `var retVal` keeps applyConstraint's sloppy implicit-global write off the frozen global.
  const quietConsole = { log() {}, info() {}, warn() {}, error() {}, debug() {} };
  const sandbox = Object.freeze({
    window: undefined,
    document: undefined,
    $: undefined,
    jQuery: undefined,
    navigator: undefined,
    console: quietConsole,
  });
  const wrapped = `(function () {\nvar retVal;\n${src}\n;return { ${EXPORTS.join(', ')} };\n})();`;
  let R;
  try {
    R = vm.runInNewContext(wrapped, sandbox, { timeout: EVAL_TIMEOUT_MS, filename: 'calc-phase3.js' });
  } catch (e) {
    throw new Error(`bundle shape changed: ${e.message}`);
  }
  for (const name of CAP_TABLES) {
    if (typeof R[name] !== 'object' || R[name] === null) {
      throw new Error(`bundle shape changed: ${name} is not an object`);
    }
  }
  for (const name of FUNCTIONS) {
    if (typeof R[name] !== 'function') {
      throw new Error(`bundle shape changed: ${name} is not a function`);
    }
  }
  return R;
}

function bandingRows(R, points) {
  // Pass a real number to calClassification (with a NaN guard) rather than leaning on its internal
  // string coercion; the original string is echoed back as the exact Decimal source for oracle.py.
  return points.map((raw) => {
    const pt = Number(raw);
    if (Number.isNaN(pt)) {
      throw new Error(`banding probe ${JSON.stringify(raw)} is not numeric`);
    }
    return { pt: raw, label: R.calClassification(pt) };
  });
}

function maxPathRows(R, pairs) {
  return pairs.map(([mis, spl]) => {
    const [key, value] = R.getMaxOrMin({ MIS: mis, SPL: spl });
    return { mis, spl, key, value };
  });
}

function clampRows(R, probes) {
  // A null probe bound is the unbounded side; the bundle spells that as its NA sentinel.
  return probes.map(([low, high, value]) => {
    const cap = [low === null ? R.NA : low, high === null ? R.NA : high];
    return { low, high, value, result: R.applyConstraint(cap, value) };
  });
}

async function readRequest() {
  const chunks = [];
  for await (const chunk of process.stdin) chunks.push(chunk);
  let request;
  try {
    request = JSON.parse(Buffer.concat(chunks).toString('utf8'));
  } catch (e) {
    throw new Error(`could not parse probe request from stdin as JSON: ${e.message}`);
  }
  for (const key of ['banding_points', 'max_path_pairs', 'clamp_probes']) {
    if (!Array.isArray(request[key])) {
      throw new Error(`probe request missing array field ${JSON.stringify(key)}`);
    }
  }
  return request;
}

async function main() {
  const request = await readRequest();
  const { src, bytes } = await fetchBundle();
  const R = extractReference(src);
  const response = {
    source_url: SOURCE_URL,
    fetched_bytes: bytes,
    na_sentinel: R.NA,
    caps: {
      code: R.EVIDENCE_CODE_CAP,
      concept: R.EVIDENCE_CONCEPT_CAP,
      category: R.EVIDENCE_CATEGORY_CAP,
      concept_to_codes: R.EVIDENCE_CONCEPT_TO_CODES,
    },
    banding: bandingRows(R, request.banding_points),
    max_path: maxPathRows(R, request.max_path_pairs),
    clamp: clampRows(R, request.clamp_probes),
  };
  process.stdout.write(JSON.stringify(response));
}

main().catch((err) => {
  process.stderr.write(`reference_oracle: ${err.message}\n`);
  process.exit(1);
});
