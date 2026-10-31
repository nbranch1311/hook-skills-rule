import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import vm from "node:vm";

const root = dirname(fileURLToPath(import.meta.url));
const repo = dirname(dirname(root));

function fail(message) {
  throw new Error(message);
}

function expectThrow(label, fn) {
  try {
    fn();
  } catch {
    return;
  }
  fail("negative control did not fail: " + label);
}

function assertSkill(text) {
  const lines = text.split(/\r?\n/);
  if (lines.length >= 500) fail("SKILL.md is 500 lines or more");
  const front = text.match(/^---\n([\s\S]*?)\n---/);
  if (!front) fail("missing frontmatter");
  const matter = front[1];
  if (!/^name:\s*make-it-make-sense\s*$/m.test(matter)) fail("name");
  if (!/^disable-model-invocation:\s*true\s*$/m.test(matter)) fail("user-invoked");
  const description = matter.match(/^description:\s*(.+)\s*$/m);
  if (!description || description[1].length > 1024) fail("description");
  const required = [
    [/return point/, "return point"],
    [/Take Note/, "Take Note"],
    [/Take Action/, "Take Action"],
    [/\bQuestion\b/, "Question"],
    [/\bAgree\b/, "Agree"],
    [/\bbreadth\b/, "breadth"],
    [/\bdepth\b/, "depth"],
    [/\b[Ww]aive\b/, "waive"],
    [/go to/, "go to"],
    [/\banchor\b/i, "anchor"],
    [/\bgaps?\b/i, "gaps"],
    [/one step per turn/i, "one step per turn"],
    [/guide\.html/, "guide.html"],
    [/walk cursor/, "walk cursor"],
    [/preview/i, "preview"],
    [/after the walk/i, "after the walk"],
    [/one guide/i, "one guide"],
    [/Sample — replace this map/, "sample sentinel"],
  ];
  for (const [pattern, label] of required) {
    if (!pattern.test(text)) fail("skill missing " + label);
  }
}

function node() {
  return {
    children: [],
    attrs: {},
    listeners: {},
    style: {},
    className: "",
    textContent: "",
    value: "",
    parent: null,
    get firstChild() {
      return this.children[0] || null;
    },
    appendChild(child) {
      child.parent = this;
      this.children.push(child);
      return child;
    },
    removeChild(child) {
      const index = this.children.indexOf(child);
      if (index >= 0) this.children.splice(index, 1);
      return child;
    },
    setAttribute(name, value) {
      this.attrs[name] = String(value);
    },
    getAttribute(name) {
      return Object.prototype.hasOwnProperty.call(this.attrs, name) ? this.attrs[name] : null;
    },
    addEventListener(type, fn) {
      this.listeners[type] = fn;
    },
  };
}

function documentFrom(json) {
  const ids = new Map();
  function register(id) {
    const el = node();
    ids.set(id, el);
    return el;
  }
  register("walk-session").textContent = json;
  register("walk-title");
  register("tree");
  register("step-status");
  register("step-title");
  register("step-anchor");
  register("step-description");
  register("returns");
  register("later");
  register("go-to-phrase");
  register("go-to");
  register("copy-state");
  return {
    getElementById(id) {
      return ids.get(id) || null;
    },
    createElement() {
      return node();
    },
  };
}

function buttons(parent, found) {
  const list = found || [];
  if (parent.getAttribute && parent.getAttribute("data-id")) list.push(parent);
  for (const child of parent.children) buttons(child, list);
  return list;
}

function clone(value) {
  return JSON.parse(JSON.stringify(value));
}

function assertPreviewKeepsCursor(click, session, stepId) {
  const before = session.cursor;
  const view = click(session, stepId);
  if (session.cursor !== before) throw new Error("preview moved the walk cursor");
  if (!view.phrase.startsWith("go to " + stepId + " ")) throw new Error("go to phrase");
  if (view.walkId !== before) throw new Error("walk cursor");
}

