"""Low-level transport clients (subprocess runners, HTTP adapters).

Clients know nothing about the domain. They execute a command or call
an external API and return the raw result. All domain rules live in
`services/`.
"""
