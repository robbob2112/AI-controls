# AI Controls

Source snapshot of AI Chat, Model Control, the status dashboard, hardware monitoring and cooling, and Benchmark Control.

- `share/` contains the AI Chat, Model Control and Benchmark Control web interfaces.
- `dashboard/` contains the status and benchmark results pages, chart code and dashboard server.
- `libexec/` contains the gateway, chat runtime, catalog and model-management code, hardware and cooling services, and benchmark queue and profiles.

This is a source export from a Linux installation. It expects locally configured inference services, authentication and TLS, hardware interfaces, data directories, benchmark fixtures, and optional lab or workspace services. Example IP addresses in the source use the documentation-only 192.0.2.0/24 range. Set the addresses and service configuration for your deployment. Local credentials, account data, model checkpoints, private configuration and operational records are excluded.

## Dependencies

| Component | Required on the target Linux host |
| --- | --- |
| Dashboard and AI Chat | Python 3.12+, SQLite, the dashboard and gateway source, and locally provisioned HTTPS certificate and key. Linux exposes hardware readings through sysfs and procfs. |
| Model Control | Ollama with a working local model store and outbound HTTPS for the public Ollama and Hugging Face catalogs and downloads. |
| GPU benchmarks | A supported GPU driver and render device, systemd, Ollama with Vulkan for Vulkan profiles, and a compatible llama.cpp build with Intel oneAPI for SYCL profiles. |
| Benchmark grading | The fixtures expected by the selected runner and its sandbox tools. Check the profile source for fixture paths, filenames, and formats before enabling it. |
| Optional services | A compatible BMC and local cooling configuration for fan control, a native llama.cpp router for separately registered GGUF models, and the LittleBit checkpoint with its PyTorch/Transformers environment for the CPU profile. |

The hardware IDs, ports, service names and paths reflect the original deployment layout. Adapt them before installation. The export does not bundle package locks, production service units, benchmark datasets, or model weights.

## How to use

1. Clone this repository on the target Linux host. If you have a local assistant with shell access, paste [the setup prompt](SETUP-PROMPT.md) into it from the clone. The prompt tells it to inspect your existing setup and configure the supported components.
2. Run `sudo python3 libexec/ai_branding.py` to choose the AI name. It saves the choice to `/etc/hal-controls/assistant-name`. Omit this step to use **Local AI**.
3. Start with a working Ollama instance and verify its loopback model inventory at `http://127.0.0.1:11434/api/tags`. Inventory your GPUs, drivers, VRAM, and available ports.
4. Stage the dashboard, gateway, and web assets in the paths expected by the source, or adjust the paths and URLs for your deployment. Configure local authentication, HTTPS, state directory permissions, and the benchmark broker socket before exposing controls. Start the dashboard and read-only monitoring first, then enable AI Chat and Model Control.
5. Open the status dashboard to check hardware readings. In **Model Control**, search a source, choose a variant, review its specs, and select **Download and register**. Open **AI Chat** and choose an installed model.
6. In **Benchmark Control**, pick the model, GPU, backend, workload, and duration. Start with a short supported test. Check the results page for run status, measurements, and failure details. Enable cooling control and additional benchmark profiles after validating their hardware assumptions and dependencies.

## First-time monitoring setup

On the target Linux host, run `python3 dashboard/hardware_inventory.py --setup` once with permission to write `/var/lib/hal-monitor/discovered-hardware.json`. Install `pciutils` to resolve GPU model names. The scanner falls back to PCI IDs. Setup reads hardware data and saves the inventory locally.

Open **Discovered hardware** on the status dashboard to see GPU models, drivers, and live readings for the saved sensor and fan channels. Missing devices show as unavailable. Rerun setup after hardware changes. Kernel labels are used when available. A raw fan channel may still need a physical label. Historical charts and fan controls use their existing configuration.

## Coding style

Use descriptive names, clear control flow, and comments that explain why a hardware or protocol
constraint matters. Keep service configuration and credentials out of source. Prefer small changes
that preserve existing routes and file formats. Format Python with Black at 100 columns. Format
JavaScript, CSS and HTML with Prettier at 100 columns and preserve HTML whitespace semantics.
Run checks locally before pushing. This repository does not require GitHub CI.

### Installed models and download sources

AI Chat, Model Control, and Benchmark Control share one installed-model catalog. It reads the Ollama registry, registered llama.cpp presets, and the optional LittleBit service live. There is no second installed-model database to reconcile. An imported model is registered in Ollama and appears in the shared catalog when its capabilities can be detected.

Model Control searches Ollama, Hugging Face GGUF, ModelScope GGUF, and GitHub release GGUF. For ModelScope and GitHub, you can enter a specific public repository as `owner/repository`. These sources import supported single-file GGUF assets into Ollama. Catalog discovery is separate from the installed inventory.
