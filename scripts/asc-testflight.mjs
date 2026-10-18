import { readFileSync } from 'node:fs';
import { createSign } from 'node:crypto';
const cfg = JSON.parse(readFileSync('/home/pavel/DEV/unity-builder-dash/config.json', 'utf8')).ios_remote;
const KEY = readFileSync(cfg.asc_key_p8_path, 'utf8');
const b64 = (o) => Buffer.from(JSON.stringify(o)).toString('base64url');
function token() {
  const now = Math.floor(Date.now() / 1000);
  const h = b64({ alg: 'ES256', kid: cfg.asc_key_id, typ: 'JWT' });
  const b = b64({ iss: cfg.asc_issuer_id, iat: now, exp: now + 600, aud: 'appstoreconnect-v1' });
  const s = createSign('SHA256').update(`${h}.${b}`).sign({ key: KEY, dsaEncoding: 'ieee-p1363' });
  return `${h}.${b}.${s.toString('base64url')}`;
}
async function api(path, init = {}) {
  const r = await fetch(`https://api.appstoreconnect.apple.com/v1${path}`, { ...init, headers: { Authorization: `Bearer ${token()}`, 'Content-Type': 'application/json' } });
  const t = await r.text();
  if (!r.ok) throw new Error(`${r.status} ${t}`);
  return t ? JSON.parse(t) : {};
}
const [cmd, ver] = process.argv.slice(2);
const apps = await api('/apps?limit=50');
const app = apps.data.find((a) => /kartoteka/i.test(a.attributes.name) && /AR|kartoteka/i.test(a.attributes.bundleId));
if (cmd === 'apps') { for (const a of apps.data) console.log(a.id, a.attributes.name, a.attributes.bundleId); process.exit(); }
const builds = await api(`/builds?filter[app]=${app.id}&filter[version]=${ver}&include=betaGroups`);
const b = builds.data[0];
if (!b) { console.log('build not found yet'); process.exit(); }
console.log('build', b.attributes.version, b.attributes.processingState);
const groups = await api(`/apps/${app.id}/betaGroups?limit=50`);
for (const g of groups.data) console.log('group', g.attributes.name, g.attributes.isInternalGroup ? 'internal' : 'external');
const inGroups = (builds.included || []).map((g) => g.attributes.name);
console.log('build in groups:', inGroups);
if (cmd === 'add' && b.attributes.processingState === 'VALID') {
  const g = groups.data.find((x) => x.attributes.name === 'Внутреннее тестирование' && x.attributes.isInternalGroup);
  await api(`/betaGroups/${g.id}/relationships/builds`, { method: 'POST', body: JSON.stringify({ data: [{ type: 'builds', id: b.id }] }) });
  console.log('added to', g.attributes.name);
}
