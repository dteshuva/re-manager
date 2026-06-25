import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// API base is read from VITE_API_URL (see .env.example); defaults to local backend.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
  },
});
