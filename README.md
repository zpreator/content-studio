# content-studio

Docker-compose app serving the content studio UI on host port 3004 (container port 3000).

## Deployment
Deployed like the other apps on the sandbox host: a systemd timer runs
`/usr/local/bin/git-auto-deploy.sh /root/content-studio` every 2 minutes, which fast-forwards
to `origin/main` and runs `docker compose up -d --build`. Merging to `main` deploys.

## Not in git (runtime volumes, see `docker-compose.yml`)
`content/`, `data/`, `secrets/` (access token / password) and `claude-config/` (Claude Code login)
live only on the host and are ignored via `.gitignore`.
