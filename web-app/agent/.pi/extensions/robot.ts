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
 *  PI_LLM_MODEL    model id (default qwen)
 */
import { type ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { Type } from "typebox";

const WEB = process.env.PI_WEB_APP_URL ?? "http://127.0.0.1:8080";
const LLM_BASE = process.env.PI_LLM_BASE ?? "http://10.30.35.110:4001/v1";
const LLM_MODEL = process.env.PI_LLM_MODEL ?? "qwen";

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
        name: "Local Qwen (vLLM)",
        reasoning: true,
        input: ["text"],
        cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 },
        contextWindow: 32768,
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
      "values, override, path velocity). Raw integer units; engineering scale is unknown.",
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
      "Starts a path run (g_xPathExecute). Optionally sets path velocity output " +
      "and override beforehand. The profile index is validated but NOT transmitted: " +
      "how the PLC selects the active profile is undocumented.",
    parameters: Type.Object({
      profile_index: Type.Optional(Type.Number({ description: "Profile index, 1..10" })),
      path_vel_out: Type.Optional(Type.Number()),
      override: Type.Optional(Type.Number()),
    }),
    async execute(_id, params) {
      return textResult(await robotApi("/path/start", "POST", params));
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
      description: `${verb} the current path run.`,
      parameters: Type.Object({}),
      async execute() {
        return textResult(await robotApi(path, "POST"));
      },
    });
  }

  pi.registerTool({
    name: "start_homing",
    label: "Start homing",
    description: "Starts the homing procedure (g_xHomeExecute).",
    parameters: Type.Object({}),
    async execute() {
      return textResult(await robotApi("/home", "POST"));
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
      "Returns the outcome, never throws on timeout.",
    parameters: Type.Object({
      condition: Type.Union([
        Type.Literal("done"),
        Type.Literal("home_ready"),
        Type.Literal("no_fault"),
      ]),
      timeout: Type.Optional(Type.Number({ description: "Seconds, default 30" })),
    }),
    async execute(_id, params) {
      return textResult(await robotApi("/wait", "POST", params));
    },
  });
}
