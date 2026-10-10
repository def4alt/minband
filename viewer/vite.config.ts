import { fileURLToPath } from 'node:url';
import { defineConfig } from 'vite';
const here = (p: string) => fileURLToPath(new URL(p, import.meta.url));
export default defineConfig({
  server: { port: 5173, fs: { allow: ['..'] } },
  build: { rollupOptions: { input: { main: here('index.html'), recon: here('recon.html') } } },
});
