import { defineConfig, loadEnv } from 'vite'
import react from '@vitejs/plugin-react'
import path from 'path'

// https://vitejs.dev/config/
// Function form so the proxy target can come from an env file. `envDir` is
// the repo ROOT, not frontend/, so loadEnv must be pointed there too — a
// `frontend/.env.local` is silently ignored by this project.
export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, path.resolve(__dirname, '..'), '');
  return {
  envDir: '../',
  plugins: [react()],
  resolve: {
    alias: {
      "@": path.resolve(__dirname, "./src"),
    },
    // Force every import of `react` / `react-dom` (including from inside
    // pre-bundled deps like lottie-react) to resolve to the SAME copy.
    // Without this, Vite's optimizer can stash a private React inside
    // lottie-react's pre-bundle, which then has its own dispatcher
    // (null in dev) and throws "Invalid hook call" the first time the
    // component mounts.
    dedupe: ['react', 'react-dom'],
  },
  optimizeDeps: {
    // Be explicit about React being part of the pre-bundle alongside
    // lottie-react so the optimizer's chunk graph doesn't duplicate it.
    include: ['react', 'react-dom', 'lottie-react'],
  },
  build: {
    rollupOptions: {
      // Two pages. `meet-addon.html` is the Cubie side panel inside Google
      // Meet (src/meetAddon/) — its own small bundle, so the panel never boots
      // the app shell. backend/main.py serves it at /meet-addon/side-panel.
      input: {
        main: path.resolve(__dirname, 'index.html'),
        meetAddon: path.resolve(__dirname, 'meet-addon.html'),
      },
    },
  },
  server: {
    proxy: {
      '/api': {
        // Overridable so a frontend-only session can point at deployed dev and
        // sign in without standing up a backend. Default is unchanged, so
        // `npm run dev` against a local API behaves exactly as before.
        // Set VITE_API_TARGET in the REPO ROOT .env.local (gitignored) —
        // not frontend/.env.local, which envDir means nothing reads.
        target: env.VITE_API_TARGET || 'http://127.0.0.1:8001',
        changeOrigin: true,
        // rewrite: (path) => path.replace(/^\/api/, ''), // FastAPI uses /api prefix correctly
      }
    }
  }
  };
})
