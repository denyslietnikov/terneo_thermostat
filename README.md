# Terneo Thermostat for Home Assistant

[![hacs_badge](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://github.com/custom-components/hacs)

Custom integration for controlling Terneo thermostats over the local network.

## Supported Devices

- Terneo AX with local API access.

Available features depend on firmware; device validation is documented in
[PLAN.md](PLAN.md).

## Features

- Power, target temperature, heating/cooling, and schedule/manual modes.
- Temperature, relay state, current power, heating time, and estimated energy.
- Temperature limits, brightness, child lock, and other model-specific settings.
- Connection diagnostics and Home Assistant diagnostics download.

Energy is estimated from relay state and configured heater wattage; heating time
does not require wattage. Counters survive normal HA restarts and integration reloads.
The first upgrade starts new persistent totals; previous history is not imported.

## Installation

### HACS

1. Add `https://github.com/denyslietnikov/terneo_thermostat` as a custom repository
   in HACS, category **Integration**.
2. Install **Terneo Thermostat** and restart Home Assistant.

### Manual

Copy `custom_components/terneo` into your HA `config/custom_components` directory
and restart Home Assistant.

## Configuration

1. Enable local API access on the thermostat (`bLc` = `oFF`). Use a trusted network.
2. Open **Settings > Devices & Services > Add Integration**, search for **Terneo**,
   and enter the thermostat's IP address and serial number.
3. Choose a device name and finish setup.

Options: status interval (default 30 seconds), settings interval (default 5 minutes),
request timeout (default 5 seconds), and advanced sensors. Diagnostic sensors are
disabled by default; enable them individually in their entity settings.

## Actions

- `terneo.set_floor_limits`: set minimum/maximum floor temperatures.
- `terneo.set_air_limits`: set air limits on supported models.
- `terneo.restart`: restart the selected thermostat.

These actions require an explicit entity, device, or area target.

## Troubleshooting

- **Cannot connect:** check the IP address, serial number, network, and local API access.
- **Unavailable:** brief polling failures retain previous readings, which may be
  stale. Prolonged failures mark the device unavailable; polling restores it on recovery.
  Failed settings reads affect configuration entities without blocking telemetry.
- **Command failed:** check the actual state before retrying; a timed-out command
  may already have applied.
- **Diagnostics:** enable connection sensors or select **Download diagnostics**
  from the integration menu when reporting an issue.

## Development

Implementation details, protocol references, test instructions, verification
results, and the improvement roadmap are in [PLAN.md](PLAN.md).

## License and Credits

MIT. Based on work by [@Makave1i](https://github.com/Makave1i),
[@DevRedOWL](https://github.com/DevRedOWL), and [@titovskiy](https://github.com/titovskiy).
