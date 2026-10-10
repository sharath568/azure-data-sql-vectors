# SQL Server 2025 + Full-Text Search.
#
# The official mcr.microsoft.com/mssql/server images don't include the
# full-text component; on Linux it is the separate mssql-server-fts package
# from Microsoft's mssql-server-2025 apt repository. This adds it.
#
# Best effort: it assumes the image is Ubuntu-based (the 2025 tags are
# published for ubuntu-22.04 and ubuntu-24.04) and that the repository for
# that Ubuntu version carries mssql-server-fts. If the image's CU is older
# than the repository's newest build, apt may also upgrade mssql-server to
# keep versions matched. CI runs this in a job that is allowed to fail.
FROM mcr.microsoft.com/mssql/server:2025-latest

USER root
RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends curl gnupg ca-certificates; \
    curl -fsSL https://packages.microsoft.com/keys/microsoft.asc \
      | gpg --dearmor -o /usr/share/keyrings/microsoft-prod.gpg; \
    . /etc/os-release; \
    curl -fsSL "https://packages.microsoft.com/config/ubuntu/${VERSION_ID}/mssql-server-2025.list" \
      -o /etc/apt/sources.list.d/mssql-server-2025.list; \
    apt-get update; \
    apt-get install -y --no-install-recommends mssql-server-fts; \
    rm -rf /var/lib/apt/lists/*
USER mssql
