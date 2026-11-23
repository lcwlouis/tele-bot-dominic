export type Theme = "dark" | "light";
const KEY = "dom-theme";

function stored(): Theme | null {
  try {
    const value = localStorage.getItem(KEY);
    return value === "dark" || value === "light" ? value : null;
  } catch {
    return null;
  }
}

function system(): Theme {
  return matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark";
}

/** Dark or light, remembered in this browser; the system preference until the owner picks one. */
class ThemeState {
  value = $state<Theme>(stored() ?? system());

  apply() {
    document.documentElement.dataset.theme = this.value;
  }

  toggle() {
    this.value = this.value === "dark" ? "light" : "dark";
    try {
      localStorage.setItem(KEY, this.value);
    } catch {
      // Private windows may refuse storage; the theme still switches for this visit.
    }
    this.apply();
  }
}

export const theme = new ThemeState();
