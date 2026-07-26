/**
 * Asset loading: manifest -> download -> verify -> cache.
 *
 * Every failure here is given a stable code so the UI can say something
 * actionable instead of "boot failed". The codes match the ones listed in
 * BROWSER_WASM_IMPLEMENTATION_PLAN.md.
 */

export class LoadError extends Error {
  constructor(code, message, cause) {
    super(message);
    this.name = 'LoadError';
    this.code = code;
    this.cause = cause;
  }
}

export const CACHE_PREFIX = 'ipod-assets';

/** Capability gate. Threads need SharedArrayBuffer, which needs COOP/COEP. */
export function checkCapabilities() {
  const problems = [];
  if (typeof WebAssembly !== 'object') {
    problems.push(new LoadError('BROWSER_UNSUPPORTED', 'WebAssembly is unavailable'));
  }
  if (typeof SharedArrayBuffer !== 'function' || !globalThis.crossOriginIsolated) {
    problems.push(new LoadError(
      'CROSS_ORIGIN_ISOLATION_MISSING',
      'The page is not cross-origin isolated. Serve it with ' +
      'Cross-Origin-Opener-Policy: same-origin and ' +
      'Cross-Origin-Embedder-Policy: require-corp ' +
      '(scripts/wasm/serve.py does this).',
    ));
  }
  return problems;
}

export async function fetchManifest(url) {
  let response;
  try {
    response = await fetch(url, { cache: 'no-cache' });
  } catch (error) {
    throw new LoadError('REMOTE_CORS_BLOCKED', `cannot fetch ${url}`, error);
  }
  if (response.status === 404) {
    throw new LoadError('REMOTE_NOT_FOUND', `no asset manifest at ${url}. ` +
      'Run scripts/wasm/stage-assets.py to create one.');
  }
  if (!response.ok) {
    throw new LoadError('REMOTE_NOT_FOUND', `${url} returned ${response.status}`);
  }
  const manifest = await response.json();
  if (manifest.schemaVersion !== 1) {
    throw new LoadError('ARCHIVE_LAYOUT_INVALID',
      `unsupported manifest schemaVersion ${manifest.schemaVersion}`);
  }
  for (const [name, asset] of Object.entries(manifest.assets ?? {})) {
    if (typeof asset.sha256 !== 'string' || asset.sha256.length !== 64) {
      throw new LoadError('ARCHIVE_LAYOUT_INVALID',
        `asset ${name} has no usable sha256 digest`);
    }
  }
  return manifest;
}

function hex(buffer) {
  return Array.from(new Uint8Array(buffer))
    .map((byte) => byte.toString(16).padStart(2, '0'))
    .join('');
}

/**
 * Download one asset with progress, then verify length and digest on the
 * bytes themselves - never on filenames or HTTP metadata.
 */
export async function fetchAsset(baseUrl, asset, onProgress = () => {}) {
  const url = new URL(asset.url, baseUrl).href;
  let response;
  try {
    response = await fetch(url);
  } catch (error) {
    throw new LoadError('REMOTE_CORS_BLOCKED', `cannot fetch ${url}`, error);
  }
  if (!response.ok) {
    throw new LoadError(
      response.status === 429 ? 'REMOTE_RATE_LIMITED' : 'REMOTE_NOT_FOUND',
      `${url} returned ${response.status}`,
    );
  }

  const total = asset.size ?? Number(response.headers.get('content-length')) ?? 0;
  const chunks = [];
  let received = 0;
  const reader = response.body.getReader();
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    chunks.push(value);
    received += value.byteLength;
    onProgress(received, total);
  }

  const bytes = new Uint8Array(received);
  let offset = 0;
  for (const chunk of chunks) {
    bytes.set(chunk, offset);
    offset += chunk.byteLength;
  }

  if (asset.size !== undefined && bytes.byteLength !== asset.size) {
    throw new LoadError('ASSET_SIZE_MISMATCH',
      `${url}: expected ${asset.size} bytes, got ${bytes.byteLength}`);
  }
  // NOTE: crypto.subtle has no streaming digest, so this hashes the whole
  // buffer at once. Fine for the 300 MB pack on desktop; revisit with a
  // chunked implementation if it hurts on constrained machines.
  const digest = hex(await crypto.subtle.digest('SHA-256', bytes));
  if (digest !== asset.sha256) {
    throw new LoadError('ASSET_HASH_MISMATCH',
      `${url}: sha256 ${digest} does not match the manifest`);
  }
  return bytes;
}

/**
 * Cache Storage keyed by asset-set id and digest: changing any input creates a
 * new namespace rather than reusing a stale entry.
 */
export async function openAssetCache(manifest) {
  return caches.open(`${CACHE_PREFIX}:${manifest.assetSet}`);
}

export async function loadAssetSet(manifestUrl, onEvent = () => {}) {
  onEvent({ state: 'checking-capabilities' });
  const problems = checkCapabilities();
  if (problems.length) throw problems[0];

  onEvent({ state: 'checking-cache' });
  const manifest = await fetchManifest(manifestUrl);
  const cache = await openAssetCache(manifest);

  const files = {};
  for (const [name, asset] of Object.entries(manifest.assets)) {
    const key = `${asset.sha256}/${name}`;
    const cached = await cache.match(key);
    if (cached) {
      onEvent({ state: 'checking-cache', asset: name, cached: true });
      files[name] = new Uint8Array(await cached.arrayBuffer());
      continue;
    }
    onEvent({ state: 'downloading', asset: name, received: 0, total: asset.size });
    const bytes = await fetchAsset(manifestUrl, asset, (received, total) => {
      onEvent({ state: 'downloading', asset: name, received, total });
    });
    onEvent({ state: 'verifying', asset: name });
    // Committed only after the digest matched, so a partial download can
    // never be mistaken for a valid cache entry on the next visit.
    try {
      await cache.put(key, new Response(bytes));
    } catch (error) {
      throw new LoadError('STORAGE_QUOTA_DENIED',
        'the browser refused to cache the firmware (storage quota)', error);
    }
    files[name] = bytes;
  }

  return { manifest, files };
}
