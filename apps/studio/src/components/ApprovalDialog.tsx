import type { PermissionRequest } from "../types/acp";

interface ApprovalDialogProps {
  request: PermissionRequest;
  onRespond: (requestId: number, optionId: string) => void;
}

function formatInputPreview(rawInput: unknown): string {
  if (rawInput === undefined || rawInput === null) {
    return "";
  }
  if (typeof rawInput === "string") {
    return rawInput;
  }
  try {
    return JSON.stringify(rawInput, null, 2);
  } catch {
    return String(rawInput);
  }
}

export function ApprovalDialog({ request, onRespond }: ApprovalDialogProps) {
  const preview = formatInputPreview(request.toolCall.rawInput);

  return (
    <div className="approval-dialog" role="dialog" aria-label="Tool permission request">
      <div className="approval-header">
        <span className="approval-badge">Approval Required</span>
        <span className="approval-kind">{request.toolCall.kind}</span>
      </div>
      <div className="approval-title">{request.toolCall.title}</div>
      {preview ? <pre className="approval-input">{preview}</pre> : null}
      <div className="approval-actions">
        {request.options.map((opt) => {
          const isReject = opt.kind.startsWith("reject");
          const isOnce = opt.kind === "allow_once";
          const btnClass = isReject
            ? "btn-danger"
            : isOnce
              ? "btn-primary"
              : "btn-secondary";
          return (
            <button
              key={opt.optionId}
              type="button"
              className={btnClass}
              onClick={() => onRespond(request.requestId, opt.optionId)}
            >
              {opt.name}
            </button>
          );
        })}
      </div>
    </div>
  );
}
