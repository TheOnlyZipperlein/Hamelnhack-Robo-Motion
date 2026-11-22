/**
 * Robot-control extension for the c500 plant.
 *
 * Registers:
 *  - a local OpenAI-compatible LLM provider (vLLM) pointed at PI_LLM_BASE
 *  - robot tools that talk back to the Flask web app's /api/robot/* REST API,
 *    which drives the SPS client (OPC UA -> GVL_Exchange)
 *
 * Environment (inherited from the Flask process):
 *  PI_WEB_APP_URL  Flask base URL (default http://127.0.0.1:8080)
 *  PI_LLM_BASE     OpenAI-compatible base URL (default http://10.30.35.110:4001/v1)
 *  PI_LLM_MODEL    model id (default terra)
 */
import { type ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { Type } from "typebox";

const WEB = process.env.PI_WEB_APP_URL ?? "http://127.0.0.1:8080";
const LLM_BASE = process.env.PI_LLM_BASE ?? "http://10.30.35.110:4001/v1";
const LLM_MODEL = process.env.PI_LLM_MODEL ?? "terra";

async function robotApi(
  path: string,
  method: "GET" | "POST" = "GET",
  body?: unknown,
): Promise<unknown> {
  const res = await fetch(WEB + "/api/robot" + path, {
    method,
    headers: { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const text = await res.text();
  let data: unknown;
  try {
    data = JSON.parse(text);
  } catch {
    data = text;
  }
  if (!res.ok) {
    const detail =
      typeof data === "string" ? data : JSON.stringify(data);
    throw new Error(`robot API ${method} ${path} -> HTTP ${res.status}: ${detail}`);
  }
  return data;
}

function textResult(obj: unknown) {
  const text = typeof obj === "string" ? obj : JSON.stringify(obj, null, 2);
  return { content: [{ type: "text" as const, text }], details: {} };
}

// ---- editable route (feature tracing on the background image) -------------
// plan_route stores the INITIAL point list of a line/polyline route.
// update_route sets offsets (dx/dz) per point RELATIVE TO THAT INITIAL
// point and can insert points on initial legs. check_route_on_image /
// simulate_route without `segments` use the current (initial + offsets) route.
type Pt = { x: number; z: number };
type RouteSpec = { segments: Record<string, unknown>[]; start?: Pt; closed?: boolean };
let routeBase: Pt[] | null = null;
let routeOffsets: Pt[] = [];

const r2 = (v: number) => Math.round(v * 100) / 100;

/** Flatten a line/polyline spec into its point list (null if it has arcs). */
function specPoints(spec: RouteSpec): Pt[] | null {
  const pts: Pt[] = [];
  if (spec.start) pts.push({ x: Number(spec.start.x), z: Number(spec.start.z) });
  for (const seg of spec.segments ?? []) {
    if (seg.kind === "line") {
      const to = seg.to as Pt;
      pts.push({ x: Number(to.x), z: Number(to.z) });
    } else if (seg.kind === "polyline") {
      for (const p of (seg.points as Pt[]) ?? []) pts.push({ x: Number(p.x), z: Number(p.z) });
    } else {
      return null;
    }
  }
  // drop a duplicated start point
  return pts.filter((p, i) => i === 0 || p.x !== pts[i - 1].x || p.z !== pts[i - 1].z);
}

function currentPoints(): Pt[] {
  return (routeBase ?? []).map((b, i) => ({
    x: r2(b.x + (routeOffsets[i]?.x ?? 0)),
    z: r2(b.z + (routeOffsets[i]?.z ?? 0)),
  }));
}

function currentSpec(): RouteSpec {
  const pts = currentPoints();
  return {
    start: pts[0],
    segments: [{ kind: "polyline", points: pts.slice(1) }],
    closed: false,
  };
}

function routeTable() {
  return (routeBase ?? []).map((b, i) => {
    const o = routeOffsets[i] ?? { x: 0, z: 0 };
    return {
      P: i + 1,
      initial: { x: r2(b.x), z: r2(b.z) },
      offset: { dx: r2(o.x), dz: r2(o.z) },
      current: { x: r2(b.x + o.x), z: r2(b.z + o.z) },
    };
  });
}

/** Resolve the spec for check/simulate: explicit segments, else current route. */
function resolveSpec(params: Partial<RouteSpec> & Record<string, unknown>): RouteSpec & Record<string, unknown> {
  if (params.segments && params.segments.length) return params as RouteSpec & Record<string, unknown>;
  if (!routeBase) throw new Error("Keine Route: zuerst plan_route (line/polyline) aufrufen oder segments angeben");
  return { ...params, ...currentSpec() };
}

/** Route-planning/dry-run endpoints (live under /api/route, not /api/robot). */
async function routeApi(
  path: string,
  method: "GET" | "POST" = "GET",
  body?: unknown,
): Promise<unknown> {
  const res = await fetch(WEB + "/api/route" + path, {
    method,
    headers: { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const text = await res.text();
  let data: unknown;
  try {
    data = JSON.parse(text);
  } catch {
    data = text;
  }
  if (!res.ok) {
    const detail =
      typeof data === "string" ? data : JSON.stringify(data);
    throw new Error(`route API ${method} ${path} -> HTTP ${res.status}: ${detail}`);
  }
  return data;
}

/** Other web-app endpoints (absolute path, e.g. /api/drawing). */
async function webApi(
  path: string,
  method: "GET" | "POST" = "GET",
  body?: unknown,
): Promise<unknown> {
  const res = await fetch(WEB + path, {
    method,
    headers: { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const text = await res.text();
  let data: unknown;
  try {
    data = JSON.parse(text);
  } catch {
    data = text;
  }
  if (!res.ok) {
    const detail = typeof data === "string" ? data : JSON.stringify(data);
    throw new Error(`web API ${method} ${path} -> HTTP ${res.status}: ${detail}`);
  }
  return data;
}

const Axis = Type.Union([Type.Literal("x"), Type.Literal("z")]);

export default function (pi: ExtensionAPI) {
  // ---- local LLM provider -------------------------------------------------
  pi.registerProvider("local-vllm", {
    baseUrl: LLM_BASE,
    apiKey: "local",
    api: "openai-completions",
    models: [
      {
        id: LLM_MODEL,
        name: `LiteLLM ${LLM_MODEL}`,
        // LiteLLM rejects `reasoning.effort` for models without
        // supports_reasoning in its cost map (e.g. sol, terra) -> only for qwen*.
        reasoning: LLM_MODEL.startsWith("qwen"),
        // Model must be vision-capable (sol and qwen are): simulate_route /
        // check_route_on_image return PNGs as image content.
        input: ["text", "image"],
        cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 },
        contextWindow: 131072, // model supports ~200k; 128k leaves headroom
        maxTokens: 8192,
      },
    ],
  });

  // ---- read tools ----------------------------------------------------------
  pi.registerTool({
    name: "get_robot_status",
    label: "Robot status",
    description:
      "Reads the current status signals of the robot (done, error, warning, " +
      "home position available, brake released, gripper active, error id, control/status words).",
    parameters: Type.Object({}),
    async execute() {
      return textResult(await robotApi("/status"));
    },
  });

  pi.registerTool({
    name: "get_robot_positions",
    label: "Robot positions",
    description:
      "Reads the set/act values of the robot (X/Z position and velocity, 8 channel " +
      "values, override, path velocity). Fractional values are preserved; engineering scale is unknown.",
    parameters: Type.Object({}),
    async execute() {
      return textResult(await robotApi("/positions"));
    },
  });

  pi.registerTool({
    name: "get_motion_profile",
    label: "Motion profile",
    description:
      "Reads one motion profile (1..10) from the g_ascProfilePar bank: " +
      "x/z position, path velocity, acceleration, deceleration, blending radius, halt flag.",
    parameters: Type.Object({
      index: Type.Number({ description: "Profile index, 1..10" }),
    }),
    async execute(_id, params) {
      return textResult(await robotApi(`/profiles/${Math.trunc(Number(params.index))}`));
    },
  });

  // ---- write tools -----------------------------------------------------------
  pi.registerTool({
    name: "set_motion_profile",
    label: "Set motion profile",
    description:
      "Partially writes one motion profile (1..10). Only the provided fields are " +
      "written; omitted fields keep their value.",
    parameters: Type.Object({
      index: Type.Number({ description: "Profile index, 1..10" }),
      x_pos: Type.Optional(Type.Number()),
      z_pos: Type.Optional(Type.Number()),
      path_vel: Type.Optional(Type.Number()),
      path_acc: Type.Optional(Type.Number()),
      path_dec: Type.Optional(Type.Number()),
      blending_radius: Type.Optional(Type.Number()),
      x_halt: Type.Optional(Type.Boolean()),
    }),
    async execute(_id, params) {
      const { index, ...fields } = params;
      return textResult(
        await robotApi(`/profiles/${Math.trunc(Number(index))}`, "POST", fields),
      );
    },
  });

  pi.registerTool({
    name: "start_path_run",
    label: "Start path run",
    description:
      "Starts a path run over the waypoints written by set_path_waypoints. " +
      "Simulator and live behave the same: the waypoints are driven by jog " +
      "(axis-parallel legs, diagonals/arcs as staircase, ~10 units/s) and returns " +
      "immediately with {mode: 'jog'}; then call wait_for_condition(done, path_run=true, " +
      "timeout >= path length * 0.8 + 30; jog drawing is slow, wait again if still running). stop_path_run cancels. path_vel_out/override " +
      "are ignored live. Never retry by jogging yourself. " +
      "Every run with draw=true (default) is kept as one PART of the drawing on the " +
      "canvas (pen down); earlier parts stay visible. Pass a short label (e.g. " +
      "'Kreis 1/4'). draw=false = travel move with the pen up (nothing drawn, no part). " +
      "Returns part {id, label}.",
    parameters: Type.Object({
      draw: Type.Optional(Type.Boolean({ description: "false = pen up / travel move. Default true" })),
      label: Type.Optional(Type.String({ description: "Short name of this drawing part" })),
      profile_index: Type.Optional(Type.Number({ description: "Profile index, 1..10" })),
      preview_id: Type.Optional(Type.String({ description: "Passed dry-run ID for read-only comparison" })),
      path_vel_out: Type.Optional(Type.Number()),
      override: Type.Optional(Type.Number()),
    }),
    async execute(_id, params) {
      return textResult(await robotApi("/path/start", "POST", params));
    },
  });

  // ---- drawing parts (canvas only, never moves the robot) -------------------
  pi.registerTool({
    name: "list_drawing_parts",
    label: "List drawing parts",
    description:
      "Lists the parts of the current drawing (id, label, start/end, running, " +
      "completed). Read-only. Use it to find the id before deleting a part.",
    parameters: Type.Object({}),
    async execute() {
      return textResult(await webApi("/api/drawing?points=0"));
    },
  });

  pi.registerTool({
    name: "delete_drawing_part",
    label: "Delete drawing part",
    description:
      "Removes ONE part from the drawing on the canvas (the robot does not move). " +
      "ONLY when the user explicitly asks to delete/remove that part. Never to " +
      "'tidy up' and never before drawing something new.",
    parameters: Type.Object({
      id: Type.Number({ description: "Part id from list_drawing_parts / start_path_run" }),
    }),
    async execute(_id, params) {
      return textResult(
        await webApi(`/api/drawing/parts/${Math.trunc(Number(params.id))}/delete`, "POST"),
      );
    },
  });

  pi.registerTool({
    name: "clear_drawing",
    label: "Clear drawing",
    description:
      "Removes ALL parts from the canvas drawing (the robot does not move). ONLY " +
      "when the user explicitly asks to clear/delete the whole drawing. Requires confirm=true.",
    parameters: Type.Object({
      confirm: Type.Boolean({ description: "Must be true; only on explicit user request" }),
    }),
    async execute(_id, params) {
      return textResult(await webApi("/api/drawing/clear", "POST", { confirm: params.confirm }));
    },
  });

  for (const [tool, verb, path] of [
    ["halt_path_run", "Halts", "/path/halt"],
    ["hold_path_run", "Holds", "/path/hold"],
    ["stop_path_run", "Stops", "/path/stop"],
    ["interrupt_path_run", "Interrupts", "/path/interrupt"],
  ] as const) {
    pi.registerTool({
      name: tool,
      label: `${verb} path run`,
      description: `${verb} the current path run.` +
        (tool === "halt_path_run" || tool === "hold_path_run" ? " Simulator only on the known c500 firmware." : ""),
      parameters: Type.Object({}),
      async execute() {
        return textResult(await robotApi(path, "POST"));
      },
    });
  }

  pi.registerTool({
    name: "start_homing",
    label: "Start homing",
    description:
      "Starts the homing procedure (g_xHomeExecute). The flag is edge-triggered " +
      "and latches: after arrival you MUST call clear_home_flag, otherwise the " +
      "next homing will be ignored.",
    parameters: Type.Object({}),
    async execute() {
      return textResult(await robotApi("/home", "POST"));
    },
  });

  pi.registerTool({
    name: "clear_home_flag",
    label: "Clear home flag",
    description:
      "Resets the latching homing flag (g_xHomeExecute) to False. Part of the " +
      "full homing recipe: start_homing -> wait_for_condition(done) -> clear_home_flag.",
    parameters: Type.Object({}),
    async execute() {
      return textResult(await robotApi("/home/clear", "POST"));
    },
  });

  pi.registerTool({
    name: "move_to_drawing_start",
    label: "Move to drawing start",
    description:
      "Home, then move to the lower-left drawing corner (30, 35) without drawing; " +
      "saved profiles are not touched. Live it runs the tested sequence " +
      "(homing -> jog X+ -> jog Z+) and blocks ~20 s; returns {ok, x, z} or an error. " +
      "Never rebuild this from start_homing/jog/path calls yourself.",
    parameters: Type.Object({}),
    async execute() {
      return textResult(await robotApi("/drawing-start", "POST"));
    },
  });

  pi.registerTool({
    name: "set_path_waypoints",
    label: "Set path waypoints",
    description:
      "Defines a path (Bahn) by writing consecutive waypoints into the profile " +
      "bank g_ascProfilePar[1..N]. Each waypoint: x, z plus optional path_vel, " +
      "path_acc, path_dec, blending_radius (corner rounding at that point). " +
      "Afterwards start_path_run executes the path (without profile_index = the " +
      "whole bank). Keep ALL points inside the safe box x: 30..260, z: 35..150 " +
      "- outside is the danger zone.",
    parameters: Type.Object({
      waypoints: Type.Array(
        Type.Object({
          x: Type.Number({ description: "X coordinate (safe: 30..260)" }),
          z: Type.Number({ description: "Z coordinate (safe: 35..150)" }),
          path_vel: Type.Optional(Type.Number()),
          path_acc: Type.Optional(Type.Number()),
          path_dec: Type.Optional(Type.Number()),
          blending_radius: Type.Optional(Type.Number({
            description:
              "Corner rounding radius at this point. Smooths sharp corners; " +
              "for a circle use ~ half the chord length (0.31 * circle radius).",
          })),
        }),
        { minItems: 1, maxItems: 10 },
      ),
    }),
    async execute(_id, params) {
      const results: unknown[] = [];
      const n = params.waypoints.length;
      for (let i = 0; i < n; i++) {
        const src = params.waypoints[i];
        // The API expects x_pos/z_pos (not x/z) — map explicitly.
        const wp: Record<string, unknown> = {
          x_pos: src.x,
          z_pos: src.z,
        };
        if (src.path_vel !== undefined) wp.path_vel = src.path_vel;
        if (src.path_acc !== undefined) wp.path_acc = src.path_acc;
        if (src.path_dec !== undefined) wp.path_dec = src.path_dec;
        if (src.blending_radius !== undefined) wp.blending_radius = src.blending_radius;
        // Mark the terminal waypoint so the path run stops there instead of
        // continuing through stale slots of the 10-slot profile bank.
        wp.x_halt = i === n - 1;
        await robotApi(`/profiles/${i + 1}`, "POST", wp);
        const actual = (await robotApi(`/profiles/${i + 1}`)) as Record<string, unknown>;
        for (const [key, expected] of Object.entries(wp)) {
          const value = actual[key];
          const matches = typeof expected === "number"
            ? typeof value === "number" && Math.abs(value - expected) < 1e-6
            : value === expected;
          if (!matches) throw new Error(`Profile ${i + 1}: read-back mismatch for ${key}`);
        }
        results.push(actual);
      }
      const last = params.waypoints[n - 1];
      const readBack = results[n - 1];
      // Soft hint (no hard gate): did these waypoints match the last
      // PASSED dry run? If false, the agent skipped the verification loop.
      let dryRunVerified = false;
      try {
        const lastRun = (await routeApi(`/last`)) as {
          has?: boolean;
          verdict?: { ok?: boolean };
          plan?: { waypoints?: Record<string, unknown>[] };
        };
        if (lastRun?.has && lastRun.verdict?.ok) {
          const ref = lastRun.plan?.waypoints ?? [];
          dryRunVerified =
            ref.length === n &&
            ref.every((rw, i) => {
              const sw = params.waypoints[i];
              return (
                Math.abs(Number(rw.x_pos) - sw.x) < 1e-6 &&
                Math.abs(Number(rw.z_pos) - sw.z) < 1e-6 &&
                rw.path_vel === sw.path_vel && rw.path_acc === sw.path_acc &&
                rw.path_dec === sw.path_dec && rw.blending_radius === sw.blending_radius
              );
            });
        }
      } catch {
        // hint is advisory — ignore lookup failures
      }
      return textResult({
        ok: true,
        count: n,
        last_slot_intended: { x_pos: last.x, z_pos: last.z },
        last_slot_read_back: readBack,
        dry_run_verified: dryRunVerified,
      });
    },
  });

  // ---- route planning + dry-run verification ------------------------------
  const SegmentSchema = Type.Union([
    Type.Object({
      kind: Type.Literal("line"),
      to: Type.Object({ x: Type.Number(), z: Type.Number() }),
    }),
    Type.Object({
      kind: Type.Literal("arc"),
      center: Type.Object({ x: Type.Number(), z: Type.Number() }),
      sweep_deg: Type.Optional(Type.Number()),
      ccw: Type.Optional(Type.Boolean()),
    }),
    Type.Object({
      kind: Type.Literal("polyline"),
      points: Type.Array(Type.Object({ x: Type.Number(), z: Type.Number() })),
    }),
  ]);

  pi.registerTool({
    name: "plan_route",
    label: "Plan route",
    description:
      "Plans a route from primitive segments (line / arc / polyline). A full " +
      "circle is an arc with sweep_deg 360. Returns <=10 profile waypoints " +
      "(auto-distributed, auto blending radius) and total length. A " +
      "line/polyline plan is stored as the INITIAL route P1..Pn for " +
      "update_route / check_route_on_image / simulate_route. Validates the WHOLE path against the safe box " +
      "(x 30..260, z 35..150) and fails with a reason when it does not fit. " +
      "Always plan BEFORE driving: the next step is simulate_route. " +
      "LIVE the waypoints are joined by STRAIGHT lines (blending is ignored), so " +
      "round shapes need many waypoints: plan at most 90 deg of arc per run " +
      "(circle = 4 consecutive runs with sweep_deg 90, each closed:false, each " +
      "start = exact last waypoint of the previous part).",
    parameters: Type.Object({
      segments: Type.Array(SegmentSchema, { minItems: 1 }),
      start: Type.Optional(
        Type.Object({ x: Type.Number(), z: Type.Number() }),
      ),
      closed: Type.Optional(Type.Boolean()),
      vel: Type.Optional(Type.Number({ description: "Default 350" })),
      acc: Type.Optional(Type.Number({ description: "Default 1000" })),
    }),
    async execute(_id, params) {
      const res = (await routeApi("/plan", "POST", params)) as Record<
        string,
        unknown
      >;
      if (typeof res === "object" && res && "error" in res) {
        return textResult({ ok: false, error: String(res.error) });
      }
      // A new plan becomes the INITIAL route for update_route (offsets reset).
      const pts = specPoints(params as RouteSpec);
      routeBase = pts && pts.length >= 2 ? pts : null;
      routeOffsets = (routeBase ?? []).map(() => ({ x: 0, z: 0 }));
      const { nominal: _n, ...slim } = res;
      return textResult({
        ...slim,
        route_points: routeBase ? routeTable().map((r) => ({ P: r.P, ...r.initial })) : undefined,
        note: routeBase
          ? "Initiale Route gespeichert (P1..Pn). Korrekturen nur noch per update_route (Offsets relativ zur initialen Route); check_route_on_image/simulate_route ohne segments nutzen die aktuelle Route."
          : undefined,
      });
    },
  });

  pi.registerTool({
    name: "simulate_route",
    label: "Simulate route (dry run)",
    description:
      "Plans AND dry-runs the route in a headless fast-forward simulation " +
      "(the robot is NEVER touched), verifies the result against generic " +
      "invariants (completed, fault-free, path fidelity, coverage, end " +
      "position, safe box, no backtracking) and returns: verdict {ok, " +
      "metrics, reasons} PLUS a rendered PNG (planned path gray, driven " +
      "trail blue, deviations red). ALWAYS evaluate both the metrics AND " +
      "the image before driving. On fail: fix the spec (fewer/smaller " +
      "geometry, lower vel) and simulate again — max 3 attempts.",
    parameters: Type.Object({
      segments: Type.Optional(Type.Array(SegmentSchema, { minItems: 1, description: "Omit to dry-run the CURRENT route (plan_route + update_route)" })),
      start: Type.Optional(
        Type.Object({ x: Type.Number(), z: Type.Number() }),
      ),
      origin: Type.Optional(Type.Object({ x: Type.Number(), z: Type.Number() }, {
        description: "Simulated robot position before approach; defaults to simulator home",
      })),
      closed: Type.Optional(Type.Boolean()),
      vel: Type.Optional(Type.Number({ description: "Default 350" })),
      acc: Type.Optional(Type.Number({ description: "Default 1000" })),
    }),
    async execute(_id, params) {
      const res = (await routeApi("/simulate", "POST", resolveSpec(params as Record<string, unknown>))) as {
        image_id?: string;
        [key: string]: unknown;
      };
      if (typeof res === "object" && res && "error" in res) {
        return textResult({ ok: false, error: String(res.error) });
      }
      const { image_id, image_url, ...rest } = res;
      // Fetch the rendered PNG and attach it as image content so the
      // vision model sees the planned vs. driven path.
      const png = await fetch(`${WEB}${image_url}`);
      if (!png.ok || !png.headers.get("content-type")?.startsWith("image/png")) {
        throw new Error("Dry-run preview image unavailable");
      }
      const b64 = Buffer.from(await png.arrayBuffer()).toString("base64");
      return {
        content: [
          {
            type: "text" as const,
            text: JSON.stringify({ ...rest, image_id }, null, 2),
          },
          {
            type: "image" as const,
            data: b64,
            mimeType: "image/png",
          },
        ],
        details: { image_id, image_url },
      };
    },
  });

  pi.registerTool({
    name: "update_route",
    label: "Update route (offsets)",
    description:
      "Corrects the route stored by the last plan_route (line/polyline only) " +
      "WITHOUT re-typing coordinates. Offsets dx/dz are ALWAYS relative to the " +
      "INITIAL point Pk from plan_route (absolute offset, not incremental): " +
      "new offset = previous offset + shift measured in check_route_on_image " +
      "(feature above the line -> dz positive). Points not listed keep their " +
      "previous offset. `insert` adds a point on the INITIAL leg Pk->Pk+1 at " +
      "fraction t (0..1, tile 'Strecke k→k+1' gives t) plus its own dx/dz; all " +
      "indices refer to the numbering BEFORE this call, afterwards points are " +
      "renumbered (see returned table). Max 10 points. Then call " +
      "check_route_on_image WITHOUT segments to review.",
    parameters: Type.Object({
      offsets: Type.Optional(Type.Array(Type.Object({
        point: Type.Integer({ minimum: 1, description: "Pk, 1-based" }),
        dx: Type.Optional(Type.Number({ description: "offset from initial x (default: keep x offset)" })),
        dz: Type.Optional(Type.Number({ description: "offset from initial z (default: keep z offset)" })),
      }))),
      insert: Type.Optional(Type.Array(Type.Object({
        after: Type.Integer({ minimum: 1, description: "insert on initial leg P_after -> P_after+1" }),
        t: Type.Optional(Type.Number({ minimum: 0.05, maximum: 0.95, description: "fraction along the leg, default 0.5" })),
        dx: Type.Optional(Type.Number()),
        dz: Type.Optional(Type.Number()),
      }))),
      reset: Type.Optional(Type.Boolean({ description: "set all offsets back to 0 first" })),
    }),
    async execute(_id, params) {
      if (!routeBase) {
        return textResult({ ok: false, error: "Keine initiale Route: zuerst plan_route mit line/polyline" });
      }
      const n = routeBase.length;
      const base = routeBase.map((p) => ({ ...p }));
      const offs = params.reset ? base.map(() => ({ x: 0, z: 0 })) : routeOffsets.map((o) => ({ ...o }));
      for (const o of params.offsets ?? []) {
        if (o.point < 1 || o.point > n) {
          return textResult({ ok: false, error: `Punkt P${o.point} existiert nicht (P1..P${n})` });
        }
        const cur = offs[o.point - 1];
        offs[o.point - 1] = { x: o.dx ?? cur.x, z: o.dz ?? cur.z };
      }
      const inserts = [...(params.insert ?? [])].sort(
        (a, b) => b.after - a.after || (b.t ?? 0.5) - (a.t ?? 0.5),
      );
      for (const ins of inserts) {
        if (ins.after < 1 || ins.after >= n) {
          return textResult({ ok: false, error: `Einfügen nach P${ins.after} nicht möglich (1..${n - 1})` });
        }
        const a = routeBase[ins.after - 1];
        const b = routeBase[ins.after];
        const t = ins.t ?? 0.5;
        // insert right after P_after (later inserts with smaller t end up first)
        base.splice(ins.after, 0, { x: a.x + t * (b.x - a.x), z: a.z + t * (b.z - a.z) });
        offs.splice(ins.after, 0, { x: ins.dx ?? 0, z: ins.dz ?? 0 });
      }
      if (base.length > 10) {
        return textResult({
          ok: false,
          error: `Route hätte ${base.length} Punkte (max. 10) – in zwei Läufe teilen oder Punkte auf geraden Stücken einsparen`,
        });
      }
      routeBase = base;
      routeOffsets = offs;
      return textResult({
        ok: true,
        points: routeTable(),
        next: "check_route_on_image ohne segments aufrufen und Korrektur reviewen",
      });
    },
  });

  pi.registerTool({
    name: "reset_route",
    label: "Reset route",
    description:
      "Discards the current route (initial points + all offsets) for a GRAVELY " +
      "wrong route: wrong feature traced, offsets > ~10 units in many tiles, " +
      "or the shape/course does not match the feature at all. Afterwards plan " +
      "a fresh initial route with plan_route (read the feature off the image " +
      "again). Do NOT use it for small corrections — those go via update_route.",
    parameters: Type.Object({
      reason: Type.String({ description: "Why the route is gravely wrong (short)" }),
    }),
    async execute(_id, params) {
      const had = routeBase ? routeTable() : null;
      routeBase = null;
      routeOffsets = [];
      return textResult({
        ok: true,
        discarded: had,
        reason: params.reason,
        next: "Merkmal neu ablesen (get_background_image), dann neue initiale Route mit plan_route",
      });
    },
  });

  pi.registerTool({
    name: "check_route_on_image",
    label: "Check route on background image",
    description:
      "ONE call checks the WHOLE planned route against the background image " +
      "(same spec as plan_route). Returns one PNG: an overview with the route " +
      "in red plus numbered zoom tiles (±8 units, 1-unit grid) centred on every " +
      "waypoint and on samples along every straight leg — the plant drives " +
      "straight lines between waypoints. In each tile the feature must lie " +
      "under the cyan line / yellow ring. Every tile and the overview carry " +
      "world-coordinate rulers (z left, x bottom; ticks per unit, numbers " +
      "every 5): read the feature's z at the ring's x and compare. " +
      "Use it while planning on image features, BEFORE simulate_route. Without " +
      "`segments` it checks the CURRENT route (plan_route + update_route). " +
      "Fix tiles that are off ONLY via update_route (offsets), then call it " +
      "again without segments. Tile 'Wegpunkt k' = point Pk. Read-only.",
    parameters: Type.Object({
      segments: Type.Optional(Type.Array(SegmentSchema, { minItems: 1 })),
      start: Type.Optional(
        Type.Object({ x: Type.Number(), z: Type.Number() }),
      ),
      closed: Type.Optional(Type.Boolean()),
    }),
    async execute(_id, params) {
      const spec = resolveSpec(params as Record<string, unknown>);
      const res = (await routeApi("/check-image", "POST", spec)) as {
        image_url?: string;
        [key: string]: unknown;
      };
      if (typeof res === "object" && res && "error" in res) {
        return textResult({ ok: false, error: String(res.error) });
      }
      const { image_url, image_id, ...rest } = res;
      const png = await fetch(`${WEB}${image_url}`);
      if (!png.ok) throw new Error("Feature-check image unavailable");
      const b64 = Buffer.from(await png.arrayBuffer()).toString("base64");
      const usesCurrent = !(params.segments && params.segments.length);
      return {
        content: [
          {
            type: "text" as const,
            text: JSON.stringify(
              { ...rest, route_points: usesCurrent ? routeTable() : undefined },
              null,
              2,
            ),
          },
          { type: "image" as const, data: b64, mimeType: "image/png" },
        ],
        details: { image_id, image_url },
      };
    },
  });

  pi.registerTool({
    name: "get_background_image",
    label: "Background image",
    description:
      "Returns the user-uploaded background image (e.g. a photo of a " +
      "workpiece) WITH a world-coordinate grid. The image is stretched " +
      "over the whole safe box: left edge x=30, right edge x=260, top " +
      "z=150, bottom z=35; grid lines every 10, labels every 50. Use it to " +
      "locate features (edges, seams, marks) in world coordinates before " +
      "planning a path on them. Read-only, never touches the robot.",
    parameters: Type.Object({}),
    async execute() {
      const info = (await (await fetch(`${WEB}/api/background/info`)).json()) as {
        has?: boolean;
        name?: string;
      };
      if (!info.has) return textResult({ has: false, note: "Kein Hintergrundbild hochgeladen" });
      const img = await fetch(`${WEB}/api/background/grid`);
      if (!img.ok) return textResult({ has: false, note: "Hintergrundbild nicht verfügbar" });
      const b64 = Buffer.from(await img.arrayBuffer()).toString("base64");
      return {
        content: [
          {
            type: "text" as const,
            text: JSON.stringify({
              has: true,
              name: info.name,
              mapping: "x = 30 + 230·(px/920), z = 150 − 115·(py/460) (image 920×460, 4 px per unit)",
            }),
          },
          { type: "image" as const, data: b64, mimeType: img.headers.get("content-type") || "image/png" },
        ],
        details: { name: info.name },
      };
    },
  });

  pi.registerTool({
    name: "compare_run",
    label: "Compare run vs. plan",
    description:
      "Compares the MEASURED position samples of the last real run (10 Hz, " +
      "read-only) against the last dry-run simulation: spatial deviation " +
      "(max/mean) and duration ratio (measurable sim-vs-plant calibration). " +
      "Call after wait_for_condition with condition=done and path_run=true succeeds.",
    parameters: Type.Object({}),
    async execute() {
      return textResult(await routeApi("/actual"));
    },
  });

  pi.registerTool({
    name: "jog_axis",
    label: "Jog axis",
    description:
      "Sets manual jogging of axis x or z. enabled=false disables both directions. " +
      "Jogging moves the axes in real time - use short bursts and verify with positions.",
    parameters: Type.Object({
      axis: Axis,
      positive: Type.Boolean({ description: "Direction: true = positive, false = negative" }),
      enabled: Type.Optional(Type.Boolean({ description: "Default true" })),
    }),
    async execute(_id, params) {
      return textResult(await robotApi("/jog", "POST", params));
    },
  });

  pi.registerTool({
    name: "pulse_gripper",
    label: "Pulse gripper",
    description:
      "Pulses the gripper command (g_xPulseGripper) for the given duration in seconds, then releases it.",
    parameters: Type.Object({
      duration: Type.Optional(Type.Number({ description: "Seconds, default 0.5" })),
    }),
    async execute(_id, params) {
      return textResult(await robotApi("/gripper", "POST", params));
    },
  });

  pi.registerTool({
    name: "release_motor_brake",
    label: "Release motor brake",
    description: "Releases the motor brake (g_xMotorBrakeRelease).",
    parameters: Type.Object({}),
    async execute() {
      return textResult(await robotApi("/brake/release", "POST"));
    },
  });

  pi.registerTool({
    name: "reset_error",
    label: "Reset error",
    description: "Requests an error reset (g_xErrorReset).",
    parameters: Type.Object({}),
    async execute() {
      return textResult(await robotApi("/error/reset", "POST"));
    },
  });

  pi.registerTool({
    name: "wait_for_condition",
    label: "Wait for condition",
    description:
      "Polls the PLC until a condition is met or the timeout elapses: " +
      "done (path run finished), home_ready (home position available), no_fault (no error/warning). " +
      "Returns the outcome, never throws on timeout. For a live jog drawing the " +
      "result includes running, end and, on abort, reason (report it verbatim; " +
      "if running is still true, wait again instead of restarting).",
    parameters: Type.Object({
      condition: Type.Union([
        Type.Literal("done"),
        Type.Literal("home_ready"),
        Type.Literal("no_fault"),
      ]),
      timeout: Type.Optional(Type.Number({ description: "Seconds, default 30" })),
      path_run: Type.Optional(Type.Boolean({ description: "Set true for done after start_path_run; omit for homing" })),
    }),
    async execute(_id, params) {
      return textResult(await robotApi("/wait", "POST", params));
    },
  });
}
