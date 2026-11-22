import { mount } from 'svelte';
import App from './App.svelte';
import './lib/styles/base.css';

mount(App, { target: document.getElementById('app')! });
