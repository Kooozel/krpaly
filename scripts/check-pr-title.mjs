/**
 * check-pr-title.mjs — the squash title is the commit message on main.
 *
 * Merges here are squash-only and GitHub takes the squashed commit's subject
 * from the pull request title, so that one string — not the branch's WIP
 * commits — is what lands in `git log` and what the history is read from.
 * This validates it against the convention in CONTRIBUTING.md.
 *
 * The scope list is closed on purpose: a new area of the repo earns a scope by
 * being added here, which is a line of review rather than a typo nobody
 * notices.
 *
 * Usage — `node scripts/check-pr-title.mjs "fix(dem): …"`; with no argument
 * it reads PR_TITLE from the environment, which is how CI calls it.
 */

const TYPES = ["feat", "fix", "perf", "refactor", "docs", "test", "build", "ci", "chore", "revert"];

// Grouped by area. `engine` is how krpaly *calls* climb-engine — the vendored
// build, the harness, DetectClimbsOptions tuning — never detection itself,
// which is a pull request in the other repo.
const SCOPES = [
  // derive/
  "derive",
  "osm",
  "dem",
  "engine",
  "anchor",
  // db/
  "db",
  "schema",
  // web/
  "web",
  "api",
  "index",
  // everything else
  "ci",
  "deps",
  "docs",
  "test",
  "build",
];

// <type>(<scope>)!: <subject> — the scope and the breaking `!` are optional.
const SUBJECT = /^(?<type>[a-z]+)(?:\((?<scope>[a-z-]+)\))?(?<breaking>!)?: (?<rest>.+)$/;

// GitHub appends " (#12)" to the squashed subject itself; a title carrying one
// already would produce "(#12) (#12)".
const TRAILING_PR_REF = /\s\(#\d+\)$/;

const title = (process.argv[2] ?? process.env.PR_TITLE ?? "").trim();

const fail = (problem, hint) => {
  console.error(`PR title: ${problem}\n\n  ${title || "(empty)"}\n\n${hint}`);
  process.exit(1);
};

if (title === "") {
  fail("empty", "Pass the title as an argument or set PR_TITLE.");
}

const match = SUBJECT.exec(title);
if (match === null) {
  fail(
    "does not match <type>(<scope>)!: <subject>",
    `Types: ${TYPES.join(", ")}\nScopes (optional): ${SCOPES.join(", ")}\n` +
      `Example: fix(dem): sample the raster at cell centres, not corners`
  );
}

const { type, scope, breaking, rest } = match.groups;

if (!TYPES.includes(type)) {
  fail(`"${type}" is not a known type`, `Types: ${TYPES.join(", ")}`);
}

if (scope !== undefined && !SCOPES.includes(scope)) {
  fail(
    `"${scope}" is not a known scope`,
    `Scopes: ${SCOPES.join(", ")}\n` +
      `A genuinely new area of the repo earns a scope by being added to SCOPES in this file.`
  );
}

if (TRAILING_PR_REF.test(title)) {
  fail(
    "already ends in a (#123) reference",
    "GitHub appends the pull request number to the squashed subject itself — leave it off."
  );
}

if (/^[A-Z]/.test(rest)) {
  fail(
    "subject starts with a capital",
    "Write the subject lower-case, as a sentence would run on."
  );
}

if (rest.endsWith(".")) {
  fail("subject ends with a full stop", "Drop it — the subject is a title, not a sentence.");
}

if (title.length > 72) {
  fail(`is ${title.length} characters, over the 72 the subject line gets`, "Say it shorter.");
}

console.log(
  `PR title is well-formed: ${type}${scope ? `(${scope})` : ""}${breaking ?? ""}` +
    `${breaking ? " — breaking" : ""}`
);
