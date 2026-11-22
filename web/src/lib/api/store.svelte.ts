/*
  The app's data and every action the owner can take, in one place.

  Actions are optimistic: the screen changes at once, and goes back if Dom refuses. For the baseline
  the data starts from the fixtures and `send` only waits, the way a request would. When Dom serves the
  JSON API (see types.ts), load with fetch and make `send` the real request; the pages don't change.
*/
import * as fixtures from './fixtures';
import type {
  ChatPatch,
  ChatsResponse,
  HomeResponse,
  ModelRole,
  ModelTest,
  RoleSettings,
  SettingsResponse,
  ToggleKey,
  TraitName,
  VibeChoice,
} from './types';

export const data = $state<{ home: HomeResponse; chats: ChatsResponse; settings: SettingsResponse }>(
  structuredClone({ home: fixtures.home, chats: fixtures.chats, settings: fixtures.settings }),
);

const send = (ms = 350) => new Promise((resolve) => setTimeout(resolve, ms));

/** Apply a change now; undo it if the request fails. */
async function optimistic(apply: () => () => void, request: Promise<unknown>): Promise<void> {
  const undo = apply();
  try {
    await request;
  } catch (error) {
    undo();
    throw error;
  }
}

export interface Actions {
  patchChat(id: number, patch: ChatPatch): Promise<void>;
  leaveChat(id: number): Promise<void>;
  setVibe(choice: VibeChoice): Promise<void>;
  saveRole(settings: RoleSettings): Promise<void>;
  testRole(role: ModelRole): Promise<ModelTest>;
  setToggle(key: ToggleKey, value: boolean): Promise<void>;
}

export const actions: Actions = {
  patchChat(id, patch) {
    const chat = data.chats.chats.find((item) => item.id === id);
    if (!chat) return Promise.reject(new Error('That chat is gone. Reload to see the current list.'));
    return optimistic(() => {
      const before = $state.snapshot(chat);
      if (patch.preset !== undefined) chat.preset = patch.preset;
      for (const [name, value] of Object.entries(patch.sliders ?? {}) as [TraitName, number | null][]) {
        // Dom answers with the inherited value; until then the soul's default stands in.
        chat.sliders[name] = value === null ? { value: 0.5, custom: false } : { value, custom: true };
      }
      return () => Object.assign(chat, before);
    }, send());
  },

  leaveChat(id) {
    return optimistic(() => {
      const before = data.chats.chats;
      data.chats.chats = before.filter((item) => item.id !== id);
      return () => (data.chats.chats = before);
    }, send());
  },

  setVibe(choice) {
    return optimistic(() => {
      const before = $state.snapshot(data.chats.vibe);
      const vibe =
        choice.mode === 'auto'
          ? structuredClone(fixtures.chats.vibe)
          : {
              mode: choice.mode,
              preset:
                choice.mode === 'locked'
                  ? (data.chats.presets.find((preset) => preset.name === choice.preset) ?? null)
                  : null,
              reason: '',
              source: '' as const,
              since: fixtures.NOW,
            };
      data.chats.vibe = vibe;
      data.home.vibe = structuredClone(vibe);
      return () => {
        data.chats.vibe = before;
        data.home.vibe = structuredClone(before);
      };
    }, send());
  },

  saveRole(settings) {
    return optimistic(() => {
      const index = data.settings.roles.findIndex((item) => item.role === settings.role);
      const before = data.settings.roles[index]!;
      data.settings.roles[index] = settings;
      return () => (data.settings.roles[index] = before);
    }, send());
  },

  async testRole(role) {
    await send(900);
    return fixtures.tests[role];
  },

  setToggle(key, value) {
    return optimistic(() => {
      const before = data.settings.toggles[key];
      data.settings.toggles[key] = value;
      return () => (data.settings.toggles[key] = before);
    }, send());
  },
};

/** Actions that answer at once and change nothing, for Storybook. */
export const noopActions: Actions = {
  patchChat: async () => {},
  leaveChat: async () => {},
  setVibe: async () => {},
  saveRole: async () => {},
  testRole: async (role) => fixtures.tests[role],
  setToggle: async () => {},
};
