const assert = require('node:assert/strict');
const test = require('node:test');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

const {
  checkBackendArtifact,
  resolveBackendArtifactPath,
} = require('../scripts/check-backend-artifact');

function withTempRoot(t) {
  const tempRoot = fs.mkdtempSync(path.join(os.tmpdir(), 'dsa-backend-artifact-'));
  t.after(() => fs.rmSync(tempRoot, { recursive: true, force: true }));
  return tempRoot;
}

test('backend artifact check fails when packaged backend is missing', (t) => {
  const projectRoot = withTempRoot(t);
  const errors = [];

  const rc = checkBackendArtifact({
    projectRoot,
    stderr: (line) => errors.push(line),
    stdout: () => undefined,
  });

  assert.equal(rc, 1);
  assert.match(errors.join('\n'), /Backend artifact not found/);
  assert.match(errors.join('\n'), /build-backend/);
});

test('backend artifact check fails when packaged backend is empty', (t) => {
  const projectRoot = withTempRoot(t);
  fs.mkdirSync(resolveBackendArtifactPath(projectRoot), { recursive: true });
  const errors = [];

  const rc = checkBackendArtifact({
    projectRoot,
    stderr: (line) => errors.push(line),
    stdout: () => undefined,
  });

  assert.equal(rc, 1);
  assert.match(errors.join('\n'), /Backend artifact is empty/);
});

test('backend artifact check passes when packaged backend has files', (t) => {
  const projectRoot = withTempRoot(t);
  const artifactPath = resolveBackendArtifactPath(projectRoot);
  fs.mkdirSync(artifactPath, { recursive: true });
  fs.writeFileSync(path.join(artifactPath, 'stock_analysis'), '');
  const logs = [];

  const rc = checkBackendArtifact({
    projectRoot,
    stderr: () => undefined,
    stdout: (line) => logs.push(line),
  });

  assert.equal(rc, 0);
  assert.match(logs.join('\n'), /Backend artifact found/);
});
