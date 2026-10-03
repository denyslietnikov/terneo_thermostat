# Terneo AX Command Audit

Date: 2026-10-03. Scope: the two configured Terneo AX thermostats, not newer
Welrok OZ/AZ hardware. Identifying values and credentials are deliberately omitted.

## Firmware and Evidence

Both device web interfaces report firmware `2.24.1.Y.20.2.20.12.43` and model AX.
HA's device registry has no firmware value and labels them `OZ (Legacy)`;
that integration-generated label is not evidence of the actual model.

The initial audit used GET requests for device pages and POST read commands
`cmd:1` and `cmd:4`. A subsequent explicitly authorized kitchen-only write
check is recorded below. No restarts, schedule edits, authentication changes,
or router changes were made.

The Terneo API reference is labeled 2.5 and describes features introduced in
2.3/2.4. It is not an exact build-specific specification for this firmware.
The decisive evidence for the mode encoding is the actual device response,
which agrees with the separate parameter and telemetry tables:

```json
{"par":[[2,2,"1"],[5,1,"16"],[118,7,"0"],[125,7,"1"]]}
```

```json
{"m.1":"3","m.5":"0","f.16":"1","f.0":"0","t.5":"256"}
```

These excerpts are sanitized, not complete responses. The active AX returned
both excerpts; the other AX returned the same manual parameter encoding.

## Corrected Mode Commands

Parameter `2` is a stored mode selector: schedule `0`, manual `1`.
Telemetry `m.1` is an effective operating state: schedule `0`, manual `3`,
away `4`, temporary `5`. The codes must not be interchanged.
`OperationMode` remains the telemetry enum; the client encodes it for writes.

Every write has the matching `sn` and a `par` list at `/api.cgi`.
For this legacy Terneo profile, the complete batches are:

| Action | Parameter records |
| --- | --- |
| OFF | `[[125,7,"1"]]` |
| AUTO / schedule preset | `[[125,7,"0"],[2,2,"0"]]` |
| HEAT | `[[125,7,"0"],[118,7,"0"],[2,2,"1"]]` |
| COOL | `[[125,7,"0"],[118,7,"1"],[2,2,"1"]]` |
| Manual preset | `[[125,7,"0"],[2,2,"1"]]` |
| Floor setpoint 27 C | `[[125,7,"0"],[2,2,"1"],[5,1,"27"]]` |

OFF preserves stored mode; AUTO and presets preserve heating/cooling selection.
Parameter writes do not contain a read-command `cmd`. Temperature parameters
for this device use integer Celsius/int8, while telemetry uses 1/16 Celsius.
Parameter batches are documented, but firmware-level atomicity is not guaranteed.

The acknowledgement checker compares parameter `2` against the write code `1`,
not telemetry `3`. A response with manual parameter `3` cannot confirm this write.
No speculative fallback writes or automatic retries are introduced.

## Other Parameter Commands

The following parameter numbers and types agree with Terneo documentation
and the parameter list read from this firmware. Tests check outgoing records;
they do not prove successful writes on physical devices.

| Settings | Number / type | Encoding |
| --- | --- | --- |
| Power off / cooling | 125 / bool, 118 / bool | String `0` or `1` |
| Control / sensor type | 3 / uint8, 18 / uint8 | Integer selector |
| Manual / away floor setpoint | 5 / int8, 7 / int8 | Integer Celsius |
| Floor limits | 26 / int8, 27 / int8 | Integer Celsius |
| Hysteresis | 19 / uint8 | Tenths of Celsius |
| Floor correction | 21 / int8 | Tenths of Celsius |
| Brightness / proportional coefficient | 23 / uint8, 25 / uint8 | Integer value |
| Night start / end | 52 / uint16, 53 / uint16 | Minutes after midnight |
| LAN / cloud block | 114 / bool, 115 / bool | String `0` or `1` |
| Relay inversion / night brightness | 117 / bool, 120 / bool | String `0` or `1` |
| Preheat / child lock | 121 / bool, 124 / bool | String `0` or `1` |

## Unverified Contracts

- Parameter acknowledgements are documented as an updated parameter list for
  API version 2.5. The kitchen AX instead returned only `{"success":"true"}`
  both for ignored writes in the initial check and for applied writes in the
  HA/local comparison below. Legacy short receipts now require a separate
  identity-bearing parameter readback confirming every requested type and value.
  The marker alone is never accepted as proof that settings changed.
- `/test.cgi` with `{"cmd":"restart"}` and a string `success=true` acknowledgement
  is an existing client contract, not documented by the referenced Terneo API.
- Button sensitivity parameters 80/81/82 are absent from both the retrieved AX
  parameter list and the Terneo parameter reference. Do not claim support on
  this firmware from the Welrok documentation alone.
- Parameter 55 appears in the retrieved list, but this does not establish its
  write semantics. Connected-power conversion also needs separate validation:
  the reference's displayed formula differs from the client's formula. Neither
  was changed without independent evidence of the correct conversion.
- Control-selector values listed in the common API do not prove that this AX
  contains an air sensor. New-device air/BLE/warning commands remain outside
  this firmware audit.
- The new-device profile retains its existing manual parameter value `3`.
  Welrok's English and Russian parameter tables disagree; compatibility with
  newer Terneo or Welrok firmware needs model-specific verification.

