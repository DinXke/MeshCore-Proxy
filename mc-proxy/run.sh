#!/usr/bin/with-contenv bashio
export MCP_NODE_HOST="$(bashio::config 'node_host')"
export MCP_NODE_PORT="$(bashio::config 'node_port')"
export MCP_LISTEN_PORT=5000
export MCP_MAX_CLIENTS="$(bashio::config 'max_clients')"
export MCP_LOG_LEVEL="$(bashio::config 'log_level')"

ALLOWED=""
for ip in $(bashio::config 'allowed_ips'); do
  ALLOWED="${ALLOWED}${ALLOWED:+,}${ip}"
done
export MCP_ALLOWED_IPS="${ALLOWED}"

if bashio::var.is_empty "${MCP_NODE_HOST}"; then
  bashio::log.fatal "Stel 'node_host' in bij de add-on-configuratie (IP van je MeshCore WiFi-node)."
  exit 1
fi

bashio::log.info "MeshCore Proxy: node ${MCP_NODE_HOST}:${MCP_NODE_PORT}, max ${MCP_MAX_CLIENTS} clients"
exec python3 /mc_proxy.py
