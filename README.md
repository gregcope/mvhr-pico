# MVHR Monitor and Boost Controller

MicroPython firmware designed for a Raspberry Pi Pico two W to monitor duct air quality and control an MVHR boost relay via MQTT and Home Assistant discovery. This document serves as both a human-readable guide and a precise execution specification for GenAI software engineering agents.

## Repository Structure

```text
mvhr-pico/
├── main.py              # Core application logic, async loop, hardware drivers and MQTT management
├── secrets.py           # Local operational credentials (git-ignored)
├── secrets_example.py   # Template for network and broker credentials
└── README.md            # Project documentation and agent execution specification

```

## Overview

This project provides environmental monitoring and automation for Mechanical Ventilation with Heat Recovery systems. It polls Sensirion SHT45 sensors across four distinct duct channels using an HW-617 I2C multiplexer, applies median filtering and delta thresholds, and publishes telemetry to Home Assistant. Additionally, it manages an opto-isolated solid-state relay for the MVHR boost function.

## Architectural Decisions and Strict Constraints

* **No Local Logging (Strict Constraint):** Local file logging is entirely prohibited to prevent unnecessary write wear on the microcontroller flash storage. All diagnostics and telemetry are routed live via MQTT to Home Assistant.
* **Asynchronous Execution (Strict Constraint):** The firmware must operate entirely within an asynchronous `asyncio` event loop. Blocking synchronous I/O operations are forbidden to prevent freezing the event loop.
* **Resilient Network Management:** Employs an exponential backoff retry loop coupled with a hardware watchdog timer. If Wi-Fi or MQTT communication fails repeatedly, the system safely backs off before triggering a clean hardware reset.
* **Sensor Noise Reduction:** Implements a three-sample median filter combined with delta thresholds for temperature and humidity to eliminate raw sensor jitter and prevent excessive MQTT state chatter.
* **Hardware Abstraction for Relays:** Decouples logical switch states from physical relay board requirements, cleanly handling active-high solid-state triggering and separate power supplies.

## Hardware Architecture and Pin Mappings

* **Microcontroller:** Raspberry Pi Pico two W
* **I2C Bus Pins:** SCL on GPIO six, SDA on GPIO four (running at four hundred kilohertz bus frequency)
* **Multiplexer:** HW-617 / PCA9548A I2C multiplexer connected to GPIO ten for hardware reset; address set to `0x70`
* **Sensors:** Four SHT45 sensors mapped to Intake, Supply, Extract and Exhaust ducts via channel addresses two through five at address `0x44`
* **Relay Output:** Pololu Isolated Solid-State Relay powered via VBUS (`5`V) and controlled via GPIO nine using active-high logic (`1` for ON, `0` for OFF)

## Agent-Executable Runtime and Timing Specifications

* **Watchdog Timeout:** `8000` ms (`8` seconds). The hardware watchdog must be fed on every iteration of the main loop.
* **Exponential Backoff Sequence:** Initial backoff is set to `1.0` second, scaling by a multiplier of two on each failure up to a maximum cap of `60.0` seconds.
* **Failure Threshold:** Reaching twenty consecutive failed connection attempts triggers an automatic hardware reset via `machine.reset()`.
* **Sensor Polling Cycle:** Ten-second sleep combined with a two-second poll yield, establishing a twelve-second total cycle duration.

## Error Handling and Exception Paths

* **`MultiplexerError`:** Raised when I2C writes to the PCA9548A fail. Caught and isolated during channel switching to prevent crashing the main loop.
* **`SensorReadError`:** Raised when command transmission or data read fails for an individual SHT45 sensor. Increments a failure counter; if failures reach three consecutive attempts, the specific duct availability is published as `offline`.
* **Exception Isolation:** Bare exception blocks are avoided where possible, ensuring socket errors, MQTT drops and OS errors are caught and handled gracefully without tearing down a valid client state.

## MQTT Discovery and Data Contracts

* **Master Availability Topic:** `homeassistant/sensor/mvhr_monitor/availability` (`online` / `offline` with retained LWT).
* **Relay State / Command Topics:**
* Command: `homeassistant/switch/mvhr_monitor_relay/set`
* State: `homeassistant/switch/mvhr_monitor_relay/state`


* **Duct State Payload Schema:**
```json
{
  "temperature": 21.5,
  "humidity": 45.2
}

```


* **System Diagnostics Payload Schema:**
```json
{
  "pico_temp": 28.4,
  "rssi": -65,
  "uptime": 3600,
  "version": "1.3.5",
  "reconnects": 1,
  "last_reset": "Power On",
  "status": "Healthy"
}

```


## Configuration

The firmware uses a clear separation between operational parameters and sensitive credentials:

* **Tunables & Hardware Pins:** Defined at the top of `main.py` (including sensor polling intervals, delta thresholds and GPIO assignments).
* **Network Secrets:** Managed via a separate `secrets.py` file templated by `secrets_example.py`, keeping Wi-Fi credentials and MQTT broker details secure and out of version control.

## Code Style and Standards

The codebase adheres to strict MicroPython design standards:

* **PEP8 & Formatting:** Strict adherence to snake_case variable naming and clean code structure.
* **Static Type Hints:** Fully typed functions and methods across all modules to assist static analysis and agent comprehension.
* **Modular Design:** Clear separation of concerns encapsulated within dedicated classes (`NetworkManager`, `I2CMultiplexer` and `SHT45`).

## Major Version History

* **v1.3.5:** Stable production release featuring fully verified active-high solid-state relay switching logic, correct default idle states and robust Home Assistant integration.
* **v1.3.4:** Introduced Home Assistant Device Registry metadata mapping and unique factory identifier support.
* **v1.3.3:** Initial release featuring multi-channel SHT45 polling, median filtering and MQTT discovery.
