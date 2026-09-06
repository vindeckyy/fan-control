import { create } from "zustand";
export const useUI = create<{
  collapsed: boolean;
  palette: boolean;
  toast: string | null;
  selectedCurve: string | null;
}>(() => ({
  collapsed: localStorage.getItem("sidebar-collapsed") === "true",
  palette: false,
  toast: null,
  selectedCurve: null,
}));
export function toggleSidebar() {
  const collapsed = !useUI.getState().collapsed;
  localStorage.setItem("sidebar-collapsed", String(collapsed));
  useUI.setState({ collapsed });
}
let toastTimer: ReturnType<typeof setTimeout>;
export function tell(toast: string) {
  useUI.setState({ toast });
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => useUI.setState({ toast: null }), 6000);
}
