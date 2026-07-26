# Svakom SL278H BLE Protocol

Reverse-engineered BLE protocol for the **Svakom SL278H** vibrator/clapper, including vibration, patterns, clapping (pat), and **heating** commands — features not documented elsewhere.

## Device Info

| Field | Value |
|-------|-------|
| Device Name | `SL278H` |
| BLE Service UUID | `0000ffe0-0000-1000-8000-00805f9b34fb` |
| Write Characteristic (commands) | `0000ffe1-0000-1000-8000-00805f9b34fb` |
| Notify Characteristic (status) | `0000ffe2-0000-1000-8000-00805f9b34fb` |
| Write Type | Write Without Response |

## Command Format

All commands start with `0x55` and are 7 bytes long:

```
55 [CMD] [B2] [B3] [B4] [B5] [B6]
```

## Commands (Write to `ffe1`)

### Vibration (CMD `0x04`)

Control vibration motor intensity.

```
55 04 00 00 [ON] [LEVEL] AA
```

| Byte | Description |
|------|-------------|
| B4 (`ON`) | `01` = on, `00` = off |
| B5 (`LEVEL`) | `0x00`–`0xFF` (0–255). Intensity level |
| B6 | `0xAA` (trailer) |

**Examples:**
- 50% vibration: `55 04 00 00 01 80 AA`
- Full vibration: `55 04 00 00 01 FF AA`
- Stop vibration: `55 04 00 00 00 00 AA`

### Pattern / Mode (CMD `0x03`)

Activate a vibration pattern.

```
55 03 00 00 [MODE] [LEVEL] 00
```

| Byte | Description |
|------|-------------|
| B4 (`MODE`) | `01`–`08` (8 patterns) |
| B5 (`LEVEL`) | `01`–`05` (5 intensity levels) |

**Example:**
- Pattern 3, level 4: `55 03 00 00 03 04 00`

### Clap / Pat (CMD `0x07`)

External patting/tapping stimulation.

```
55 07 00 00 [LEVEL] 00 00
```

| Byte | Description |
|------|-------------|
| B4 (`LEVEL`) | `01`–`07` (7 intensity levels). `00` = stop |

**Examples:**
- Clap level 3: `55 07 00 00 03 00 00`
- Clap level 7 (max): `55 07 00 00 07 00 00`
- Stop clap: `55 07 00 00 00 00 00`

### Heating (CMD `0x05`)

Control the built-in heating element. Target temperature ~55°C (0x37).

```
Heat ON:  55 05 01 37 00 00 00
Heat OFF: 55 05 00 00 00 00 00
```

| Byte | Description |
|------|-------------|
| B2 | `01` = on, `00` = off |
| B3 | Target temperature in hex (`0x37` = 55°C). `00` when off |

### Stop All

Send vibration stop command:

```
55 04 00 00 00 00 AA
```

> **Note:** Stopping vibration may also stop the clapper. If you only want to stop vibration, re-send the clap command afterward.

## Notify Responses (from `ffe2`)

The device sends status updates on the notify characteristic:

| Response | Meaning |
|----------|---------|
| `55 05 01 37 00 00 00` | Heating ON, current temp 0x37 (55°C) |
| `55 05 00 00 00 00 00` | Heating OFF |
| `55 07 00 00 XX 00 00` | Clap status, level XX |
| `55 02 0A 01 00 00 00 00 00` | Device status report |
| `55 00 0F 00 00 ...` | Device info / serial |

## Keepalive

The device requires periodic re-sending of the current command every **~1.5 seconds**, otherwise it will stop. This applies to vibration, pattern, and clap commands. Heating does not appear to need keepalive.

## Remote Control Architecture

This repo includes a relay-based remote control system:

```
AI/Claude  →  POST /toy-cmd  →  Relay Server  →  GET /toy-next  →  BLE Bridge (Chrome)  →  Web Bluetooth  →  SL278H
```

1. **Relay Server** (`relay/server.py`) — Python HTTP server, queues commands
2. **BLE Bridge** (`bridge/toy.html`) — Web Bluetooth page, runs on Android Chrome, polls relay and writes BLE commands
3. **AI** sends JSON commands: `{"speed": 0.5}`, `{"clap": 3}`, `{"heat": true}`, `{"stop": true}`

### JSON Command Reference

| Command | Example | Description |
|---------|---------|-------------|
| `speed` | `{"speed": 0.5}` | Vibration 0.0–1.0 |
| `pattern` | `{"pattern": 3, "level": 0.8}` | Pattern 1–8, level 0.0–1.0 |
| `clap` | `{"clap": 5}` | Clap level 1–7 |
| `heat` | `{"heat": true}` | Heating on/off |
| `stop` | `{"stop": true}` | Stop all |
| `raw` | `{"raw": "5504000001FFAA"}` | Raw hex command |
| `sec` | `{"speed": 0.8, "sec": 30}` | Auto-stop after N seconds (optional, for any command) |

> **提示（中文）：** `sec` 是可选参数，加在任何命令里，表示 N 秒后自动停止。不加则一直运行，直到 AI 主动发 `{"stop": true}`。例：震动 30 秒后自动停 → `{"speed": 0.7, "sec": 30}`

## Setup

### 1. Relay Server

```bash
python3 relay/server.py
# Runs on port 18099
```

### 2. Expose via Cloudflare Tunnel (optional)

```bash
cloudflared tunnel --url http://localhost:18099
```

### 3. BLE Bridge

Open `bridge/toy.html` on Android Chrome (Web Bluetooth required). Click "Connect Toy", select SL278H.

### 4. Send Commands

```bash
# Vibrate at 50%
curl -X POST http://localhost:18099/toy-cmd -d '{"speed": 0.5}'

# Clap level 5
curl -X POST http://localhost:18099/toy-cmd -d '{"clap": 5}'

# Heat on
curl -X POST http://localhost:18099/toy-cmd -d '{"heat": true}'

# Stop
curl -X POST http://localhost:18099/toy-cmd -d '{"stop": true}'
```

## Reversing Methodology

Commands were reverse-engineered using **nRF Connect** on Android:
1. Subscribe to notify characteristic (`ffe2`) to observe device responses
2. Write candidate commands to write characteristic (`ffe1`)
3. Cross-reference with the Svakom official app's behavior
4. Heating command captured by toggling heat in the official app while monitoring BLE traffic

## Credits

- Protocol analysis by **柠柠 & 肸奕** (2026-07)
- Inspired by [buttplugio/stpihkal](https://github.com/buttplugio/stpihkal) Svakom documentation
- Reference: [vickyldr/svakom-ble-ai](https://github.com/vickyldr/svakom-ble-ai)

## License

MIT
