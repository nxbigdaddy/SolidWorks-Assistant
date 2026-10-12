import { Type } from "typebox";

export default function (pi) {
  const baseUrl = process.env.SW_ASSISTANT_PI_API_BASE;
  const model = process.env.SW_ASSISTANT_PI_MODEL;
  if (!baseUrl || !model) {
    throw new Error("SolidWorks Assistant Pi provider is not configured");
  }
  const requestBody = JSON.parse(process.env.SW_ASSISTANT_PI_REQUEST_BODY || "{}");
  // Only the host's endpoint compatibility switches are accepted, never
  // arbitrary provider settings, tool definitions or model-generated payloads.
  const requestExtras = {};
  if (requestBody.thinking?.type === "disabled") requestExtras.thinking = { type: "disabled" };
  if (requestBody.enable_thinking === false) requestExtras.enable_thinking = false;
  pi.on("before_provider_request", async (event) => {
    if (Object.keys(requestExtras).length && event.payload && typeof event.payload === "object") {
      return { ...event.payload, ...requestExtras };
    }
  });

  pi.registerProvider("solidworks-assistant", {
    baseUrl,
    apiKey: "$SW_ASSISTANT_PI_API_KEY",
    api: "openai-completions",
    models: [
      {
        id: model,
        name: model,
        reasoning: false,
        input: ["text"],
        cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 },
        contextWindow: 128000,
        maxTokens: 8192,
      },
    ],
  });

  // Only fixed application tools are enabled. CAD stages go to the local
  // executor; this extension has no Shell, files, DLL or direct COM tools.
  const bridgeId = process.env.SW_ASSISTANT_PI_BRIDGE_ID;
  const stageBridgeId = process.env.SW_ASSISTANT_PI_STAGE_BRIDGE_ID;
  if (bridgeId || stageBridgeId) {
    let attempts = 0;
    let stageAttempts = 0;
    let stageLimit = 2;
    let stageAllowed = false;
    let stageEpoch = "";
    let investigationClosed = !bridgeId;
    let stageClosed = true;
    let decisionOnly = false;
    const decisionInstruction = "宿主调查阶段已结束，且本轮其余工具额度或安全边界已结束。本轮不要再调用工具。根据已有证据返回规定 JSON 决策；若已经通过阶段工具执行，不得把同一 act 再返回重放，使用最新结果申请验收或等待下一轮。未知结果必须暂停，不能猜测成功。";
    const refreshTools = () => {
      const active = [];
      if (bridgeId && !investigationClosed) active.push("sw_investigate");
      if (stageBridgeId && stageAllowed && !stageClosed) active.push("sw_model_stage");
      decisionOnly = active.length === 0;
      pi.setActiveTools(active);
    };
    const finishInvestigation = () => {
      investigationClosed = true;
      refreshTools();
    };
    const resetInvestigation = (text = "") => {
      attempts = 0;
      stageAttempts = 0;
      stageAllowed = false;
      stageLimit = 2;
      stageEpoch = "";
      investigationClosed = !bridgeId;
      try {
        const protocol = JSON.parse(text).host_stage_protocol;
        stageAllowed = Boolean(stageBridgeId && protocol?.allowed === true);
        if (protocol?.max_attempts === 1) stageLimit = 1;
        if (typeof protocol?.epoch === "string") stageEpoch = protocol.epoch;
      } catch { /* Ordinary read-only task prompts need no stage protocol. */ }
      stageClosed = !stageAllowed;
      refreshTools();
    };
    // input(source=rpc) runs once per outer host prompt, not once per internal
    // tool/LLM turn. Never replenish the allowance on turn_start.
    pi.on("input", async (event) => {
      if (event.source === "rpc" && !event.streamingBehavior) resetInvestigation(event.text);
    });
    pi.on("tool_execution_start", async (event) => {
      if (event.toolName === "sw_model_stage") {
        stageAttempts += 1;
        if (stageAttempts >= stageLimit) {
          stageClosed = true;
          refreshTools();
        }
        return;
      }
      attempts += 1;
      // This event precedes schema validation, so invalid tool arguments and
      // unknown tools cannot bypass the independent total-attempt allowance.
      if (attempts >= 10) finishInvestigation();
    });
    pi.on("context", async (event) => {
      // Invalid-schema/unknown calls skip Pi's tool_result hook altogether.
      // Context runs before the next model request even on that path, so the
      // decision-only instruction cannot depend on successful tool execution.
      if (decisionOnly) return { messages: [...event.messages, {
        role: "user", content: [{ type: "text", text: decisionInstruction }], timestamp: Date.now(),
      }] };
    });
    pi.on("tool_result", async (event) => {
      let failed = event.isError;
      if (["sw_investigate", "sw_model_stage"].includes(event.toolName)) {
        try {
          const first = event.content.find((part) => part.type === "text");
          if (first && JSON.parse(first.text).ok === false) failed = true;
        } catch { /* Pi validation errors are already marked isError. */ }
      }
      return {
        isError: failed,
        ...(decisionOnly ? { content: [...event.content, { type: "text", text: decisionInstruction }] } : {}),
      };
    });
    if (bridgeId) pi.registerTool({
      name: "sw_investigate",
      label: "调查 SolidWorks 任务",
      description: "主动读取能力、模型、实体外形尺寸、候选基准面/平面面、草图、特征或失败诊断。只读；不会打开文档或启动 SolidWorks。使用结果继续调查或决定下一阶段。",
      parameters: Type.Object({
        tool: Type.Union([
          "session.capabilities", "model.observe", "model.inspect", "model.references", "model.measure_extents",
          "sketch.inspect", "feature.inspect", "diagnostics.recent",
        ].map((name) => Type.Literal(name))),
        arguments_json: Type.String({ maxLength: 8000, description: "工具参数 JSON 对象；无参数时传 {}。" }),
      }),
      async execute(_id, params, signal, _onUpdate, ctx) {
        if (signal?.aborted) throw new Error("任务已停止");
        const args = JSON.parse(params.arguments_json);
        if (!args || Array.isArray(args) || typeof args !== "object") {
          throw new Error("调查参数必须为 JSON 对象");
        }
        const value = await ctx.ui.input(
          bridgeId, JSON.stringify({ tool: params.tool, arguments: args }),
          { signal, timeout: 120000 },
        );
        if (value === undefined || signal?.aborted) throw new Error("调查已取消或宿主不可用");
        const envelope = JSON.parse(value);
        if (envelope.host_control?.decision_only === true || envelope.host_control?.investigation_closed === true) {
          finishInvestigation();
        }
        // Host observations are data, not executable code. Lifecycle control
        // comes only from the host envelope and is not exposed as model input.
        return { content: [{ type: "text", text: JSON.stringify(envelope.result) }] };
      },
    });
    if (stageBridgeId) pi.registerTool({
      name: "sw_model_stage",
      label: "执行受控建模阶段",
      description: "提交完整 action=act 初始化或 trial 小阶段；宿主白名单执行器负责校验、实测与恢复。每轮最多两次。不要重放已成功或结果未知的请求；准备阶段禁用。",
      parameters: Type.Object({
        decision_json: Type.String({ maxLength: 32768, description: "完整 TaskDecision JSON 对象，action 必须为 act。" }),
      }),
      async execute(id, params, signal, _onUpdate, ctx) {
        const epoch = stageEpoch;
        if (signal?.aborted || !stageAllowed || stageAttempts > stageLimit || !epoch) {
          throw new Error("阶段执行未获宿主允许、已停止或预算耗尽");
        }
        if (new TextEncoder().encode(params.decision_json).length > 32768) {
          throw new Error("阶段决策超过 32 KB");
        }
        const decision = JSON.parse(params.decision_json);
        if (!decision || Array.isArray(decision) || typeof decision !== "object" || decision.action !== "act") {
          throw new Error("阶段工具需要完整 action=act 决策对象");
        }
        const value = await ctx.ui.input(stageBridgeId, JSON.stringify({
          request_id: id, epoch, decision, expires_at: Date.now() + 630000,
        }), { signal, timeout: 630000 });
        if (value === undefined || signal?.aborted || epoch !== stageEpoch) {
          stageClosed = true;
          refreshTools();
          return { isError: true, content: [{ type: "text", text: JSON.stringify({
            ok: false, status: "unknown", request_id: id, requires_review: true,
            error: "阶段已提交但桥接中断或超时，结果未知；不得换 request_id 重放，等待宿主安全检查点。",
          }) }] };
        }
        const envelope = JSON.parse(value);
        if (envelope.host_control?.stage_closed === true) stageClosed = true;
        if (envelope.host_control?.requires_review === true) {
          stageClosed = true;
          investigationClosed = true;
        }
        refreshTools();
        return { content: [{ type: "text", text: JSON.stringify(envelope.result) }] };
      },
    });
    // The host supplies an explicit --tools sw_investigate allowlist. Keep the
    // active set equally narrow after session startup or restored session state.
    pi.on("session_start", async () => {
      resetInvestigation();
    });
  }
}
