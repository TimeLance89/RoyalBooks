"""Read-only Docker audit. Never prints container environments or secrets."""
import argparse
import json
import posixpath
import subprocess


def mapped(container, destination):
    mounts = [m for m in container.get('Mounts', [])
              if destination == m['Destination'] or destination.startswith(m['Destination'].rstrip('/') + '/')]
    if not mounts:
        return None
    mount = max(mounts, key=lambda m: len(m['Destination']))
    suffix = destination[len(mount['Destination']):].lstrip('/')
    # Docker inspect reports the volume root; Subpath is stored separately in
    # HostConfig.Mounts, even though the container sees only that subdirectory.
    options = next((m for m in container.get('HostConfig', {}).get('Mounts', [])
                    if m.get('Target') == mount['Destination']), {})
    subpath = options.get('VolumeOptions', {}).get('Subpath', '')
    return {'host': posixpath.normpath(posixpath.join(mount['Source'], subpath, suffix)),
            'writable': mount.get('RW', False), 'type': mount['Type']}


def audit(orbit, royal):
    env = dict(e.split('=', 1) for e in orbit.get('Config', {}).get('Env', []) if '=' in e)
    dock = env.get('BOOK_DOCK_PATH', '').strip() or '/data/book-dock'
    incoming = mapped(orbit, dock)
    target = mapped(royal, '/bookorbit-ingest')
    checks = []
    def check(name, ok, detail):
        checks.append({'name':name,'status':'ok' if ok else 'error','detail':detail})
    check('bookorbit_running', orbit.get('State', {}).get('Running'), 'BookOrbit-Container läuft')
    check('royalbooks_running', royal.get('State', {}).get('Running'), 'RoyalBooks-Container läuft')
    check('shared_dock', bool(incoming and target and incoming['host'] == target['host']), 'Beide Container müssen denselben Book-Dock-Unterordner verwenden')
    check('dock_writable', bool(incoming and target and incoming['writable'] and target['writable']), 'Beide Mounts müssen beschreibbar sein; UID-Schreibprobe zusätzlich erforderlich')
    library = mapped(orbit, '/books')
    if incoming and library:
        a,b=incoming['host'].rstrip('/'),library['host'].rstrip('/')
        check('dock_not_library', not (a==b or a.startswith(b+'/') or b.startswith(a+'/')), 'Book Dock liegt außerhalb der Bibliothek')
    networks = set(orbit.get('NetworkSettings', {}).get('Networks', {})) & set(royal.get('NetworkSettings', {}).get('Networks', {}))
    return {'status':'error' if any(c['status']=='error' for c in checks) else 'review_required',
            'checks':checks,'bookorbit_dock':dock,'shared_networks':sorted(networks),
            'mounts':{'bookorbit':incoming,'royalbooks':target},'changes_made':False,
            'notice':'Mountvergleich bestätigt keine API-Anmeldung, registrierten Bibliotheksordner oder erfolgreiche Aufnahme. Kein PostgreSQL-Zugriff.'}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bookorbit-container',default='bookorbit-app')
    parser.add_argument('--royalbooks-container',default='royalbooks-royalbooks-1')
    args=parser.parse_args()
    snapshots=json.loads(subprocess.check_output(['docker','inspect',args.bookorbit_container,args.royalbooks_container],text=True))
    print(json.dumps(audit(*snapshots),ensure_ascii=False,indent=2))


if __name__=='__main__':main()
