export const MarkerSeverity = {
  Error: 8,
  Warning: 4,
  Info: 2,
} as const;

export const KeyMod = {
  CtrlCmd: 2048,
} as const;

export const KeyCode = {
  KeyS: 49,
} as const;

export const editor = {
  create: () => ({
    getModel: () => null,
    getValue: () => "",
    setValue: () => {},
    onDidChangeModelContent: () => ({ dispose: () => {} }),
    addCommand: () => {},
    revealLineInCenter: () => {},
    setPosition: () => {},
    focus: () => {},
    dispose: () => {},
  }),
  setModelLanguage: () => {},
  setModelMarkers: () => {},
};
