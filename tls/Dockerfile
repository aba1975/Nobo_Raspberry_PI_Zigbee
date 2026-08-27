# Caddy for Nobø Control.
#
# Two ways of getting a certificate are supported, and they need different
# binaries:
#
#   Caddy's own CA (the default)  - stock Caddy. Nothing to compile.
#   Let's Encrypt over DNS-01     - needs a DNS provider plugin compiled in,
#                                   because stock Caddy ships none.
#
# The stage that provides the binary is chosen by CADDY_BINARY_SOURCE, and
# BuildKit only builds the stages it actually needs. So the default path pulls
# an image and finishes in seconds, and nobody compiles Go unless they asked
# for ACME.

ARG CADDY_VERSION=2

# Which stage supplies the binary. This has to be declared up here, before the
# first FROM: an ARG used in a FROM line is only visible if it is global, and
# one declared further down expands to nothing ("base name should not be
# blank"). Declaring it twice is not the answer either — the value has to live
# in the global scope.
ARG CADDY_BINARY_SOURCE=caddy-stock

# --- Fast path: the official binary, unmodified. ---------------------------
FROM caddy:${CADDY_VERSION} AS caddy-stock

# --- ACME path: rebuild with a DNS provider. -------------------------------
# Swap the plugin for your DNS host: github.com/caddy-dns/cloudflare,
# .../route53, .../desec and so on. The full list is at
# https://github.com/orgs/caddy-dns/repositories
#
# acme-dns is the default because it works with any DNS host, including the
# many that offer no API at all.
FROM caddy:${CADDY_VERSION}-builder AS caddy-plugins
ARG CADDY_DNS_PLUGIN=github.com/caddy-dns/acmedns
RUN xcaddy build --with ${CADDY_DNS_PLUGIN}

# --- Whichever of the two was asked for. -----------------------------------
FROM ${CADDY_BINARY_SOURCE} AS chosen

FROM caddy:${CADDY_VERSION}
COPY --from=chosen /usr/bin/caddy /usr/bin/caddy
