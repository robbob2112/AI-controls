# HAL Controls

Source snapshot of HAL Chat, Model Control, the status dashboard, hardware monitoring and cooling, and Benchmark Control.

- `share/` contains the HAL Chat, Model Control and Benchmark Control web interfaces.
- `dashboard/` contains the status and benchmark results pages, chart code and dashboard server.
- `libexec/` contains the gateway, chat runtime, catalog and model-management code, hardware and cooling services, and benchmark queue and profiles.

This is a source export from a Linux installation. It expects locally configured inference services, authentication and TLS, hardware interfaces, data directories, benchmark fixtures, and optional lab or workspace services. Example IP addresses in the source use the documentation-only 192.0.2.0/24 range. Set the addresses and service configuration for your deployment. Local credentials, account data, model checkpoints, private configuration and operational records are excluded.
