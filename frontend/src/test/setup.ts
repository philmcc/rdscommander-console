import "@testing-library/jest-dom/vitest";

// jsdom has neither of these; cmdk (the launcher's list) needs both.
class ResizeObserverStub {
  observe(): void {}
  unobserve(): void {}
  disconnect(): void {}
}
globalThis.ResizeObserver ??= ResizeObserverStub;
Element.prototype.scrollIntoView ??= () => {};