## Authorized Kitchen Write Check

This initial check preceded the HA/local comparison below. The then-current
local `TerneoThermostat` client was exercised directly, not deployed
into the running HA integration. Baseline: off, manual parameter `1`, heating
selected, floor setpoint 16 C, floor reading about 21.3 C, relay off.
All 30 parameter records were saved before the first write; prerequisites
were checked again before each diagnostic attempt. Nearby time-triggered
automations and grid-input state were inspected, but no automation was changed.

The intended HEAT / 16 -> 17 -> 16 C / manual preset / OFF sequence stopped
at the first failed acknowledgement. Only a limited transport investigation
followed; the remaining client transitions were not executed.

| Attempt | Wire format | Response | Independent readback |
| --- | --- | --- | --- |
| Client HEAT batch | `requests.post(json=...)`, application/json | `{"success":"true"}` | Still off, 16 C, manual parameter 1 |
| Single power-on | Compact JSON, application/json | Same marker | Still off |
| Single power-on | Compact JSON, text/plain | Same marker | Still off |
| Single floor setpoint 17 C | Compact JSON, text/plain;charset=UTF-8, keep-alive | Same marker | Still 16 C after a 5-second wait |

Each attempt was followed by an explicit restoration batch from the saved
baseline and a full parameter/telemetry readback. Across all four checks there
were eight write attempts including four restoration attempts. Every final
parameter list exactly matched the saved 30 records, and power and relay were
off. No write was sent to the bathroom AX. No observed relay activation occurred.
The restored values were verified by reading them, not inferred from the
restoration requests' identical `success` responses.

Conclusion: physical control did **not** pass validation. An HTTP 200 response
and `success=true` were not evidence of application on this firmware. The new
client rejected the incomplete acknowledgement; the subsequent comparison
showed that rejection alone is too strict for successfully applied AX writes.
A regression test
now reproduces this marker followed by unchanged settings, preserving OFF and
the command error without automatically repeating the write.

This check does not establish why the firmware did not apply the writes, nor
does it prove the commands can never work. Authentication, transport/firmware
behavior, and any model-specific restrictions need further evidence. Do not
guess by changing LAN/cloud locks or restarting/upgrading the device.
At this point AUTO, COOL, manual preset, successful temperature writes, and newer
devices were physically unverified. The later comparison closes only the specific
gaps listed below.

## HA MCP and Local Comparison

On 2026-10-03, the requested comparison first exercised the running integration
through HA MCP, then repeated the same sequence with the actual local client.
The new baseline was HEAT/manual 16 C, parameter 2 = 1, relay off, with both LAN
and cloud blocks = 0. The initial check above had cloud block = 1 and power off.
No test changed either lock; this difference is not a controlled causal test.
All 30 baseline parameter records were saved before writes.

| Step | HA MCP and independent readback | Fixed local client and independent readback |
| --- | --- | --- |
| Set 17 C, HEAT | Parameters 5/31 = 17; t.5 = 272; power on | Same, command confirmed |
| Set 16 C, HEAT | Parameters 5/31 = 16; t.5 = 256; power on | Same, command confirmed |
| OFF | Parameter 125 = 1; f.16 = 1; effective mode -1 | Same, command confirmed |
| HEAT | Parameter 125 = 0; f.16 = 0; effective mode 3 | Same, command confirmed |
| Manual preset | Parameter 2 = 1; m.1 = 3; 16 C, power on | Same, command confirmed |

Before the fix, local writes also changed the device, but the client returned
failure because all write replies contained only `{"success":"true"}`. One
independent full poll after the manual step timed out; restoration and its full
readback subsequently succeeded. No failed write was automatically repeated.

The fix applies only to the legacy profile: a short successful receipt triggers
one `cmd=1` read inside the command lock. Identity, complete requested parameters,
types, and values must match. This does not publish partial cached state and
does not repeat the write. New-profile strict acknowledgement rules are unchanged.
Regression tests cover applied and ignored writes, transport/readback failure,
malformed receipts, cache publication, and locking using real HA APIs.

The repeated fixed-client run passed every step. Its explicit final restoration
was followed by a full parameter/telemetry poll: all 30 records matched the new
baseline exactly, HEAT/manual 16 C with relay off. The relay was off in every
observed readback, and no write targeted the bathroom thermostat. Automations,
schedules, router configuration, and LAN/cloud locks were not modified.

This verifies the tested kitchen firmware's writes and acknowledgement handling,
not long-term network reliability. AUTO, COOL, newer devices, restart, minimum HA
version, and deployment of this new code into the running HA remain unverified.

## Sources

- [Terneo API introduction](https://terneo-api.readthedocs.io/intro.html)
- [Terneo parameters](https://terneo-api.readthedocs.io/parameters.html)
- [Terneo telemetry](https://terneo-api.readthedocs.io/telemetry.html)
- [Terneo commands](https://terneo-api.readthedocs.io/commands.html)
- [Terneo safety](https://terneo-api.readthedocs.io/safety.html)

Welrok references are secondary and do not establish the Terneo AX contract:

- [New OZ English parameters](https://welrok-local-api.readthedocs.io/OZ/en/parameters.html)
- [New OZ Russian parameters](https://welrok-local-api.readthedocs.io/OZ/ru/parameters_ru.html)
