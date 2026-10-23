# Isolate the entire reviewer, including Gateway-side code

Run the pinned OpenClaw reviewer with no network interface, no source-account
credentials, no host source mounts, and no container-runtime socket. Tool
permissions alone cannot constrain plugins or MCP code executing inside the
Gateway process.

Provide only narrow local sockets: an application broker that accepts reviewer
input/proposal operations, and an HTTPS CONNECT proxy restricted to explicitly
configured inference destinations. Reject private/reserved destination addresses
and arbitrary proxy targets. A local forwarding process can expose the inference
socket to the runtime's HTTP proxy support without granting external networking.
The broker must reject user actions and delivery operations independently of
model instructions.

Keep runtime configuration read-only, disable all agent tools and undeclared
plugins/MCP, and explicitly select OpenClaw's embedded runtime. Use only a
separately provisioned subscription OAuth profile, with no API-key fallback.
The source collector's credentials are never supplied to the reviewer.

Assessment sessions use disposable state. Remove session, prompt, and output
artifacts after each bounded run and on startup recovery; keep OAuth renewal
state separately permissioned. This prevents reviewer transcripts becoming an
unmanaged retention or deletion store.

The tradeoff is a small deterministic broker/proxy and explicit runtime setup.
It avoids dependence on host firewall changes and constrains the whole runtime,
including code outside the tool sandbox. Verify the actual pinned container's
permissions, sockets, and denied network paths before enabling an account.
Synthetic policy checks and container denial checks do not prove real model
judgment or OAuth renewal; those remain separately reported activation gates.
