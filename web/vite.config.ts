import { defineConfig, type Plugin } from 'vite'
import react from '@vitejs/plugin-react'

/**
 * index.html's CSP allows pictures, video, frames and fetches from 'self' only,
 * which is Jarvis when it serves the built page. The dev server is another
 * origin, so `npm run dev` (and only that) adds Jarvis's address back.
 */
function devJarvisCsp(): Plugin {
  const port = Number(process.env.JARVIS_WEB_PORT) || 8790
  const jarvis = `http://localhost:${port} http://127.0.0.1:${port}`
  return {
    name: 'jarvis-dev-csp',
    apply: 'serve',
    transformIndexHtml(html) {
      return html.replace(/\b(frame-src|img-src|media-src|connect-src) 'self'/g, `$1 'self' ${jarvis}`)
    },
  }
}

// https://vite.dev/config/
export default defineConfig({
  plugins: [react(), devJarvisCsp()],
  server: {
    // Honour PORT so a second instance can run alongside the first. The bridge
    // only accepts sockets from localhost:5173-5199, so stay inside that range
    // or set JARVIS_ALLOWED_ORIGINS to match.
    port: Number(process.env.PORT) || 5173,
  },
  optimizeDeps: {
    // kokoro-js pulls in `phonemizer`, which carries espeak-ng as inline WASM.
    // Vite's dependency pre-bundler rewrites that initialisation and the
    // language table ends up empty — the symptom is
    // `Invalid language identifier: "en". Should be one of: .` at generate()
    // time, long after the model has loaded successfully. Serving these
    // untouched fixes it.
    exclude: ['kokoro-js', 'phonemizer', '@huggingface/transformers'],
  },
})
