// The search mode the owner last picked (D19): "words" (keywords, the default) or "meaning" (the embedding index).
// Remembered in this browser, so the top box and the Search page start where the owner left them.
const KEY = "dom-search-mode";

function stored(): boolean {
  try {
    return localStorage.getItem(KEY) === "meaning";
  } catch {
    return false;
  }
}

class SearchMode {
  meaning = $state(stored());

  set(meaning: boolean) {
    this.meaning = meaning;
    try {
      localStorage.setItem(KEY, meaning ? "meaning" : "words");
    } catch {
      // Private windows may refuse storage; the mode still holds for this visit.
    }
  }
}

export const searchMode = new SearchMode();
