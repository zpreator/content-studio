FROM python:3.12-slim

ENV TZ=America/Denver PYTHONUNBUFFERED=1
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates tzdata \
    && rm -rf /var/lib/apt/lists/* \
    && useradd -m -u 1000 studio

USER studio
ENV PATH="/home/studio/.local/bin:${PATH}"
# The studio's AI steps shell out to the Claude Code CLI (`claude -p`).
RUN curl -fsSL https://claude.ai/install.sh | bash && claude --version

WORKDIR /app
COPY --chown=studio:studio app/ /app/

# Everything that changes lives in volumes (see docker-compose.yml): content, exported game data, the access token, Claude's login.
ENV CLAUDE_CONFIG_DIR=/config \
    STUDIO_CONTENT_DIR=/app/content \
    STUDIO_DATA_DIR=/app/data \
    STUDIO_TOKEN_FILE=/secrets/access_token STUDIO_PASSWORD_FILE=/secrets/password

EXPOSE 3000
# Loopback requests need no token, so the health check can ask the API directly.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
    CMD python3 -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:3000/api/state', timeout=4)"

CMD ["python3", "tools/content_studio.py", "--port", "3000", "--lan", "--no-open", "--allow-host", "content.puzzleshome.com"]
