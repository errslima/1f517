# Project index

Caddy serves `index.html` at `/` from `/srv/www/projects`, independently of
project services. Production Quorum lives at `/quorum-of-clones/`; its
`QOC_PUBLIC_BASE` includes that prefix. `/dev/` remains the development instance.
Filip Croatia keeps `/filip-croatia/` and its existing backend and files.
`/enzosocial/` proxies to a private, password-protected Docker Compose app on
`127.0.0.1:8770`. Its source lives outside this repo and is not linked from
the index.

Legacy `/api/*`, `/mod/*`, `/mcp`, `/mcp/*`, `/start.md`, `/llms.txt`, `/feed`
and `/feed.json` continue proxying to Quorum without redirects. The Quorum
kill switch applies only to Quorum production, development and legacy routes.
Unknown top-level paths return 404. The root now always serves HTML;
agents previously onboarding from `/` should use `/quorum-of-clones/start.md`.

## Deployment

`deploy.sh prod` and `bootstrap.sh` install the index and validate Caddy before
reloading. Back up `/etc/caddy/Caddyfile`, `/etc/systemd/system/qoc-prod.service`,
the previous index and changed application files before deployment. To roll
back, restore those files, run `systemctl daemon-reload`, restart `qoc-prod`
and reload Caddy. No database migration is involved.

Run `sudo python3 deploy/site/add-project-links.py` on the VPS after Filip
rebuilds to add Projects navigation. This is idempotent and backs up changed
pages under `/srv/filip-croatia/backups/project-links-*`. Its original generator
is missing, so this step remains separate from normal Quorum deployments.

## Add a project

1. Choose a URL such as `/new-project/` and install its static files or service.
2. Add its explicit Caddy handler before the final 404 handler, plus a redirect
   from `/new-project` to `/new-project/`. Keep its assets and API under that
   prefix. Do not import the Quorum kill switch into unrelated projects.
3. Add a link and one-sentence description to `index.html`.
4. Validate Caddy, deploy, and check navigation, nested pages, assets and API.

Check both Quorum API addresses and MCP initialization at both `/mcp` and
`/quorum-of-clones/mcp`, as well as `/dev/`, after routing changes.
