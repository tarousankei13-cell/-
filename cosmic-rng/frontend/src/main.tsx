import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter } from "react-router-dom";
import { BASE, withBase } from "./lib/base";
import App from "./App";
import "./styles/global.css";

const el = document.getElementById("root");
if (!el) throw new Error("#root missing");

// Installability and a warm start for the static bundle. Registration is
// best-effort: a browser without service workers, or a page served over plain
// http, simply plays the game without one.
if ("serviceWorker" in navigator && location.protocol === "https:") {
  window.addEventListener("load", () => {
    navigator.serviceWorker.register(withBase("/sw.js"), { scope: withBase("/") }).catch(() => undefined);
  });
}

createRoot(el).render(
  <StrictMode>
    <BrowserRouter basename={BASE}>
      <App />
    </BrowserRouter>
  </StrictMode>,
);
