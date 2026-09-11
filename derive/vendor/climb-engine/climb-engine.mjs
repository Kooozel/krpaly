// src/climb-engine.config.ts
var RESAMPLE_MIN_INTERVAL_M = 12;
var CLIMB_LEADIN_GRADE_PCT = 2.5;
var DEFAULT_CLIMB_CONFIG = {
  // ── Resampling (Step 2) ─────────────────────────────────────────────────────
  /** Minimum distance (m) between two consecutive profile points after resampling. */
  RESAMPLE_MIN_INTERVAL_M,
  // ── Profile interpolation (between Step 2 and Step 3) ──────────────────────
  /** Minimum gap (m) that interpolateProfile() will fill with intermediate points.
   *  Pre-smoothing interpolation makes the rolling-average window behave uniformly
   *  across dense and sparse sections, at the cost of shifting climb boundaries
   *  slightly — the trade this value tunes. Applied in Step 2b of detectClimbs(). */
  INTERPOLATE_MAX_GAP_M: 25,
  // ── Smoothing — gradient estimation window (Step 3, Pass 1) ────────────────
  /** Half-width (m) of the window used to estimate local gradient magnitude. */
  SMOOTH_GRAD_WINDOW_M: 200,
  // ── Smoothing — adaptive rolling-average window (Step 3, Pass 2) ───────────
  /** Grade (fraction) above which the narrowest smoothing window is applied. */
  SMOOTH_STEEP_GRADE_THRESHOLD: 0.08,
  /** Grade (fraction) below which interpolation shifts toward the widest window. */
  SMOOTH_MID_GRADE_THRESHOLD: 0.03,
  /** Narrowest rolling-average window in metres (used on steep segments). */
  SMOOTH_WINDOW_MIN_M: 50,
  /** Rolling-average window (m) at the mid-grade boundary. */
  SMOOTH_WINDOW_MID_M: 150,
  /** Widest rolling-average window in metres (used on flat segments). */
  SMOOTH_WINDOW_MAX_M: 250,
  // ── Noise spike filter (Step 3, Pass 3) ────────────────────────────────────
  /** Gradient (fraction) that flags a point as a potential spike. */
  SPIKE_GRADIENT_THRESHOLD: 0.12,
  /** Gradient (fraction) the neighbouring segment must be below to confirm a spike. */
  SPIKE_NEIGHBOR_THRESHOLD: 0.08,
  /** Maximum segment length (m) for a point to be considered a spike candidate.
   *  Segments longer than this represent real terrain, not GPS jitter.
   *
   *  Its default is RESAMPLE_MIN_INTERVAL_M × 2, but the merged config is flat:
   *  overriding RESAMPLE_MIN_INTERVAL_M does not move this key. Set both. */
  SPIKE_MAX_SEGMENT_M: RESAMPLE_MIN_INTERVAL_M * 2,
  // ── Climb identification (Step 4) ───────────────────────────────────────────
  /** Gradient (%) at or above which a new climb candidate begins. */
  CLIMB_START_GRADE_PCT: 3.75,
  /** Lead-in extension threshold: gradient (%) above which a segment that
   *  *precedes* a climb-opening trigger is treated as part of the climb's
   *  natural approach. When a new candidate opens, the most recent run of
   *  segments at or above this grade (capped at CLIMB_LEADIN_MAX_DISTANCE_M)
   *  is prepended to the candidate. Stops at the first descent / flat segment. */
  CLIMB_LEADIN_GRADE_PCT,
  /** Maximum lead-in distance (m) absorbed by the start-extension step. */
  CLIMB_LEADIN_MAX_DISTANCE_M: 500,
  /** Gradient (%) above which an already-open candidate stays alive.
   *  This is the *continue* threshold; it resets the flat-distance close-counter
   *  but does NOT open a new candidate on its own. Setting it below
   *  CLIMB_START_GRADE_PCT introduces hysteresis: a steady 3.5 % climb with
   *  brief 4 % bursts starts on a burst and survives the 3.5 % stretches
   *  instead of being closed by the 700 m flat rule.
   *
   *  Empirical note: the current fixture set tolerates values in [3.5, 3.75]
   *  without regressions. Lower values (2.5–3.0) over-merge hukvaldy's chain of
   *  back-to-back sub-km bumps connected by 2.5–3.5 % slopes. A topology-aware
   *  trigger (net-gain-over-window) would let CONTINUE drop further safely;
   *  that's Phase 2 work. */
  CLIMB_CONTINUE_GRADE_PCT: 3.5,
  /** Gradient (%) at or below which a segment counts as a descent. */
  DESCENT_END_GRADE_PCT: -1,
  /** Accumulated descent distance (m) that ends the current climb candidate. */
  DESCENT_END_DISTANCE_M: 150,
  /** Accumulated flat/low-grade distance (m, grade < CLIMB_START_GRADE_PCT) that ends the current
   *  climb candidate. Prevents a brief 2%+ ramp at the start of a long flat section from absorbing
   *  every subsequent climb into one low-average-grade candidate. Keeping this small enough means
   *  distinct climbs separated by ~1 km of flat terrain are still identified as separate candidates,
   *  while the gain-scaled merge step can later re-join climbs that deserve it. */
  CLIMB_END_FLAT_M: 700,
  // ── Climb merging (Step 4 cont.) ───────────────────────────────────────────
  //
  // Gap distance uses a two-part formula:
  //   effectiveMaxGap = adjustedBase + min(smallerGain × MERGE_GAP_GAIN_SCALE, MERGE_GAP_MAX_BONUS_M)
  //
  // CLIMB_END_FLAT_M (above) creates a real distance gap between raw climb candidates
  // when a long flat section ends a climb. MERGE_GAP_GAIN_SCALE then decides whether
  // that gap is small enough relative to the smaller climb's elevation gain to merge.
  /** Base maximum gap (m) between two climbs that can be merged. Gain-scaling extends this. */
  MERGE_MAX_GAP_M: 1200,
  /** Metres of extra merge-gap allowance per metre of the smaller climb's elevation gain. */
  MERGE_GAP_GAIN_SCALE: 5.5,
  /** Cap on the gain-based bonus (m), keeping total effective gap from growing unbounded. */
  MERGE_GAP_MAX_BONUS_M: 4e3,
  /** Tighter base gap (m) used when the terrain in the gap *descends* in the raw profile.
   *  Prevents merging two climbs across a genuine valley when the gap distance would
   *  otherwise be within MERGE_MAX_GAP_M. Ascending/flat gaps keep the full base.
   *  The effective cap is max(MERGE_DESCENT_GAP_MAX_M, smallerGain × MERGE_DESCENT_SCALE)
   *  so large high-gain climbs can still bridge longer descent gaps (e.g. a levelling
   *  section mid-mountain). */
  MERGE_DESCENT_GAP_MAX_M: 760,
  MERGE_DESCENT_SCALE: 4,
  /** Absolute maximum valley drop (m) allowed between two merged climbs. */
  MERGE_MAX_VALLEY_DROP_M: 20,
  /** Combined-gain fraction used to compute the relative valley-drop limit. */
  MERGE_VALLEY_RATIO: 0.2,
  /** Reference ratio used only by the debug-stage `coherentAscent` signal in
   *  mergeNearbyClimbs(). Not currently wired into the merge decision — a
   *  naive force-merge based on this ratio over-merged adjacent-but-distinct
   *  climbs on rolling routes (bk / grun / hukvaldy) where two ascents share a
   *  high start-to-end raw rise. Kept here so future tuning / Phase 2 work can
   *  reuse the constant rather than rediscovering it. */
  MERGE_COHERENT_ASCENT_RATIO: 0.85,
  // ── Endpoint trimming (Step 5) ──────────────────────────────────────────────
  /** Lead-in trim threshold: gradient (%) below which *leading* segments are
   *  stripped. Set to CLIMB_LEADIN_GRADE_PCT so the explicit lead-in extension
   *  (prepended at identify time) survives trim. Stricter than this and the
   *  extension is undone immediately.
   *
   *  Like SPIKE_MAX_SEGMENT_M, the derivation is a default only: the merged
   *  config is flat, so overriding CLIMB_LEADIN_GRADE_PCT does not move this
   *  key. Set both. */
  TRIM_START_GRADE_PCT: CLIMB_LEADIN_GRADE_PCT,
  /** Tail trim threshold: gradient (%) below which *trailing* segments are
   *  stripped. Lower than CLIMB_START_GRADE_PCT so the climb extends through
   *  the natural sub-trigger run-out toward the summit without making detection
   *  more permissive in opening new candidates. */
  TRIM_END_GRADE_PCT: 2.5,
  /** Backward look-behind window (m) used to judge whether a candidate end-segment lies in a
   *  genuinely steep zone. At each candidate endIndex the algorithm sums the steep and total
   *  distance of the last TRIM_TAIL_WINDOW_M metres. If the steep fraction is below
   *  TRIM_STEEP_RATIO the segment is treated as an isolated noise spike and discarded. */
  TRIM_TAIL_WINDOW_M: 200,
  /** Minimum fraction of TRIM_TAIL_WINDOW_M that must be steep (≥ TRIM_END_GRADE_PCT) for
   *  the candidate endpoint to be accepted. Lower values tolerate noisy climbs; higher
   *  values enforce a cleaner end. */
  TRIM_STEEP_RATIO: 0.2,
  // ── Max sustained gradient (reporting / hiking score) ──────────────────────
  /** Window (m) over which the max *sustained* gradient is measured. Wide enough
   *  that a single steep pitch cannot dominate it — reporting that pitch is
   *  maxPitchGradient's job (gradient-zones.ts). Feeds the hiking score's G_max
   *  term and the CLI's `max_grade` column.
   *
   *  Deliberately its own constant rather than a reuse of SMOOTH_GRAD_WINDOW_M,
   *  which happens to be 200 too: that one is a smoothing input, and tying them
   *  together would move a reported figure every time smoothing is retuned. */
  MAX_SUSTAINED_GRADIENT_WINDOW_M: 200,
  // ── Summit snap (Step 5b) ───────────────────────────────────────────────────
  /** Cap (m) on how far past a climb's trimmed end the summit snap will look for a
   *  higher raw-profile point. The effective lookahead is the smaller of this and
   *  half the gap to the next climb, so the snap can never reach into one. */
  SNAP_LOOKAHEAD_MAX_M: 300
};

