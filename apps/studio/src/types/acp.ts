export interface ImplementationInfo {
  name: string;
  version: string;
  title?: string | null;
}

export interface InitializeResult {
  protocolVersion: number;
  agentCapabilities: Record<string, unknown>;
  agentInfo?: ImplementationInfo | null;
  authMethods?: Array<{ id: string; name: string; description?: string | null }>;
}

export interface SessionInfo {
  sessionId: string;
  cwd: string;
  title?: string | null;
  updatedAt?: string | null;
  _meta?: {
    provider?: string;
    model?: string;
    turnCount?: number;
  };
}

export interface SessionListResult {
  sessions: SessionInfo[];
  nextCursor?: string | null;
}

export interface SessionMode {
  id: string;
  name: string;
  description?: string | null;
}

export interface SessionModeState {
  availableModes: SessionMode[];
  currentModeId: string;
}

export interface SessionConfigSelectOption {
  value: string;
  name: string;
  description?: string | null;
}

export interface SessionConfigOption {
  id: string;
  name: string;
  description?: string | null;
  category?: string | null;
  type: "select";
  currentValue: string;
  options: SessionConfigSelectOption[];
}

export interface SessionNewResult {
  sessionId: string;
  modes?: SessionModeState | null;
  configOptions: SessionConfigOption[];
}

export interface SessionLoadResult {
  modes?: SessionModeState | null;
  configOptions: SessionConfigOption[];
}

export interface SessionPromptResult {
  stopReason: "end_turn" | "max_tokens" | "max_turn_requests" | "refusal" | "cancelled";
  _meta?: {
    runId?: string;
    runState?: string;
  };
}

export interface TextContent {
  type: "text";
  text: string;
}

export interface ContentToolCallContent {
  type: "content";
  content: TextContent;
}

export interface DiffToolCallContent {
  type: "diff";
  path: string;
  oldText?: string | null;
  newText: string;
}

export type ToolCallContentBlock = ContentToolCallContent | DiffToolCallContent;

export interface ToolCallLocation {
  path: string;
  line?: number | null;
}

export interface PlanEntry {
  content: string;
  priority: "high" | "medium" | "low";
  status: "pending" | "in_progress" | "completed";
}

export interface AvailableCommand {
  name: string;
  description: string;
  input?: { hint: string } | null;
}

export type ToolCallKind =
  | "read"
  | "edit"
  | "delete"
  | "move"
  | "search"
  | "execute"
  | "think"
  | "fetch"
  | "other";

export type ToolCallStatus = "pending" | "in_progress" | "completed" | "failed";

export type SessionUpdate =
  | {
      sessionUpdate: "user_message_chunk";
      content: TextContent;
    }
  | {
      sessionUpdate: "agent_message_chunk";
      content: TextContent;
    }
  | {
      sessionUpdate: "agent_thought_chunk";
      content: TextContent;
    }
  | {
      sessionUpdate: "tool_call";
      toolCallId: string;
      title: string;
      kind: ToolCallKind;
      status: ToolCallStatus;
      rawInput?: unknown;
      locations?: ToolCallLocation[];
    }
  | {
      sessionUpdate: "tool_call_update";
      toolCallId: string;
      status?: ToolCallStatus | null;
      content?: ToolCallContentBlock[];
      locations?: ToolCallLocation[];
      rawOutput?: unknown;
    }
  | {
      sessionUpdate: "plan";
      entries: PlanEntry[];
    }
  | {
      sessionUpdate: "available_commands_update";
      availableCommands: AvailableCommand[];
    }
  | {
      sessionUpdate: "current_mode_update";
      currentModeId: string;
    }
  | {
      sessionUpdate: "config_option_update";
      configOptions: SessionConfigOption[];
    };

export interface PermissionOption {
  optionId: string;
  name: string;
  kind: "allow_once" | "allow_always" | "reject_once" | "reject_always";
}

export interface PermissionRequest {
  requestId: number;
  sessionId: string;
  toolCall: {
    toolCallId?: string;
    title: string;
    kind: string;
    status?: string;
    rawInput?: unknown;
  };
  options: PermissionOption[];
}

export type PermissionOutcome =
  | { outcome: "selected"; optionId: string }
  | { outcome: "cancelled" };

export type ACPConnectionState = "disconnected" | "connecting" | "connected" | "error";

export type ACPUpdateListener = (sessionId: string, update: SessionUpdate) => void;
export type ACPPermissionListener = (request: PermissionRequest) => void;
export type ACPConnectionListener = (state: ACPConnectionState, detail?: string) => void;
export type ACPReadFileHandler = (
  path: string,
  line?: number | null,
  limit?: number | null,
) => Promise<string | null> | string | null;

export interface ACPTransport {
  connect(): Promise<void>;
  disconnect(): void;
  getConnectionState(): ACPConnectionState;
  initialize(): Promise<InitializeResult>;
  listSessions(cwd?: string, cursor?: string): Promise<SessionListResult>;
  newSession(cwd: string): Promise<SessionNewResult>;
  loadSession(sessionId: string, cwd?: string): Promise<SessionLoadResult>;
  sendPrompt(sessionId: string, text: string): Promise<SessionPromptResult>;
  cancelSession(sessionId: string): void;
  setConfigOption(
    sessionId: string,
    configId: string,
    value: string,
  ): Promise<{ configOptions: SessionConfigOption[] }>;
  setMode(sessionId: string, modeId: string): Promise<void>;
  respondToPermission(requestId: number, outcome: PermissionOutcome): void;
  onSessionUpdate(listener: ACPUpdateListener): () => void;
  onPermissionRequest(listener: ACPPermissionListener): () => void;
  onConnectionChange(listener: ACPConnectionListener): () => void;
  setReadFileHandler(handler: ACPReadFileHandler | null): void;
}
