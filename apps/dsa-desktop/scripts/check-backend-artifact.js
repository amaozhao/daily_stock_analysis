const fs = require('node:fs');
const path = require('node:path');

function resolveBackendArtifactPath(projectRoot = path.resolve(__dirname, '..', '..', '..')) {
  return path.join(projectRoot, 'dist', 'backend', 'stock_analysis');
}

function checkBackendArtifact(options = {}) {
  const projectRoot = options.projectRoot || path.resolve(__dirname, '..', '..', '..');
  const artifactPath = resolveBackendArtifactPath(projectRoot);
  const stderr = options.stderr || console.error;
  const stdout = options.stdout || console.log;

  if (!fs.existsSync(artifactPath) || !fs.statSync(artifactPath).isDirectory()) {
    stderr(`Backend artifact not found: ${artifactPath}`);
    stderr('Run the backend packaging step first: scripts/build-backend-macos.sh or scripts\\build-backend.ps1.');
    return 1;
  }

  if (fs.readdirSync(artifactPath).length === 0) {
    stderr(`Backend artifact is empty: ${artifactPath}`);
    stderr('Run the backend packaging step again before building the desktop app.');
    return 1;
  }

  stdout(`Backend artifact found: ${artifactPath}`);
  return 0;
}

if (require.main === module) {
  process.exitCode = checkBackendArtifact();
}

module.exports = {
  checkBackendArtifact,
  resolveBackendArtifactPath,
};
