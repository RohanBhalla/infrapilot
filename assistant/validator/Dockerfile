# The validator Lambda (plan C11): the linters for generated files, which are too big for the
# chat Lambda's zip. Build context: assistant/. infra/ builds and deploys it (ValidatorFunction).
# Pinned versions were released at least two weeks before they were added; checksums are from
# each release's checksum file.
FROM public.ecr.aws/lambda/python:3.12-arm64

ARG ACTIONLINT=1.7.12
ARG ACTIONLINT_SHA256=325e971b6ba9bfa504672e29be93c24981eeb1c07576d730e9f7c8805afff0c6
ARG HADOLINT=2.15.1
ARG HADOLINT_SHA256=f6198ef8090f404dbb771abfee086eb8c48ac177f30da7fd3510aca35b344b5d

RUN dnf install -y tar gzip && dnf clean all \
 && curl -fsSL -o /tmp/al.tgz "https://github.com/rhysd/actionlint/releases/download/v${ACTIONLINT}/actionlint_${ACTIONLINT}_linux_arm64.tar.gz" \
 && echo "${ACTIONLINT_SHA256}  /tmp/al.tgz" | sha256sum -c - \
 && tar -xzf /tmp/al.tgz -C /usr/local/bin actionlint && rm /tmp/al.tgz \
 && curl -fsSL -o /usr/local/bin/hadolint "https://github.com/hadolint/hadolint/releases/download/v${HADOLINT}/hadolint-linux-arm64" \
 && echo "${HADOLINT_SHA256}  /usr/local/bin/hadolint" | sha256sum -c - \
 && chmod +x /usr/local/bin/hadolint

COPY validator/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

# Only the assistant package (its rag dependency isn't needed to run the checks).
COPY pyproject.toml README.md /tmp/assistant/
COPY infrapilot_assistant /tmp/assistant/infrapilot_assistant
RUN pip install --no-cache-dir --no-deps --target "${LAMBDA_TASK_ROOT}" /tmp/assistant && rm -rf /tmp/assistant

# Lambda can only write to /tmp; checkov and cfn-lint keep caches under $HOME.
ENV HOME=/tmp
CMD ["infrapilot_assistant.generators.validate.lambda_handler"]
