"""Add index navigation to deployed Filip pages; preserve a backup of each change."""
from datetime import datetime, timezone
from pathlib import Path
import shutil

root = Path('/srv/www/enzoserver/filip-croatia')
backup = Path('/srv/filip-croatia/backups') / (
    'project-links-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S'))
marker = b'<nav class="tabs" aria-label="Destinations">'
link = b'<a href="/" data-project-index>Projects</a>'
updates = {}
for page in root.rglob('*.html'):
    data = page.read_bytes()
    if b'data-project-index' in data:
        continue
    if marker not in data:
        raise SystemExit(f'Missing navigation marker: {page}')
    updates[page] = data.replace(marker, marker + b'\n      ' + link, 1)
for page, data in updates.items():
    saved = backup / page.relative_to(root)
    saved.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(page, saved)
    page.write_bytes(data)
print(f'Added Projects links to {len(updates)} pages. Backup: {backup}')