function assertReturnClears(openReturns, session) {
  const step = session.sections[0].steps[1];
  for (const option of step.fork) option.coverage = "visited";
  if (openReturns(session).some((item) => item.id === "1.2")) {
    throw new Error("return point survived visited options");
  }
  step.fork[1].coverage = "waived";
  if (openReturns(session).some((item) => item.id === "1.2")) {
    throw new Error("return point survived a waived option");
  }
}

try {
run();
} catch (error) {
  console.error(error.message);
  process.exit(1);
}

function run() {
const skill = readFileSync(join(root, "SKILL.md"), "utf8");
const badSkill = skill.replaceAll("return point", "bookmark");
expectThrow("skill without return point", () => assertSkill(badSkill));
assertSkill(skill);

const html = readFileSync(join(root, "guide.html"), "utf8");
const script = html.match(/<script id="walk-guide">([\s\S]*?)<\/script>/);
if (!script) fail("guide script");
const island = html.match(/<script type="application\/json" id="walk-session">([\s\S]*?)<\/script>/);
if (!island) fail("session island");
const sample = JSON.parse(island[1]);
if (sample.title !== "Sample — replace this map") fail("sample title");

const sandbox = { console, globalThis: null };
sandbox.globalThis = sandbox;
vm.runInNewContext(script[1], sandbox, { filename: "guide.html" });
const api = sandbox.MakeItMakeSense;
if (!api) fail("guide api");

const session = clone(sample);
if (!api.openReturns(session).some((item) => item.id === "1.2" && item.open.includes("1.2.b"))) {
  fail("sample fork has no return point");
}
expectThrow("return point that ignores coverage", () =>
  assertReturnClears(() => [{ id: "1.2", open: ["1.2.b"] }], clone(sample)),
);
assertReturnClears(api.openReturns, clone(sample));

expectThrow("click that moves the cursor", () =>
  assertPreviewKeepsCursor((value, stepId) => {
    value.cursor = stepId;
    return api.viewState(value, stepId);
  }, clone(sample), "1.2.b"),
);
const preview = clone(sample);
assertPreviewKeepsCursor(api.onStepClick, preview, "1.2.b");
if (api.viewState(preview, "1.2.b").description !== "A tool that can show the route, the current step, a description, and the other steps.") {
  fail("preview description");
}

const doc = documentFrom(island[1]);
const mounted = api.mount(doc);
const here = buttons(doc.getElementById("tree")).find((button) => button.getAttribute("data-walk") === "here");
if (!here || here.getAttribute("data-id") !== "1.1") fail("walk cursor highlight");
if (doc.getElementById("step-description").textContent !== "The walk is about one named artifact.") {
  fail("current description");
}
const other = buttons(doc.getElementById("tree")).find((button) => button.getAttribute("data-id") === "1.2.b");
if (!other) fail("missing option button");
other.listeners.click();
if (mounted.session.cursor !== "1.1") fail("mount click moved the walk cursor");
if (doc.getElementById("step-description").textContent !== "A tool that can show the route, the current step, a description, and the other steps.") {
  fail("clicked description");
}
if (!doc.getElementById("go-to-phrase").value.startsWith("go to 1.2.b ")) fail("go to phrase");
const stillHere = buttons(doc.getElementById("tree")).find((button) => button.getAttribute("data-walk") === "here");
if (!stillHere || stillHere.getAttribute("data-id") !== "1.1") fail("highlight left the walk cursor");
if (!doc.getElementById("returns").textContent.includes("1.2.b")) fail("return point hidden");

const readme = readFileSync(join(repo, "README.md"), "utf8");
if (!readme.includes("skills/make-it-make-sense/")) fail("readme");
const ignore = readFileSync(join(repo, ".gitignore"), "utf8");
if (!ignore.includes(".walk/")) fail("gitignore");

console.log("make-it-make-sense verification passed");
}
