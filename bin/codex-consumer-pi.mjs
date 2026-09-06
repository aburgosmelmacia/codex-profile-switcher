#!/usr/bin/env node
// Stage the complete batch under Pi's auth lock; roll back failed replacements.
import { createRequire } from 'node:module';
import { dirname, join, isAbsolute } from 'node:path';
import { promises as fs } from 'node:fs';
import { randomUUID } from 'node:crypto';

const statOrMissing = path => fs.lstat(path).catch(e => { if (e.code !== 'ENOENT') throw e; });
const remove = path => fs.unlink(path).catch(e => { if (e.code !== 'ENOENT') throw e; });

try {
  const [piPackage, authPath] = process.argv.slice(2);
  const require = createRequire(join(piPackage, 'package.json'));
  const lockfile = require('proper-lockfile');
  let input = '';
  for await (const chunk of process.stdin) input += chunk;
  const { credential: block, files, consumerMarker } = JSON.parse(input);
  if (block.type !== 'oauth' || block.refresh !== '' || !block.access || !block.accountId || !Number.isFinite(block.expires)) {
    throw new Error('Invalid access-only credential');
  }
  if (!Array.isArray(files) || files.some(f => !isAbsolute(f.path) || typeof f.text !== 'string') ||
      new Set([...files.map(f => f.path), authPath]).size !== files.length + 1) {
    throw new Error('Invalid transaction targets');
  }
  await fs.mkdir(dirname(authPath), { recursive: true, mode: 0o700 });
  const authStat = await statOrMissing(authPath);
  if (authStat && !authStat.isFile()) throw new Error('Invalid auth target');
  const release = await lockfile.lock(authPath, {
    realpath: false, stale: 30000,
    retries: { retries: 20, minTimeout: 50, maxTimeout: 100 },
  });
  const staged = [];
  let rollbackFailed = false;
  try {
    let live;
    try { live = JSON.parse(await fs.readFile(authPath, 'utf8')); }
    catch (e) { if (e.code !== 'ENOENT') throw e; live = {}; }
    if (!live || typeof live !== 'object' || Array.isArray(live)) throw new Error('Invalid Pi auth document');
    live['openai-codex'] = block;
    files.push({ path: authPath, text: JSON.stringify(live, null, 2) + '\n' });
    // Every file is fully written and flushed before replacing any live target.
    for (const file of files) {
      await fs.mkdir(dirname(file.path), { recursive: true, mode: 0o700 });
      const existing = await statOrMissing(file.path);
      if (existing && !existing.isFile()) throw new Error('Invalid transaction target');
      if (existing && await fs.readFile(file.path, 'utf8') === file.text) {
        await fs.chmod(file.path, 0o600);
        continue;
      }
      const prefix = join(dirname(file.path), `.consumer-${randomUUID()}`);
      const entry = { ...file, temporary: prefix + '.new', backup: prefix + '.old',
        existed: !!existing, backedUp: false, committed: false };
      staged.push(entry);
      const handle = await fs.open(entry.temporary, 'wx', 0o600);
      try { await handle.writeFile(file.text); await handle.sync(); }
      finally { await handle.close(); }
    }
    for (const entry of staged) {
      if (entry.existed) {
        // Retain the old inode without ever removing the live filename.
        await fs.link(entry.path, entry.backup);
        entry.backedUp = true;
      }
      await fs.rename(entry.temporary, entry.path);
      entry.committed = true;
    }
  } catch (error) {
    for (const entry of [...staged].reverse()) {
      // If storage itself prevents rollback, retain consumer mode and backups.
      if (rollbackFailed && entry.path === consumerMarker) continue;
      try {
        if (entry.backedUp) await fs.rename(entry.backup, entry.path);
        else if (entry.committed) await remove(entry.path);
      } catch { rollbackFailed = true; }
    }
    throw error;
  } finally {
    for (const entry of staged) {
      await remove(entry.temporary).catch(() => {});
      if (!rollbackFailed) await remove(entry.backup).catch(() => {});
    }
    await release();
  }
} catch {
  console.error('Could not safely commit consumer credentials; check storage and auth locks.');
  process.exitCode = 1;
}
