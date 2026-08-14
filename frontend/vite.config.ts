import { defineConfig, loadEnv } from "vite";

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), "RK_");
  const apiOrigin = env.RK_API_ORIGIN;

  return {
    server: {
      host: env.RK_FRONTEND_HOST || "127.0.0.1",
      port: env.RK_FRONTEND_PORT ? Number(env.RK_FRONTEND_PORT) : 5173,
      proxy: apiOrigin
        ? {
            "/v1": {
              target: apiOrigin,
              changeOrigin: false,
            },
          }
        : undefined,
    },
    preview: {
      host: env.RK_FRONTEND_HOST || "127.0.0.1",
      port: env.RK_FRONTEND_PORT ? Number(env.RK_FRONTEND_PORT) : 4173,
    },
  };
});
