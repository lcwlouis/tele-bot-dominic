import type { StudioChain, StudioProvider } from "$lib/api";

/** The studio's cloud backup chains (dom/config.py CHAIN_PROVIDERS) and the providers each one takes. */
export const CHAIN_INFO: Record<StudioChain, { label: string; noun: string; summary: string; providers: StudioProvider[] }> = {
  voice: {
    label: "Voice notes", noun: "speech", summary: "Tried in order when the local studio can't voice a note.",
    providers: ["openrouter_tts", "gemini_tts"],
  },
  image: {
    label: "Photos", noun: "image", summary: "Tried in order when the local studio can't make a photo.",
    providers: ["openrouter_image"],
  },
  video: {
    label: "Video notes", noun: "video",
    summary: "Tried in order when the local studio can't make a round video note, everyday or special.",
    providers: ["openrouter_video"],
  },
};
export const CHAINS: StudioChain[] = ["voice", "image", "video"];

/** Each backup provider's name, suggested key variable and an example model. */
export const STUDIO_PROVIDERS: Record<StudioProvider, { label: string; key: string; example: string }> = {
  openrouter_tts: { label: "OpenRouter speech", key: "OPENROUTER_MEDIA_API_KEY", example: "google/gemini-3.8-flash-lite-tts" },
  gemini_tts: { label: "Gemini speech (direct)", key: "GEMINI_API_KEY", example: "gemini-2.5-flash-preview-tts" },
  openrouter_image: { label: "OpenRouter image", key: "OPENROUTER_MEDIA_API_KEY", example: "google/gemini-2.5-flash-image" },
  openrouter_video: { label: "OpenRouter video", key: "OPENROUTER_MEDIA_API_KEY", example: "heygen/avatar-iv" },
};
