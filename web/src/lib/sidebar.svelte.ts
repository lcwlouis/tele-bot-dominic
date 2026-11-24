const KEY = "dom-sidebar";

function stored(): boolean {
  try {
    return localStorage.getItem(KEY) === "collapsed";
  } catch {
    return false;
  }
}

/** The desktop sidebar: full, or a slim rail of icons. Remembered in this browser. */
class SidebarState {
  collapsed = $state(stored());

  toggle() {
    this.collapsed = !this.collapsed;
    try {
      localStorage.setItem(KEY, this.collapsed ? "collapsed" : "expanded");
    } catch {
      // Private windows may refuse storage; the sidebar still folds for this visit.
    }
  }
}

export const sidebar = new SidebarState();

/** True when a key press belongs to whatever has focus: a text field, an editable area or a select. */
export function typing(target: EventTarget | null): boolean {
  if (!(target instanceof HTMLElement)) return false;
  return target.isContentEditable || target.closest("input, textarea, select, [contenteditable], [role='textbox'], [role='combobox']") !== null;
}
