import { readFileSync } from 'node:fs'
import { defineConfig } from 'vite'
import react, { reactCompilerPreset } from '@vitejs/plugin-react'
import babel from '@rolldown/plugin-babel'
import path from 'path'

const packageJson = JSON.parse(
  readFileSync(new URL('./package.json', import.meta.url), 'utf-8'),
) as { version?: string }
const buildTime = new Date().toISOString()

// https://vite.dev/config/
export default defineConfig({
  define: {
    __APP_PACKAGE_VERSION__: JSON.stringify(packageJson.version ?? '0.0.0'),
    __APP_BUILD_TIME__: JSON.stringify(buildTime),
  },
  plugins: [
    react(),
    babel({ presets: [reactCompilerPreset()] }),
  ],
  server: {
    host: '0.0.0.0',  // 允许公网访问
    port: 5173,       // 默认端口
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:8000',
        changeOrigin: true,
      },
    },
  },
  build: {
    // 打包输出到项目根目录的 static 文件夹
    outDir: path.resolve(__dirname, '../../static'),
    emptyOutDir: true,
    rolldownOptions: {
      output: {
        codeSplitting: {
          groups: [
            {
              name: 'react-vendor',
              test: /node_modules\/(react|react-dom|scheduler)\//,
            },
            {
              name: 'router-vendor',
              test: /node_modules\/(react-router|react-router-dom)\//,
            },
            {
              name: 'charts-vendor',
              test: /node_modules\/(recharts|d3-|victory-vendor)\//,
            },
            {
              name: 'markdown-vendor',
              test: /node_modules\/(react-markdown|remark-|rehype-|micromark|mdast-util|hast-util|unified|unist-util|vfile)\//,
            },
            {
              name: 'ui-vendor',
              test: /node_modules\/(@remixicon|lucide-react|motion)\//,
            },
          ],
        },
      },
    },
  },
})
