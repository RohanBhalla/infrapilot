import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import App from "./App";
import { configureAuth } from "./auth";
import { loadConfig } from "./config";
import "./styles.css";

void loadConfig().then(() => {
  configureAuth();
  createRoot(document.getElementById("root")!).render(
    <StrictMode>
      <App />
    </StrictMode>,
  );
});
