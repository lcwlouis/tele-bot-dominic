import { mount } from "svelte";
import "./app.css";
import App from "./App.svelte";
import { theme } from "$lib/theme.svelte";

theme.apply();
mount(App, { target: document.getElementById("app")! });
