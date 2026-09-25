const { test, describe } = require("node:test");
const assert = require("node:assert");
const fs = require("node:fs");
const path = require("node:path");

// The launcher asks two different questions of a local port: whether anything
// holds it, and who holds it. Only the second can rest on the holder's own
// command line, because argv is what its owner chooses to present. This file
// asserts the two questions stay separate -- that nothing deciding mere
// occupancy reaches its verdict through a process's claim about what it is.
//
// The sites are enumerated from the SOURCE by the shape of each comparison, not
// listed here: a comparison against a sentinel is resolved back to the probe
// that produced its left operand, and the pairing is what is asserted. A site
// added later is therefore covered without editing this file, and a site
// switched back to the identity probe fails it.

const ROOT = path.join(__dirname, "..");

/** Source with comments removed: this file asserts on code, and the code it
 *  guards names both probes in prose. The `[^:]` guard keeps `http://` inside a
 *  string literal from reading as the start of a line comment. */
function code(file) {
  return fs.readFileSync(path.join(ROOT, file), "utf8")
    .replace(/\/\*[\s\S]*?\*\//g, " ")
    .replace(/(^|[^:])\/\/.*$/gm, "$1");
}

const SUPERVISOR = code("gateway-supervisor.js");
const STOP = code("gateway-stop.js");

// A verdict about occupancy. "none" belongs here because it is the identity
// classifier's own occupancy value: comparing its answer against "none" is the
// exact shape this separation removes, so it must never resolve to that probe.
const OCCUPANCY_SENTINELS = new Set(["free", "bound", "none"]);
// A verdict about who the holder is. Reaching one of these through the
// occupancy probe would be a decision the occupancy probe cannot make.
const IDENTITY_SENTINELS = new Set(["kirocrew", "service", "foreign"]);

const OCCUPANCY_PROBE = "probeGatewayPortBinding";
const IDENTITY_PROBE = "probeGatewayPortOwner";
const PROBES = `(?:${OCCUPANCY_PROBE}|${IDENTITY_PROBE})`;

/**
 * The probe whose result `operand` carries, or null when it carries none.
 *
 * Two shapes reach a comparison: the call inline in the comparison itself, and
 * a binding assigned from the call and compared afterwards. For the second the
 * nearest PRECEDING assignment wins, which is what a reader of the statement
 * sequence would also take it to mean.
 *
 * An assignment counts only when the probe call is the assigned value itself. A
 * probe called inside another call's arguments -- a callback handed to a waiter,
 * say -- says nothing about what that waiter returns, so matching it would
 * attribute an unrelated verdict to a probe and let a real regression pass.
 */
function probeBehind(operand, offset) {
  const inline = operand.match(new RegExp(`await\\s+(${PROBES})\\s*\\(`));
  if (inline) return inline[1];
  const name = operand.trim().replace(/^\(+|\)+$/g, "").trim();
  if (!/^[A-Za-z_$][\w$]*$/.test(name)) return null;
  const decl = new RegExp(`(?:const|let|var)\\s+${name}\\s*=`, "g");
  let found = null;
  for (let m = decl.exec(SUPERVISOR); m; m = decl.exec(SUPERVISOR)) {
    if (m.index > offset) break;
    const probe = probeAtTopLevelOf(m.index + m[0].length);
    if (probe !== undefined) found = probe;
  }
  return found;
}

/**
 * The probe awaited at nesting depth zero of the statement starting at `from`,
 * null when that statement awaits none, and undefined when `from` does not begin
 * a statement that terminates.
 */
function probeAtTopLevelOf(from) {
  let depth = 0;
  for (let i = from; i < SUPERVISOR.length; i += 1) {
    const ch = SUPERVISOR[i];
    if (ch === "(" || ch === "[" || ch === "{") depth += 1;
    else if (ch === ")" || ch === "]" || ch === "}") depth -= 1;
    else if (ch === ";" && depth === 0) return null;
    else if (depth === 0 && SUPERVISOR.startsWith("await", i)) {
      const here = SUPERVISOR.slice(i).match(new RegExp(`^await\\s+(${PROBES})\\s*\\(`));
      if (here) return here[1];
    }
  }
  return undefined;
}

/** Every sentinel comparison in the supervisor that resolves to a probe. */
function adjudicatedComparisons() {
  const out = [];
  const cmp = /([\w$)\s.]*?(?:await\s+\w+\s*\([^()]*\)\s*\)*|[A-Za-z_$][\w$]*))\s*[!=]==\s*"([a-z-]+)"/g;
  for (let m = cmp.exec(SUPERVISOR); m; m = cmp.exec(SUPERVISOR)) {
    const sentinel = m[2];
    const kind = OCCUPANCY_SENTINELS.has(sentinel)
      ? "occupancy"
      : IDENTITY_SENTINELS.has(sentinel) ? "identity" : null;
    if (!kind) continue;
    const probe = probeBehind(m[1], m.index);
    if (!probe) continue;
    out.push({ probe, sentinel, kind });
  }
  return out;
}

