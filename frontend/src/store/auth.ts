import { create } from "zustand";
import { persist, createJSONStorage } from "zustand/middleware";

interface AuthState {
  token: string | null;
  username: string | null;
  login: (token: string, username: string) => void;
  logout: () => void;
}

// The token lives in sessionStorage (cleared when the tab closes). No exchange
// credentials ever reach the browser: they exist only in the backend environment.
export const useAuth = create<AuthState>()(
  persist(
    (set) => ({
      token: null,
      username: null,
      login: (token, username) => set({ token, username }),
      logout: () => set({ token: null, username: null }),
    }),
    { name: "tb-auth", storage: createJSONStorage(() => sessionStorage) },
  ),
);

interface UiState {
  theme: "dark" | "light";
  toggleTheme: () => void;
}

export const useUi = create<UiState>()(
  persist(
    (set) => ({ theme: "dark", toggleTheme: () => set((s) => ({ theme: s.theme === "dark" ? "light" : "dark" })) }),
    { name: "tb-ui" },
  ),
);
