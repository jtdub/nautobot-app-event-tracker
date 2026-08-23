# The netmiko MCP server, as a container the lab can reach the devices from.
#
# A Dockerfile rather than a `pip install` in the compose command: the install is then a cached
# image layer instead of a network round trip on every `docker compose up`, and the version is
# pinned somewhere a person will look for it.
FROM python:3.12-slim

# Pinned. An MCP server is a thing this deployment allows an agent to call, and "whatever PyPI had
# that morning" is not a description of what you reviewed.
ARG NETMIKO_MCP_VERSION=0.2.0
# `mcp<2` is not fussiness. netmiko-mcp 0.2.0 declares `mcp>=1.27.1` with no upper bound and
# imports `mcp.server.fastmcp`, which the 2.0 SDK removed - so an unpinned install resolves to 2.0
# and the server will not start at all. Upstream's constraint is wrong; this is the pin it needs.
#
# Nothing to do with the `mcp` version Event Tracker itself uses. That is 2.x, in a different
# container, speaking to this one over HTTP - which is rather the point of a protocol.
RUN pip install --no-cache-dir "netmiko-mcp==${NETMIKO_MCP_VERSION}" "mcp>=1.27.1,<2"

# The three files netmiko-mcp reads. Mounted by compose rather than copied, so editing the
# inventory or the command whitelist does not need a rebuild.
ENV NETMIKO_MCP_CONFIG=/config/netmiko-mcp.yml

EXPOSE 8000
CMD ["netmiko-mcp"]