/** The body of `name` in `source`, by brace balance from its declaration. The
 *  parameter list is skipped by paren balance first: both probes destructure
 *  their dependencies, so the first brace after the name belongs to a
 *  parameter, not to the body whose contents are being asserted. */
function bodyOf(source, name) {
  const start = source.search(new RegExp(`\\bfunction\\s+${name}\\s*\\(`));
  assert.notStrictEqual(start, -1, `${name} must exist`);
  const paramsOpen = source.indexOf("(", start);
  let parens = 0;
  let paramsClose = -1;
  for (let i = paramsOpen; i < source.length; i += 1) {
    if (source[i] === "(") parens += 1;
    else if (source[i] === ")") {
      parens -= 1;
      if (parens === 0) { paramsClose = i; break; }
    }
  }
  assert.notStrictEqual(paramsClose, -1, `${name}'s parameter list must close`);
  const open = source.indexOf("{", paramsClose);
  assert.notStrictEqual(open, -1, `${name} must have a body`);
  let depth = 0;
  for (let i = open; i < source.length; i += 1) {
    if (source[i] === "{") depth += 1;
    else if (source[i] === "}") {
      depth -= 1;
      if (depth === 0) return source.slice(open, i + 1);
    }
  }
  throw new Error(`${name}'s body is unbalanced`);
}

// Anything through which a process's own account of itself can enter a verdict.
// `getCommand` reads the holder's command line; the predicates judge it.
const SELF_ASSERTED_IDENTITY = [
  "getCommand",
  "isKirocrew",
  "isKirocrewCommand",
  "isTrustedWindowsGatewayCommand",
  "classifyPortOwner",
  "windowsProcessCommand",
  "psCommand",
];

describe("port occupancy is decided without a process's self-asserted identity", () => {
  test("every occupancy comparison resolves to the occupancy probe", () => {
    const wrong = adjudicatedComparisons()
      .filter((c) => c.kind === "occupancy" && c.probe !== OCCUPANCY_PROBE);
    assert.deepStrictEqual(
      wrong,
      [],
      "an occupancy verdict is being read from the identity probe, so a same-user "
      + "process can move it by choosing its argv: "
      + wrong.map((c) => `${c.probe} vs "${c.sentinel}"`).join(", "),
    );
  });

  test("every identity comparison resolves to the identity probe", () => {
    const wrong = adjudicatedComparisons()
      .filter((c) => c.kind === "identity" && c.probe !== IDENTITY_PROBE);
    assert.deepStrictEqual(
      wrong,
      [],
      "an identity verdict is being read from the occupancy probe, which cannot "
      + "answer it: "
      + wrong.map((c) => `${c.probe} vs "${c.sentinel}"`).join(", "),
    );
  });

  test("the enumeration adjudicates both probes, so a pass is not vacuous", () => {
    const seen = adjudicatedComparisons();
    const byKind = (k) => seen.filter((c) => c.kind === k).length;
    assert.ok(
      byKind("occupancy") > 0,
      `no occupancy comparison was resolved; the scan found ${seen.length} in total`,
    );
    assert.ok(
      byKind("identity") > 0,
      `no identity comparison was resolved; the scan found ${seen.length} in total`,
    );
  });

  test("the occupancy probe reads no command line", () => {
    const body = bodyOf(STOP, "probePortBinding");
    const leaked = SELF_ASSERTED_IDENTITY.filter((n) => new RegExp(`\\b${n}\\b`).test(body));
    assert.deepStrictEqual(
      leaked,
      [],
      `probePortBinding must decide from the pid list alone, but names ${leaked.join(", ")}`,
    );
  });

  test("the occupancy probe is wired with no identity dependency", () => {
    const body = bodyOf(SUPERVISOR, OCCUPANCY_PROBE);
    const leaked = SELF_ASSERTED_IDENTITY.filter((n) => new RegExp(`\\b${n}\\b`).test(body));
    assert.deepStrictEqual(
      leaked,
      [],
      `${OCCUPANCY_PROBE} must inject only a pid probe, but passes ${leaked.join(", ")}`,
    );
  });

  test("the identity classifier still refuses an unreadable probe", () => {
    const body = bodyOf(STOP, "classifyPortOwner");
    assert.match(
      body,
      /return "unknown"/,
      "classifyPortOwner must keep answering unknown when it cannot look",
    );
  });
});
