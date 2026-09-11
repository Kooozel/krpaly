// krpaly's side of climb-engine: one Node process that runs detectClimbs over
// a whole batch of profiles, driven by krpaly_derive.detect.
//
// The library root, never climb-cli. The CLI takes one GPX path and emits ride
// JSON — moving time, VAM, heart-rate zones — that a DEM profile has no inputs
// for, and a process per candidate over a kraj's worth of candidates is the
// wrong shape. The import is the vendored build by relative path, so the code
// that ran is the code derivation.engine_commit names.
//
// NDJSON both ways, in lockstep: Python writes one line and reads one back.
// That cannot deadlock the way "write everything, then read" can on a pipe,
// and a missing or out-of-order reply is visible at once.
//
//   in  #1  {"config": {…override}, "model": "aso"}
//   out #1  {"engine": {"effective_config": {…}, "node": "v20…"}}
//   in  #n  {"id": <run_id>, "points": [[distance_m, elevation_m, lat, lon], …]}
//   out #n  {"id": <run_id>, "climbs": [{…}, …]}
//
// A line this side cannot answer ends the process with exit 2 and one line on
// stderr, which Python raises with.

import { createInterface } from "node:readline";

import {
  DEFAULT_CLIMB_CONFIG,
  SCORING_CONFIGS,
  detectClimbs,
  score,
} from "../vendor/climb-engine/climb-engine.mjs";

// The engine computes each of these from the other key once, at module load,
// and merges the override flat — so overriding the source alone leaves the
// derived key at its old value. The engine's own comments say "set both";
// this is where that is enforced.
const DERIVED_FROM = {
  SPIKE_MAX_SEGMENT_M: "RESAMPLE_MIN_INTERVAL_M",
  TRIM_START_GRADE_PCT: "CLIMB_LEADIN_GRADE_PCT",
};

function refuse(reason) {
  process.stderr.write(`harness: ${reason}\n`);
  process.exit(2);
}

// Checked here rather than in Python because this side holds the key set of
// the build that will actually run. The engine validates nothing: an unknown
// key is merged and never read, a string compares by coercion.
function readHeader(header) {
  const config = header?.config;
  if (typeof config !== "object" || config === null || Array.isArray(config)) {
    refuse(`the header's config is ${JSON.stringify(config)}, not an object`);
  }
  for (const [key, value] of Object.entries(config)) {
    if (!Object.hasOwn(DEFAULT_CLIMB_CONFIG, key)) {
      refuse(`${key} is not a climb-engine config key — the engine would ignore it`);
    }
    if (typeof value !== "number" || !Number.isFinite(value)) {
      refuse(`${key} is ${JSON.stringify(value)}, not a finite number`);
    }
  }
  for (const [derived, source] of Object.entries(DERIVED_FROM)) {
    if (Object.hasOwn(config, source) && !Object.hasOwn(config, derived)) {
      refuse(
        `${source} is overridden without ${derived}, which the engine derives from it ` +
          `at load and would leave at ${DEFAULT_CLIMB_CONFIG[derived]} — set both`,
      );
    }
  }
  if (!Object.hasOwn(SCORING_CONFIGS, header.model)) {
    refuse(
      `${JSON.stringify(header.model)} is not a scoring model — ` +
        `one of ${Object.keys(SCORING_CONFIGS).join(", ")}`,
    );
  }
  return { config, model: header.model };
}

// `segments` is the bulk of a climb and Python needs only where it starts and
// ends along the input: the engine reports no input indices, so the two
// distances are the position.
function project(climb) {
  return {
    distance: climb.distance,
    elevation: climb.elevation,
    avgGrade: climb.avgGrade,
    maxSustainedGradient: climb.maxSustainedGradient,
    startDistance: climb.segments[0].startDistance,
    endDistance: climb.segments.at(-1).endDistance,
    markerCoords: climb.markerCoords,
    endCoords: climb.endCoords,
    difficulty: climb.difficulty,
    category: climb.category,
  };
}

function reply(message) {
  process.stdout.write(`${JSON.stringify(message)}\n`);
}

let header = null;
for await (const line of createInterface({ input: process.stdin, crlfDelay: Infinity })) {
  let message;
  try {
    message = JSON.parse(line);
  } catch (error) {
    refuse(`a line is not JSON: ${error.message}`);
  }

  if (header === null) {
    header = readHeader(message);
    reply({
      engine: {
        effective_config: { ...DEFAULT_CLIMB_CONFIG, ...header.config },
        node: process.version,
      },
    });
    continue;
  }

  if (!Number.isSafeInteger(message.id) || message.id < 0 || !Array.isArray(message.points)) {
    refuse(`a run line needs an integer id and a points array, got id ${message.id}`);
  }
  // score() maps the result's climbs to scored copies, and a climb that clears
  // no threshold comes back with difficulty and category null — kept.
  const climbs = score(detectClimbs(message.points, { config: header.config }), header.model);
  reply({ id: message.id, climbs: climbs.map(project) });
}