// src/max-gradient.ts
function maxGradientOverWindow(points, windowM) {
  if (points.length < 2) return 0;
  const first = points[0];
  const last = points[points.length - 1];
  const totalSpan = last.distance - first.distance;
  if (totalSpan < windowM) {
    if (totalSpan <= 0) return 0;
    return Math.max(0, (last.elevation - first.elevation) / totalSpan * 100);
  }
  let best = 0;
  for (let i = 0; i < points.length - 1; i++) {
    for (let j = i + 1; j < points.length; j++) {
      const dist = points[j].distance - points[i].distance;
      if (dist < windowM) continue;
      best = Math.max(best, (points[j].elevation - points[i].elevation) / dist * 100);
      break;
    }
  }
  return best;
}

// src/climb-engine.ts
var NOOP_DEBUG = () => {
};
function emptyDetectionResult() {
  return {
    climbs: [],
    totalDistance: 0,
    totalElevationGain: 0,
    totalElevationLoss: 0
  };
}
function detectClimbs(elevationData, options = {}) {
  const emit = options.debug ?? NOOP_DEBUG;
  const cfg = { ...DEFAULT_CLIMB_CONFIG, ...options.config };
  if (!elevationData || elevationData.length < 2) return emptyDetectionResult();
  const profile = elevationData.map((point) => ({
    distance: point[0],
    elevation: point[1],
    lat: point[2] ?? null,
    lon: point[3] ?? null
  }));
  const resampled = resamplePoints(profile, cfg);
  const interpolated = interpolateProfile(resampled, cfg);
  const smoothed = smoothElevationProfile(interpolated, cfg);
  const segments = calculateGradients(smoothed);
  emit({
    stage: "pipeline",
    rawPoints: profile.length,
    resampled: resampled.length,
    interpolated: interpolated.length,
    smoothed: smoothed.length,
    segments: segments.length
  });
  const rawClimbs = identifyClimbs(segments, resampled, emit, cfg);
  const mergedClimbs = mergeNearbyClimbs(rawClimbs, segments, resampled, emit, cfg);
  const measured = trimAndMeasure(mergedClimbs, emit, cfg);
  const climbs = snapAllEndCoords(measured, resampled, cfg);
  const { gain, descent } = calculateStats(resampled);
  return {
    climbs,
    totalDistance: profile[profile.length - 1].distance,
    totalElevationGain: gain,
    totalElevationLoss: descent
  };
}
function resamplePoints(profile, cfg = DEFAULT_CLIMB_CONFIG) {
  if (profile.length <= 2) return profile;
  const resampled = [profile[0]];
  for (let i = 1; i < profile.length; i++) {
    const prev = resampled[resampled.length - 1];
    const curr = profile[i];
    if (curr.distance - prev.distance >= cfg.RESAMPLE_MIN_INTERVAL_M) {
      resampled.push(curr);
    }
  }
  if (resampled[resampled.length - 1].distance !== profile[profile.length - 1].distance) {
    resampled.push(profile[profile.length - 1]);
  }
  return resampled;
}
function interpolateProfile(profile, cfg = DEFAULT_CLIMB_CONFIG) {
  if (profile.length <= 1) return profile;
  const result = [profile[0]];
  for (let i = 1; i < profile.length; i++) {
    const prev = profile[i - 1];
    const curr = profile[i];
    const gap = curr.distance - prev.distance;
    if (gap > cfg.INTERPOLATE_MAX_GAP_M) {
      const steps = Math.round(gap / cfg.RESAMPLE_MIN_INTERVAL_M);
      for (let s = 1; s < steps; s++) {
        const t = s / steps;
        result.push({
          distance: prev.distance + t * gap,
          elevation: prev.elevation + t * (curr.elevation - prev.elevation),
          lat: prev.lat != null && curr.lat != null ? prev.lat + t * (curr.lat - prev.lat) : null,
          lon: prev.lon != null && curr.lon != null ? prev.lon + t * (curr.lon - prev.lon) : null
        });
      }
    }
    result.push(curr);
  }
  return result;
}
function smoothElevationProfile(profile, cfg = DEFAULT_CLIMB_CONFIG) {
  if (profile.length <= 2) return profile;
  const localGradients = new Array(profile.length);
  for (let i = 0; i < profile.length; i++) {
    const center = profile[i].distance;
    const centerElev = profile[i].elevation;
    let sumGradBack = 0, sumWeightBack = 0;
    for (let j = i; j >= 0 && center - profile[j].distance <= cfg.SMOOTH_GRAD_WINDOW_M; j--) {
      const dist = center - profile[j].distance;
      const weight = 1 - dist / cfg.SMOOTH_GRAD_WINDOW_M;
      const grad = dist > 0 ? Math.abs(profile[j].elevation - centerElev) / dist : 0;
      sumGradBack += grad * weight;
      sumWeightBack += weight;
    }
    let sumGradFwd = 0, sumWeightFwd = 0;
    for (let j = i + 1; j < profile.length && profile[j].distance - center <= cfg.SMOOTH_GRAD_WINDOW_M; j++) {
      const dist = profile[j].distance - center;
      const weight = 1 - dist / cfg.SMOOTH_GRAD_WINDOW_M;
      const grad = Math.abs(profile[j].elevation - centerElev) / dist;
      sumGradFwd += grad * weight;
      sumWeightFwd += weight;
    }
    const backGrad = sumWeightBack > 0 ? sumGradBack / sumWeightBack : 0;
    const fwdGrad = sumWeightFwd > 0 ? sumGradFwd / sumWeightFwd : 0;
    localGradients[i] = Math.max(backGrad, fwdGrad);
  }
  const smoothed = new Array(profile.length);
  for (let i = 0; i < profile.length; i++) {
    const localGrad = localGradients[i];
    let windowMeters;
    if (localGrad > cfg.SMOOTH_STEEP_GRADE_THRESHOLD) {
      windowMeters = cfg.SMOOTH_WINDOW_MIN_M;
    } else if (localGrad > cfg.SMOOTH_MID_GRADE_THRESHOLD) {
      windowMeters = cfg.SMOOTH_WINDOW_MIN_M + (cfg.SMOOTH_STEEP_GRADE_THRESHOLD - localGrad) / (cfg.SMOOTH_STEEP_GRADE_THRESHOLD - cfg.SMOOTH_MID_GRADE_THRESHOLD) * (cfg.SMOOTH_WINDOW_MID_M - cfg.SMOOTH_WINDOW_MIN_M);
    } else {
      windowMeters = cfg.SMOOTH_WINDOW_MID_M + (cfg.SMOOTH_MID_GRADE_THRESHOLD - localGrad) / cfg.SMOOTH_MID_GRADE_THRESHOLD * (cfg.SMOOTH_WINDOW_MAX_M - cfg.SMOOTH_WINDOW_MID_M);
    }
    windowMeters = Math.max(
      cfg.SMOOTH_WINDOW_MIN_M,
      Math.min(cfg.SMOOTH_WINDOW_MAX_M, windowMeters)
    );
    const center = profile[i].distance;
    let sumElev = 0, sumWeight = 0;
    for (let j = i; j >= 0 && center - profile[j].distance <= windowMeters; j--) {
      const w = 1 - (center - profile[j].distance) / windowMeters;
      sumElev += profile[j].elevation * w;
      sumWeight += w;
    }
    for (let j = i + 1; j < profile.length && profile[j].distance - center <= windowMeters; j++) {
      const w = 1 - (profile[j].distance - center) / windowMeters;
      sumElev += profile[j].elevation * w;
      sumWeight += w;
    }
    smoothed[i] = {
      distance: profile[i].distance,
      elevation: sumWeight > 0 ? sumElev / sumWeight : profile[i].elevation,
      lat: profile[i].lat,
      lon: profile[i].lon
    };
  }
  return filterNoiseSpikes(smoothed, cfg);
}
function filterNoiseSpikes(profile, cfg = DEFAULT_CLIMB_CONFIG) {
  if (profile.length <= 2) return profile;
  const result = profile.map((p) => ({ ...p }));
  const original = profile;
  for (let i = 1; i < result.length - 1; i++) {
    const prev = original[i - 1];
    const curr = original[i];
    const next = original[i + 1];
    const leftDist = curr.distance - prev.distance;
    const rightDist = next.distance - curr.distance;
    if (leftDist > cfg.SPIKE_MAX_SEGMENT_M || rightDist > cfg.SPIKE_MAX_SEGMENT_M) continue;
    const prevGrad = Math.abs((curr.elevation - prev.elevation) / leftDist);
    const nextGrad = Math.abs((next.elevation - curr.elevation) / rightDist);
    if (prevGrad > cfg.SPIKE_GRADIENT_THRESHOLD && nextGrad < cfg.SPIKE_NEIGHBOR_THRESHOLD || nextGrad > cfg.SPIKE_GRADIENT_THRESHOLD && prevGrad < cfg.SPIKE_NEIGHBOR_THRESHOLD) {
      result[i] = { ...result[i], elevation: (prev.elevation + next.elevation) / 2 };
    }
  }
  return result;
}
function calculateGradients(profile) {
  const segments = [];
  for (let i = 1; i < profile.length; i++) {
    const prev = profile[i - 1];
    const curr = profile[i];
    const distanceDelta = curr.distance - prev.distance;
    const elevationDelta = curr.elevation - prev.elevation;
    const gradient = distanceDelta > 0 ? elevationDelta / distanceDelta * 100 : 0;
    segments.push({
      startDistance: prev.distance,
      endDistance: curr.distance,
      distance: distanceDelta,
      elevation: elevationDelta,
      gradient,
      startElevation: prev.elevation,
      endElevation: curr.elevation,
      startLat: prev.lat,
      startLon: prev.lon,
      endLat: curr.lat,
      endLon: curr.lon
    });
  }
  return segments;
}
function identifyClimbs(segments, rawProfile, emit, cfg = DEFAULT_CLIMB_CONFIG) {
  const climbs = [];
  let currentClimb = null;
  let descentDistance = 0;
  let flatDistance = 0;
  let leadinBuffer = [];
  let leadinDistance = 0;
  const resetLeadin = () => {
    leadinBuffer = [];
    leadinDistance = 0;
  };
  const trimLeadinTo = (maxM) => {
    while (leadinBuffer.length > 0 && leadinDistance - leadinBuffer[0].distance >= maxM) {
      leadinDistance -= leadinBuffer[0].distance;
      leadinBuffer.shift();
    }
  };
  const closeCurrentClimb = (tailTrimGrade, reason, atKm) => {
    if (!currentClimb) return;
    emit({ stage: "identify-close", reason, atKm, tailTrimGradePct: tailTrimGrade });
    const finalized = finalizeRawClimb(currentClimb, tailTrimGrade, emit);
    if (finalized) {
      const s0 = finalized.segments[0];
      const sN = finalized.segments[finalized.segments.length - 1];
      const rawGain = rawElevationGain(rawProfile, s0.startDistance, sN.endDistance);
      emit({
        stage: "identify-candidate",
        index: climbs.length,
        startKm: s0.startDistance / 1e3,
        endKm: sN.endDistance / 1e3,
        distanceM: finalized.totalDistance,
        elevationM: finalized.totalElevation,
        avgGradePct: finalized.totalElevation / finalized.totalDistance * 100,
        rawGainM: rawGain
      });
      climbs.push(finalized);
    }
    currentClimb = null;
    descentDistance = 0;
    flatDistance = 0;
  };
  for (const segment of segments) {
    const isClimbing = segment.gradient >= cfg.CLIMB_START_GRADE_PCT;
    const isContinuing = segment.gradient >= cfg.CLIMB_CONTINUE_GRADE_PCT;
    const isLeadin = segment.gradient >= cfg.CLIMB_LEADIN_GRADE_PCT;
    const isDescent = segment.gradient <= cfg.DESCENT_END_GRADE_PCT;
    descentDistance = isDescent ? descentDistance + segment.distance : 0;
    flatDistance = isContinuing ? 0 : flatDistance + segment.distance;
    if (currentClimb === null) {
      if (isLeadin) {
        leadinBuffer.push(segment);
        leadinDistance += segment.distance;
        trimLeadinTo(cfg.CLIMB_LEADIN_MAX_DISTANCE_M);
      } else {
        resetLeadin();
      }
    }
    if (isClimbing && currentClimb === null) {
      const segs = [...leadinBuffer];
      let totDist = 0;
      let totElev = 0;
      for (const s of segs) {
        totDist += s.distance;
        totElev += s.elevation;
      }
      currentClimb = {
        segments: segs,
        totalDistance: totDist,
        totalElevation: totElev
      };
      resetLeadin();
      descentDistance = 0;
      flatDistance = 0;
    } else if (currentClimb !== null) {
      currentClimb.segments.push(segment);
      currentClimb.totalDistance += segment.distance;
      currentClimb.totalElevation += segment.elevation;
      if (descentDistance >= cfg.DESCENT_END_DISTANCE_M) {
        closeCurrentClimb(0, "descent", segment.endDistance / 1e3);
      } else if (flatDistance >= cfg.CLIMB_END_FLAT_M) {
        closeCurrentClimb(cfg.CLIMB_START_GRADE_PCT, "flat", segment.endDistance / 1e3);
      }
    }
  }
  closeCurrentClimb(0, "descent", segments[segments.length - 1]?.endDistance / 1e3 || 0);
  return climbs;
}
function rawElevationGain(profile, startDist, endDist) {
  let lo = 0;
  while (lo < profile.length - 1 && profile[lo].distance < startDist) lo++;
  let hi = lo;
  while (hi < profile.length - 1 && profile[hi].distance < endDist) hi++;
  if (hi <= lo) return 0;
  let gain = 0;
  for (let i = lo; i < hi; i++) {
    const delta = profile[i + 1].elevation - profile[i].elevation;
    if (delta > 0) gain += delta;
  }
  return gain;
}
function finalizeRawClimb(climb, tailTrimGrade, emit) {
  const candidate = { ...climb, segments: [...climb.segments] };
  const origStartKm = climb.segments[0] ? climb.segments[0].startDistance / 1e3 : 0;
  const origEndKm = climb.segments[climb.segments.length - 1] ? climb.segments[climb.segments.length - 1].endDistance / 1e3 : 0;
  while (candidate.segments.length > 0 && candidate.segments[candidate.segments.length - 1].gradient < tailTrimGrade) {
    const removed = candidate.segments.pop();
    candidate.totalDistance -= removed.distance;
    candidate.totalElevation -= removed.elevation;
  }
  if (candidate.segments.length === 0 || candidate.totalDistance <= 0) {
    emit({
      stage: "identify-reject",
      reason: "empty",
      startKm: origStartKm,
      endKm: origEndKm
    });
    return null;
  }
  return candidate;
}
function rawElevationAt(profile, distanceM) {
  let lo = 0;
  while (lo < profile.length - 1 && profile[lo + 1].distance <= distanceM) lo++;
  if (lo >= profile.length - 1) return profile[lo].elevation;
  const a = profile[lo], b = profile[lo + 1];
  const t = b.distance > a.distance ? (distanceM - a.distance) / (b.distance - a.distance) : 0;
  return a.elevation + t * (b.elevation - a.elevation);
}
function mergeNearbyClimbs(climbs, allSegments, rawProfile = [], emit = NOOP_DEBUG, cfg = DEFAULT_CLIMB_CONFIG) {
  if (climbs.length <= 1) return climbs;
  const result = [climbs[0]];
  for (let i = 1; i < climbs.length; i++) {
    const prev = result[result.length - 1];
    const curr = climbs[i];
    const prevStart = prev.segments[0];
    const prevEnd = prev.segments[prev.segments.length - 1];
    const currStart = curr.segments[0];
    const currEnd = curr.segments[curr.segments.length - 1];
    const gapDistance = currStart.startDistance - prevEnd.endDistance;
    const valleyDrop = prevEnd.endElevation - currStart.startElevation;
    const combinedGain = prev.totalElevation + curr.totalElevation;
    const smallerGain = Math.min(prev.totalElevation, curr.totalElevation);
    const gainBonus = Math.min(smallerGain * cfg.MERGE_GAP_GAIN_SCALE, cfg.MERGE_GAP_MAX_BONUS_M);
    const gapRawNet = rawProfile.length > 0 ? rawElevationAt(rawProfile, currStart.startDistance) - rawElevationAt(rawProfile, prevEnd.endDistance) : 0;
    const descentCap = Math.max(cfg.MERGE_DESCENT_GAP_MAX_M, smallerGain * cfg.MERGE_DESCENT_SCALE);
    const adjustedBase = gapRawNet < -1 ? Math.min(cfg.MERGE_MAX_GAP_M, descentCap) : cfg.MERGE_MAX_GAP_M;
    const effectiveMaxGap = adjustedBase + gainBonus;
    const floor = Math.min(cfg.MERGE_MAX_VALLEY_DROP_M, smallerGain * 0.5);
    const maxAllowedDrop = Math.max(floor, combinedGain * cfg.MERGE_VALLEY_RATIO);
    const shouldMerge = gapDistance >= 0 && gapDistance <= effectiveMaxGap && valleyDrop <= maxAllowedDrop;
    const combinedRawRise = rawProfile.length > 0 ? rawElevationAt(rawProfile, currEnd.endDistance) - rawElevationAt(rawProfile, prevStart.startDistance) : 0;
    const coherentAscent = combinedGain > 0 && combinedRawRise >= cfg.MERGE_COHERENT_ASCENT_RATIO * combinedGain;
    emit({
      stage: "merge-pair",
      prevStartKm: prevStart.startDistance / 1e3,
      prevEndKm: prevEnd.endDistance / 1e3,
      currStartKm: currStart.startDistance / 1e3,
      currEndKm: currEnd.endDistance / 1e3,
      gapM: gapDistance,
      valleyDropM: valleyDrop,
      effectiveMaxGapM: effectiveMaxGap,
      maxAllowedDropM: maxAllowedDrop,
      coherentAscent,
      combinedRawRiseM: combinedRawRise,
      decision: shouldMerge ? "merge" : "skip",
      reason: shouldMerge ? "within-gap-and-valley" : gapDistance < 0 ? "negative-gap" : gapDistance > effectiveMaxGap ? "gap-too-large" : "valley-too-deep"
    });
    if (shouldMerge) {
      const gapSegs = allSegments.filter(
        (s) => s.startDistance >= prevEnd.endDistance - 0.1 && s.startDistance < currStart.startDistance
      );
      const mergedSegs = [...prev.segments, ...gapSegs, ...curr.segments];
      let totalDist = 0, totalElev = 0;
      for (const s of mergedSegs) {
        totalDist += s.distance;
        totalElev += s.elevation;
      }
      const merged = {
        segments: mergedSegs,
        totalDistance: totalDist,
        totalElevation: totalElev
      };
      const trimmed = trimClimbEndpoints(merged, cfg);
      result[result.length - 1] = trimmed.totalDistance > 0 ? trimmed : merged;
    } else {
      result.push(curr);
    }
  }
  return result;
}
function snapEndCoordsToRawPeak(climb, rawProfile, lookaheadM, cfg = DEFAULT_CLIMB_CONFIG) {
  if (lookaheadM <= 0) return climb;
  const lastSeg = climb.segments[climb.segments.length - 1];
  if (!lastSeg) return climb;
  const endDist = lastSeg.endDistance;
  const limitDist = endDist + lookaheadM;
  const rawEndElev = rawElevationAt(rawProfile, endDist);
  const DESCENT_STOP_M = 2;
  let runningMax = rawEndElev;
  let peakPoint = null;
  for (const pt of rawProfile) {
    if (pt.distance < endDist) continue;
    if (pt.distance > limitDist) break;
    if (pt.elevation > runningMax) {
      runningMax = pt.elevation;
      if (pt.lat != null && pt.lon != null) peakPoint = pt;
    } else if (pt.elevation < runningMax - DESCENT_STOP_M) {
      break;
    }
  }
  if (!peakPoint || peakPoint.lat == null || peakPoint.lon == null) return climb;
  if (peakPoint.distance <= endDist) return climb;
  const distToPeak = peakPoint.distance - endDist;
  if (peakPoint.elevation - rawEndElev < 1.5) return climb;
  if (distToPeak > 150) return climb;
  const distExtension = peakPoint.distance - endDist;
  const elevExtension = peakPoint.elevation - lastSeg.endElevation;
  if (elevExtension <= 0) return climb;
  const extensionSeg = {
    startDistance: endDist,
    endDistance: peakPoint.distance,
    distance: distExtension,
    elevation: elevExtension,
    gradient: elevExtension / distExtension * 100,
    startElevation: lastSeg.endElevation,
    endElevation: peakPoint.elevation,
    startLat: lastSeg.endLat,
    startLon: lastSeg.endLon,
    endLat: peakPoint.lat,
    endLon: peakPoint.lon
  };
  const newDistance = climb.distance + distExtension;
  const newElevation = climb.elevation + elevExtension;
  const newSegments = [...climb.segments, extensionSeg];
  return {
    ...climb,
    segments: newSegments,
    distance: newDistance,
    elevation: newElevation,
    avgGrade: newElevation / newDistance * 100,
    // Recomputed, not carried over: the extension segment is part of the climb
    // now, so a gradient measured before it was appended describes a shape that
    // no longer exists. That staleness is exactly what the old score-then-snap
    // ordering shipped — here it is one line to keep honest.
    maxSustainedGradient: computeMaxSustainedGradient(
      newSegments,
      cfg.MAX_SUSTAINED_GRADIENT_WINDOW_M
    ),
    endCoords: { lat: peakPoint.lat, lon: peakPoint.lon }
  };
}
function snapAllEndCoords(climbs, rawProfile, cfg = DEFAULT_CLIMB_CONFIG) {
  return climbs.map((climb, i) => {
    const nextStart = i + 1 < climbs.length ? climbs[i + 1].segments[0].startDistance : Infinity;
    const lastSeg = climb.segments[climb.segments.length - 1];
    const lookahead = Math.min(cfg.SNAP_LOOKAHEAD_MAX_M, (nextStart - lastSeg.endDistance) / 2);
    return snapEndCoordsToRawPeak(climb, rawProfile, lookahead, cfg);
  });
}
function trimClimbEndpoints(climb, cfg = DEFAULT_CLIMB_CONFIG) {
  const trimmed = { ...climb, segments: [...climb.segments] };
  if (!trimmed.segments || trimmed.segments.length === 0) return trimmed;
  let startIndex = 0;
  while (startIndex < trimmed.segments.length && trimmed.segments[startIndex].gradient < cfg.TRIM_START_GRADE_PCT) {
    startIndex++;
  }
  let endIndex = trimmed.segments.length - 1;
  while (endIndex >= 0 && trimmed.segments[endIndex].gradient < cfg.TRIM_END_GRADE_PCT) {
    endIndex--;
  }
  while (endIndex >= 0) {
    let windowDist = 0, steepDist = 0;
    for (let j = endIndex; j >= 0 && windowDist < cfg.TRIM_TAIL_WINDOW_M; j--) {
      windowDist += trimmed.segments[j].distance;
      if (trimmed.segments[j].gradient >= cfg.TRIM_END_GRADE_PCT) {
        steepDist += trimmed.segments[j].distance;
      }
    }
    if (windowDist < cfg.TRIM_TAIL_WINDOW_M * 0.5 || steepDist / windowDist >= cfg.TRIM_STEEP_RATIO)
      break;
    endIndex--;
    while (endIndex >= 0 && trimmed.segments[endIndex].gradient < cfg.TRIM_END_GRADE_PCT) {
      endIndex--;
    }
  }
  if (startIndex > endIndex) {
    return { segments: [], totalDistance: 0, totalElevation: 0 };
  }
  const climbSegments = trimmed.segments.slice(startIndex, endIndex + 1);
  let newDistance = 0, newElev = 0;
  for (const seg of climbSegments) {
    newDistance += seg.distance;
    newElev += seg.elevation;
  }
  return { segments: climbSegments, totalDistance: newDistance, totalElevation: newElev };
}
function trimAndMeasure(mergedClimbs, emit, cfg = DEFAULT_CLIMB_CONFIG) {
  return mergedClimbs.map((raw) => {
    const before = raw.segments;
    const trimmed = trimClimbEndpoints(raw, cfg);
    const kept = trimmed.totalDistance > 0 && trimmed.totalElevation > 0;
    if (before.length > 0) {
      const beforeStart = before[0].startDistance;
      const beforeEnd = before[before.length - 1].endDistance;
      let droppedHead = 0;
      let droppedTail = 0;
      if (kept && trimmed.segments.length > 0) {
        const ts = trimmed.segments[0].startDistance;
        const te = trimmed.segments[trimmed.segments.length - 1].endDistance;
        for (const s of before) {
          if (s.endDistance <= ts) droppedHead++;
          if (s.startDistance >= te) droppedTail++;
        }
      } else {
        droppedHead = before.length;
      }
      emit({
        stage: "trim",
        startKm: beforeStart / 1e3,
        endKm: beforeEnd / 1e3,
        droppedHeadSegs: droppedHead,
        droppedTailSegs: droppedTail,
        remainingDistanceM: trimmed.totalDistance,
        kept
      });
    }
    if (!kept) return null;
    const s0 = trimmed.segments[0];
    const sN = trimmed.segments[trimmed.segments.length - 1];
    emit({
      stage: "measure",
      startKm: s0.startDistance / 1e3,
      endKm: sN.endDistance / 1e3,
      distanceM: trimmed.totalDistance,
      avgGradePct: trimmed.totalElevation / trimmed.totalDistance * 100
    });
    return measureClimb(trimmed, cfg);
  }).filter((c) => c !== null);
}
function computeMaxSustainedGradient(segments, windowM = DEFAULT_CLIMB_CONFIG.MAX_SUSTAINED_GRADIENT_WINDOW_M) {
  if (segments.length === 0) return 0;
  const last = segments[segments.length - 1];
  const points = segments.map((s) => ({
    distance: s.startDistance,
    elevation: s.startElevation
  }));
  points.push({ distance: last.endDistance, elevation: last.endElevation });
  return maxGradientOverWindow(points, windowM) / 100;
}
function measureClimb(climb, cfg = DEFAULT_CLIMB_CONFIG) {
  if (!climb || climb.totalDistance === 0 || climb.totalElevation === 0) return null;
  const firstSeg = climb.segments[0];
  const lastSeg = climb.segments[climb.segments.length - 1];
  const markerCoords = firstSeg?.startLat != null && firstSeg?.startLon != null ? { lat: firstSeg.startLat, lon: firstSeg.startLon } : null;
  const endCoords = lastSeg?.endLat != null && lastSeg?.endLon != null ? { lat: lastSeg.endLat, lon: lastSeg.endLon } : null;
  return {
    distance: climb.totalDistance,
    elevation: climb.totalElevation,
    avgGrade: climb.totalElevation / climb.totalDistance * 100,
    maxSustainedGradient: computeMaxSustainedGradient(
      climb.segments,
      cfg.MAX_SUSTAINED_GRADIENT_WINDOW_M
    ),
    segments: climb.segments,
    markerCoords,
    endCoords
  };
}
function calculateStats(resampled) {
  let gain = 0;
  let descent = 0;
  for (let i = 1; i < resampled.length; i++) {
    const diff = resampled[i].elevation - resampled[i - 1].elevation;
    if (diff > 0) {
      gain += diff;
    } else if (diff < 0) {
      descent += Math.abs(diff);
    }
  }
  return { gain, descent };
}

