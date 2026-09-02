# Image holding sqlfluff and the validator.
#
# Everything the validator needs is a python dependency, so the image is little more than a
# python base with the package installed: sqlfluff is pinned in pyproject.toml, and both
# extractors are part of the package.
FROM python:3.13-slim-bookworm AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Built from a scratch directory that is thrown away afterwards, so the image does not carry
# a pyproject.toml around at /. That matters: the validator treats the working directory's
# pyproject.toml as sqlfluff configuration when it has a [tool.sqlfluff] section, and its own
# must never be mistaken for the checked repository's.
COPY pyproject.toml README.md LICENSE /build/
COPY src /build/src
RUN pip install --no-cache-dir /build && rm -rf /build

# Run as a normal user. pre-commit overrides this with the host uid so the scratch files it
# writes are not owned by root.
#
# pip puts the console scripts on the PATH at /usr/local/bin. The published dashboard hooks
# name /bin/validate-dashboard-sql as their entry point, so that path is kept working.
RUN useradd --create-home --uid 1000 checker && \
    ln -s /usr/local/bin/validate-dashboard-sql /usr/bin/validate-dashboard-sql && \
    ln -s /usr/local/bin/databricks-validator /usr/bin/databricks-validator

# The test image: the same install, plus pytest and the tests themselves. The build pipeline
# runs `docker build --target test` so the unit tests exercise the very image that gets
# published.
FROM base AS test

RUN pip install --no-cache-dir pytest==9.1.1

WORKDIR /work
COPY tests /work/tests
COPY examples /work/examples
USER 1000
ENTRYPOINT []
# No cache provider: /work belongs to root and the tests run as uid 1000.
CMD ["pytest", "-q", "-p", "no:cacheprovider"]

# The published image. Last stage, so a plain `docker build` produces it.
FROM base AS runtime

USER 1000
ENTRYPOINT ["/bin/databricks-validator"]
