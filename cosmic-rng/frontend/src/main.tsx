import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter } from "react-router-dom";
import { BASE } from "./lib/base";
import App from "./App";
import "./styles/global.css";

const el = document.getElementById("root");
if (!el) throw new Error("#root missing");

createRoot(el).render(
  <StrictMode>
    <BrowserRouter basename={BASE}>
      <App />
    </BrowserRouter>
  </StrictMode>,
);
