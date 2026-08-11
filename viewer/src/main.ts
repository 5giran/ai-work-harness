import "./styles.css";

import { createViewerApp } from "./app";

const root = document.querySelector<HTMLElement>("#app");
if (!root) throw new Error("Viewer root element is missing");

createViewerApp(root);
