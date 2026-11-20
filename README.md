# content-studio

Docker-compose app serving the content studio UI on host port 3004 (container port 3000).

## Deployment
Deployed like the other apps on the sandbox host: a systemd timer runs
`/usr/local/bin/git-auto-deploy.sh /root/content-studio` every 2 minutes, which fast-forwards
to `origin/main` and runs `docker compose up -d --build`. Merging to `main` deploys.

## Access control
The app has no login of its own. Access is controlled upstream (Cloudflare Access on the public hostname).
Port 3004 must not be exposed to untrusted networks: anything that can reach it can run the studio's
AI steps with the Claude Code login stored in `claude-config/`.

## Not in git (runtime volumes, see `docker-compose.yml`)
`content/`, `data/`, `claude-config/` (Claude Code login)
live only on the host and are ignored via `.gitignore`.
