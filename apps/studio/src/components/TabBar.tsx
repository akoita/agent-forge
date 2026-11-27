import type { OpenTab } from "../types/workspace";

interface TabBarProps {
  tabs: OpenTab[];
  activePath: string | null;
  saving: boolean;
  onSelectTab: (path: string) => void;
  onCloseTab: (path: string) => void;
  onSaveActive: () => void;
  onRevertActive: () => void;
}

export function TabBar({
  tabs,
  activePath,
  saving,
  onSelectTab,
  onCloseTab,
  onSaveActive,
  onRevertActive,
}: TabBarProps) {
  const activeTab = tabs.find((tab) => tab.path === activePath) ?? null;

  return (
    <div className="studio-tabbar">
      <div className="tab-strip" role="tablist" aria-label="Open files">
        {tabs.map((tab) => {
          const isActive = tab.path === activePath;
          const label = tab.path.split("/").pop() ?? tab.path;
          return (
            <div
              key={tab.path}
              className={`tab-item${isActive ? " is-active" : ""}`}
              role="tab"
              aria-selected={isActive}
            >
              <button
                type="button"
                className="tab-select"
                onClick={() => onSelectTab(tab.path)}
                title={tab.path}
              >
                <span>{label}</span>
                {tab.dirty ? (
                  <span className="dirty-dot" aria-label="Unsaved changes">
                    ●
                  </span>
                ) : null}
              </button>
              <button
                type="button"
                className="tab-close"
                onClick={() => onCloseTab(tab.path)}
                aria-label={`Close ${label}`}
              >
                ×
              </button>
            </div>
          );
        })}
      </div>

      {activeTab ? (
        <div className="tab-actions">
          <button
            type="button"
            className="btn-secondary"
            disabled={!activeTab.dirty || saving}
            onClick={onRevertActive}
          >
            Revert
          </button>
          <button
            type="button"
            className="btn-primary"
            disabled={!activeTab.dirty || saving}
            onClick={onSaveActive}
          >
            {saving ? "Saving..." : "Save"}
          </button>
        </div>
      ) : null}
    </div>
  );
}
