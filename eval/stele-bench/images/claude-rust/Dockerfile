FROM rust@sha256:38bc5a86d998772d4aec2348656ed21438d20fcdce2795b56ca434cf21430d89

ARG CLAUDE_CODE_VERSION=2.1.220

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        cmake \
        make \
        nodejs \
        npm \
        perl \
        pkg-config \
        procps \
    && npm install -g "@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}" \
        --no-audit \
        --no-fund \
    && test "$(claude --version)" = "${CLAUDE_CODE_VERSION} (Claude Code)" \
    && rm -rf /var/lib/apt/lists/* /root/.npm

WORKDIR /workspace
