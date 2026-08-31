/// <reference types="vite/client" />

export {};

declare global {
  interface Window {
    __fanControl?: {
      call: (method: string, params?: Record<string, unknown>) => Promise<unknown>;
      event: (name: string, payload: unknown) => void;
    };
    webkit?: {
      messageHandlers?: {
        fan?: { postMessage: (payload: unknown) => Promise<unknown> };
      };
    };
  }
}
