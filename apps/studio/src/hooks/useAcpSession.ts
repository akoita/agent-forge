import { useCallback, useEffect, useRef, useState } from "react";
import type { ToolCallEntry } from "../components/ToolCallCard";
import type {
  ACPConnectionState,
  ACPReadFileHandler,
  ACPTransport,
  AvailableCommand,
  PermissionRequest,
  PlanEntry,
  SessionConfigOption,
  SessionInfo,
  SessionUpdate,
} from "../types/acp";

export type TimelineEntry =
  | { kind: "user"; id: string; text: string }
  | { kind: "assistant"; id: string; text: string }
  | { kind: "thought"; id: string; text: string }
  | ToolCallEntry;

interface UseAcpSessionOptions {
  acp: ACPTransport;
  onTurnCompleted?: () => Promise<void> | void;
  readFileBuffer?: ACPReadFileHandler;
}

export function useAcpSession({
  acp,
  onTurnCompleted,
  readFileBuffer,
}: UseAcpSessionOptions) {
  const [connectionState, setConnectionState] = useState<ACPConnectionState>(
    acp.getConnectionState(),
  );
  const [sessions, setSessions] = useState<SessionInfo[]>([]);
  const [activeSessionId, setActiveSessionId] = useState<string | null>(null);
  const [configOptions, setConfigOptions] = useState<SessionConfigOption[]>([]);
  const [availableCommands, setAvailableCommands] = useState<AvailableCommand[]>([]);
  const [planEntries, setPlanEntries] = useState<PlanEntry[]>([]);
  const [timeline, setTimeline] = useState<TimelineEntry[]>([]);
  const [permissionQueue, setPermissionQueue] = useState<PermissionRequest[]>([]);
  const [promptRunning, setPromptRunning] = useState(false);
  const [agentError, setAgentError] = useState("");

  const activeSessionRef = useRef<string | null>(null);
  const entryCounterRef = useRef(0);
  const workspaceRootRef = useRef("");

  useEffect(() => {
    activeSessionRef.current = activeSessionId;
  }, [activeSessionId]);

  useEffect(() => {
    acp.setReadFileHandler(readFileBuffer ?? null);
    return () => acp.setReadFileHandler(null);
  }, [acp, readFileBuffer]);

  const nextId = useCallback((prefix: string): string => {
    entryCounterRef.current += 1;
    return `${prefix}-${entryCounterRef.current}`;
  }, []);

  const applySessionUpdate = useCallback(
    (sessionId: string, update: SessionUpdate) => {
      if (activeSessionRef.current && sessionId !== activeSessionRef.current) {
        return;
      }
      switch (update.sessionUpdate) {
        case "user_message_chunk": {
          const text = update.content.text;
          setTimeline((prev) => {
            const last = prev[prev.length - 1];
            if (last && last.kind === "user") {
              return [...prev.slice(0, -1), { ...last, text: last.text + text }];
            }
            return [...prev, { kind: "user", id: nextId("usr"), text }];
          });
          break;
        }
        case "agent_message_chunk": {
          const text = update.content.text;
          setTimeline((prev) => {
            const last = prev[prev.length - 1];
            if (last && last.kind === "assistant") {
              return [...prev.slice(0, -1), { ...last, text: last.text + text }];
            }
            return [...prev, { kind: "assistant", id: nextId("ast"), text }];
          });
          break;
        }
        case "agent_thought_chunk": {
          const text = update.content.text;
          setTimeline((prev) => {
            const last = prev[prev.length - 1];
            if (last && last.kind === "thought") {
              return [...prev.slice(0, -1), { ...last, text: last.text + text }];
            }
            return [...prev, { kind: "thought", id: nextId("tht"), text }];
          });
          break;
        }
        case "tool_call": {
          setTimeline((prev) => {
            const existingIdx = prev.findIndex(
              (item) => item.kind === "tool" && item.toolCallId === update.toolCallId,
            );
            const nextEntry: ToolCallEntry = {
              kind: "tool",
              id: existingIdx >= 0 ? prev[existingIdx].id : nextId("tool"),
              toolCallId: update.toolCallId,
              title: update.title,
              toolKind: update.kind,
              status: update.status,
              rawInput: update.rawInput,
              content: [],
              locations: update.locations ?? [],
            };
            if (existingIdx >= 0) {
              const copy = [...prev];
              copy[existingIdx] = nextEntry;
              return copy;
            }
            return [...prev, nextEntry];
          });
          break;
        }
        case "tool_call_update": {
          setTimeline((prev) =>
            prev.map((item) => {
              if (item.kind !== "tool" || item.toolCallId !== update.toolCallId) {
                return item;
              }
              return {
                ...item,
                status: update.status ?? item.status,
                content:
                  update.content && update.content.length > 0 ? update.content : item.content,
                locations:
                  update.locations && update.locations.length > 0
                    ? update.locations
                    : item.locations,
                rawOutput: update.rawOutput !== undefined ? update.rawOutput : item.rawOutput,
              };
            }),
          );
          break;
        }
        case "plan": {
          setPlanEntries(update.entries);
          break;
        }
        case "available_commands_update": {
          setAvailableCommands(update.availableCommands);
          break;
        }
        case "config_option_update": {
          setConfigOptions(update.configOptions);
          break;
        }
        case "current_mode_update": {
          setConfigOptions((prev) =>
            prev.map((opt) =>
              opt.id === "mode" ? { ...opt, currentValue: update.currentModeId } : opt,
            ),
          );
          break;
        }
      }
    },
    [nextId],
  );

  useEffect(() => {
    const unsubUpdate = acp.onSessionUpdate(applySessionUpdate);
    const unsubPerm = acp.onPermissionRequest((req) => {
      setPermissionQueue((prev) => [...prev, req]);
    });
    const unsubConn = acp.onConnectionChange((state, detail) => {
      setConnectionState(state);
      if (state === "error" && detail) {
        setAgentError(detail);
      }
    });
    return () => {
      unsubUpdate();
      unsubPerm();
      unsubConn();
    };
  }, [acp, applySessionUpdate]);

  const startSessionFlow = useCallback(
    async (workspaceRoot: string) => {
      workspaceRootRef.current = workspaceRoot;
      setAgentError("");
      await acp.connect();
      await acp.initialize();

      const listResp = await acp.listSessions(workspaceRoot || undefined);
      setSessions(listResp.sessions);

      if (workspaceRoot) {
        const created = await acp.newSession(workspaceRoot);
        activeSessionRef.current = created.sessionId;
        setActiveSessionId(created.sessionId);
        setConfigOptions(created.configOptions);
        const refreshed = await acp.listSessions(workspaceRoot);
        setSessions(refreshed.sessions);
      }
    },
    [acp],
  );

  const selectSession = useCallback(
    async (sessionId: string) => {
      if (!sessionId) {
        return;
      }
      setAgentError("");
      setTimeline([]);
      setPlanEntries([]);
      setPermissionQueue([]);
      activeSessionRef.current = sessionId;
      setActiveSessionId(sessionId);
      try {
        const result = await acp.loadSession(sessionId, workspaceRootRef.current || undefined);
        setConfigOptions(result.configOptions);
      } catch (err) {
        setAgentError(err instanceof Error ? err.message : "Failed to load ACP session.");
      }
    },
    [acp],
  );

  const createNewSession = useCallback(async () => {
    const cwd = workspaceRootRef.current;
    if (!cwd) {
      setAgentError("Workspace root is unavailable; reconnect bridge first.");
      return;
    }
    setAgentError("");
    setTimeline([]);
    setPlanEntries([]);
    setPermissionQueue([]);
    try {
      const created = await acp.newSession(cwd);
      activeSessionRef.current = created.sessionId;
      setActiveSessionId(created.sessionId);
      setConfigOptions(created.configOptions);
      const listResp = await acp.listSessions(cwd);
      setSessions(listResp.sessions);
    } catch (err) {
      setAgentError(err instanceof Error ? err.message : "Failed to create ACP session.");
    }
  }, [acp]);

  const submitPrompt = useCallback(
    async (text: string) => {
      const trimmed = text.trim();
      const sessionId = activeSessionRef.current;
      if (!trimmed || !sessionId) {
        return;
      }
      setAgentError("");
      setTimeline((prev) => [...prev, { kind: "user", id: nextId("usr"), text: trimmed }]);
      setPromptRunning(true);
      try {
        await acp.sendPrompt(sessionId, trimmed);
        if (workspaceRootRef.current) {
          const listResp = await acp.listSessions(workspaceRootRef.current);
          setSessions(listResp.sessions);
        }
        await onTurnCompleted?.();
      } catch (err) {
        setAgentError(err instanceof Error ? err.message : "ACP prompt failed.");
      } finally {
        setPromptRunning(false);
        setPermissionQueue((prev) => prev.filter((item) => item.sessionId !== sessionId));
      }
    },
    [acp, nextId, onTurnCompleted],
  );

  const cancelTurn = useCallback(() => {
    const sessionId = activeSessionRef.current;
    if (!sessionId) {
      return;
    }
    acp.cancelSession(sessionId);
  }, [acp]);

  const changeConfigOption = useCallback(
    async (configId: string, value: string) => {
      const sessionId = activeSessionRef.current;
      if (!sessionId) {
        return;
      }
      setAgentError("");
      try {
        const res = await acp.setConfigOption(sessionId, configId, value);
        setConfigOptions(res.configOptions);
      } catch (err) {
        setAgentError(err instanceof Error ? err.message : "Failed to update option.");
      }
    },
    [acp],
  );

  const respondPermission = useCallback(
    (requestId: number, optionId: string) => {
      acp.respondToPermission(requestId, { outcome: "selected", optionId });
      setPermissionQueue((prev) => prev.filter((item) => item.requestId !== requestId));
    },
    [acp],
  );

  const reset = useCallback(() => {
    acp.disconnect();
    workspaceRootRef.current = "";
    activeSessionRef.current = null;
    setActiveSessionId(null);
    setSessions([]);
    setConfigOptions([]);
    setAvailableCommands([]);
    setPlanEntries([]);
    setTimeline([]);
    setPermissionQueue([]);
    setPromptRunning(false);
    setAgentError("");
  }, [acp]);

  return {
    connectionState,
    sessions,
    activeSessionId,
    configOptions,
    availableCommands,
    planEntries,
    timeline,
    pendingPermission: permissionQueue[0] ?? null,
    promptRunning,
    agentError,
    setAgentError,
    startSessionFlow,
    selectSession,
    createNewSession,
    submitPrompt,
    cancelTurn,
    changeConfigOption,
    respondPermission,
    reset,
  };
}