// src/climb-types.ts
var ClimbCategory = {
  HC: "HC",
  Cat1: "1",
  Cat2: "2",
  Cat3: "3",
  Cat4: "4",
  Uncategorized: "uncategorized"
};

// src/scoring.ts
var ASO = {
  score: (climb) => climb.distance / 1e3 * climb.avgGrade * climb.avgGrade,
  thresholds: [
    { category: ClimbCategory.HC, min: 600 },
    { category: ClimbCategory.Cat1, min: 300 },
    { category: ClimbCategory.Cat2, min: 150 },
    { category: ClimbCategory.Cat3, min: 75 },
    { category: ClimbCategory.Cat4, min: 25 },
    { category: ClimbCategory.Uncategorized, min: 8 }
  ]
};
var GARMIN = {
  score: (climb) => climb.distance * climb.avgGrade,
  thresholds: [
    { category: ClimbCategory.HC, min: 64e3 },
    { category: ClimbCategory.Cat1, min: 48e3 },
    { category: ClimbCategory.Cat2, min: 32e3 },
    { category: ClimbCategory.Cat3, min: 16e3 },
    { category: ClimbCategory.Cat4, min: 8e3 },
    { category: ClimbCategory.Uncategorized, min: 1500 }
  ]
};
var HIKING = {
  score: (climb) => climb.elevation * climb.elevation / (8 * climb.distance) + climb.elevation * 2e-3 + climb.maxSustainedGradient * 0.5,
  thresholds: [
    { category: ClimbCategory.HC, min: 40 },
    { category: ClimbCategory.Cat1, min: 25 },
    { category: ClimbCategory.Cat2, min: 15 },
    { category: ClimbCategory.Cat3, min: 8 },
    { category: ClimbCategory.Cat4, min: 4 },
    { category: ClimbCategory.Uncategorized, min: 0.5 }
  ]
};
var SCORING_CONFIGS = {
  aso: ASO,
  garmin: GARMIN,
  hiking: HIKING
};
function score(result, model) {
  const config = typeof model === "string" ? SCORING_CONFIGS[model] : model;
  return result.climbs.map((climb) => {
    const difficulty = config.score(climb);
    const match = config.thresholds.find((t) => difficulty >= t.min);
    return match ? { ...climb, difficulty, category: match.category } : { ...climb, difficulty: null, category: null };
  });
}
export {
  ASO,
  ClimbCategory,
  DEFAULT_CLIMB_CONFIG,
  GARMIN,
  HIKING,
  SCORING_CONFIGS,
  computeMaxSustainedGradient as _computeMaxSustainedGradient,
  interpolateProfile as _interpolateProfile,
  measureClimb as _measureClimb,
  mergeNearbyClimbs as _mergeNearbyClimbs,
  resamplePoints as _resamplePoints,
  smoothElevationProfile as _smoothElevationProfile,
  snapAllEndCoords as _snapAllEndCoords,
  trimAndMeasure as _trimAndMeasure,
  detectClimbs,
  emptyDetectionResult,
  maxGradientOverWindow,
  score
};
