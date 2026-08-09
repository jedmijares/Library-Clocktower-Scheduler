FROM node:22

ARG CLAUDE_CODE_VERSION=latest

RUN apt-get update && apt-get install -y --no-install-recommends \
        wget \
        unzip \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

# Add project-specific toolchain steps here as the project grows (e.g. a
# language runtime, CLI, or build tool) - see Factory-Game's Dockerfile for
# an example of layering a project-specific binary (Godot) on this same base.

# GitHub CLI. `gh auth login --web` uses a device flow, so pushing from inside the
# container never requires a token to be pasted anywhere. Installed from the release
# tarball rather than apt, which would mean trusting a third-party repo and key for a
# single static binary. Pinned rather than "latest" so rebuilds are reproducible.
ARG GH_VERSION=2.97.0
RUN wget -q "https://github.com/cli/cli/releases/download/v${GH_VERSION}/gh_${GH_VERSION}_linux_amd64.tar.gz" -O /tmp/gh.tgz \
    && tar -xzf /tmp/gh.tgz -C /tmp \
    && install -m755 "/tmp/gh_${GH_VERSION}_linux_amd64/bin/gh" /usr/local/bin/gh \
    && rm -rf /tmp/gh.tgz "/tmp/gh_${GH_VERSION}_linux_amd64" \
    && gh --version

# Ensure default node user has access to /usr/local/share for global npm installs
RUN mkdir -p /usr/local/share/npm-global && chown -R node:node /usr/local/share

# Pre-create and own the .claude config dir so the node user can write into it
# once devcontainer.json's volume mount lands on top of this path.
RUN mkdir -p /home/node/.claude && chown -R node:node /home/node/.claude

# Same treatment for gh's config dir, which devcontainer.json mounts a volume onto so
# the GitHub login survives rebuilds. Without the mount the token lives in the container
# layer and is lost every time the image is rebuilt.
RUN mkdir -p /home/node/.config/gh && chown -R node:node /home/node/.config

# Windows Terminal sends Ctrl+Backspace as BS (\C-h), distinct from plain
# Backspace's DEL - bash's default readline map doesn't bind BS to word
# deletion, so without this it just deletes one character like plain
# Backspace. Bind it to backward-kill-word to match Windows' native
# PSReadLine behavior (Ctrl+Backspace already works there natively, outside
# any terminal byte stream, which is why this only needs fixing here).
RUN echo '"\C-h": backward-kill-word' > /home/node/.inputrc && chown node:node /home/node/.inputrc

USER node

# The workspace is bind-mounted from the Windows host under a different uid, so every git
# command otherwise fails with "dubious ownership". Baked into the image because
# /home/node/.gitconfig lives in the container layer, not the .claude volume — setting it
# at runtime is lost on the next rebuild. Update the path if the folder is renamed.
RUN git config --global --add safe.directory /workspaces/Library-Clocktower-Scheduler

ENV NPM_CONFIG_PREFIX=/usr/local/share/npm-global
ENV PATH=$PATH:/usr/local/share/npm-global/bin

RUN npm install -g @anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}
