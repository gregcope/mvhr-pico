"""MicroPython firmware for the MVHR monitor and relay controller.

Running on a Raspberry Pi Pico 2 W with HW-617 multiplexer and SHT45 sensors.
"""

import asyncio
import json
import os
import time
try:
    from typing import Any, Dict, List, Optional
except ImportError:
    Any = object
    Dict = dict
    List = list
    Optional = object
import machine
import network
import secrets
import ugit
from ubinascii import hexlify

try:
    from simple import MQTTClient
except ImportError:
    from umqtt.simple import MQTTException, MQTTClient

app_version: str = "1.7.0"

# ==========================================
# 1. CONFIGURATION
# ==========================================
client_id: str = "mvhr_monitor"
sensor_interval_secs: int = 10
heartbeat_interval_secs: int = 300
sensor_change_threshold_t: float = 0.2  # Degrees Celsius
sensor_change_threshold_h: float = 1.0  # Percentage Relative Humidity

# Hardware and Network Timing
watchdog_timeout_ms: int = 8000
mqtt_keepalive_grace_secs: int = 20
initial_backoff_secs: float = 1.0
max_backoff_secs: float = 60.0
max_failed_attempts: int = 20

# Hardware Pin Assignments
i2c_scl_pin: int = 6
i2c_sda_pin: int = 4
mux_reset_pin_num: int = 10
relay_pin_num: int = 9

# I2C Addresses
mux_address: int = 0x70
sht_address: int = 0x44

# Channel mapping to MVHR ducts (four active channels)
active_ducts: Dict[int, Dict[str, Any]] = {
    # intake (Silver/White)
    2: {
        "id": "intake",
        "name": "Intake",
        "last_temp": -999.0,
        "last_hum": -999.0,
        "last_avail": "unknown",
        "history_t": [],
        "history_h": [],
        "fail_count": 0,
    },
    # supply (Plain)
    3: {
        "id": "supply",
        "name": "Supply",
        "last_temp": -999.0,
        "last_hum": -999.0,
        "last_avail": "unknown",
        "history_t": [],
        "history_h": [],
        "fail_count": 0,
    },
    # extract (Silver/Silver)
    4: {
        "id": "extract",
        "name": "Extract",
        "last_temp": -999.0,
        "last_hum": -999.0,
        "last_avail": "unknown",
        "history_t": [],
        "history_h": [],
        "fail_count": 0,
    },
    # exhaust (Gold)
    5: {
        "id": "exhaust",
        "name": "Exhaust",
        "last_temp": -999.0,
        "last_hum": -999.0,
        "last_avail": "unknown",
        "history_t": [],
        "history_h": [],
        "fail_count": 0,
    },
}

# --- System Internals ---
system_start_time_secs: float = time.time()
watchdog: Optional[machine.WDT] = None
onboard_led: machine.Pin = machine.Pin("LED", machine.Pin.OUT, value=1)
internal_temp_sensor: machine.ADC = machine.ADC(4)
device_serial: str = hexlify(machine.unique_id()).decode()

system_status: str = "Initializing"
last_published_status: str = ""
force_sensor_publish: bool = False


class MultiplexerError(Exception):
    """Raised when the I2C multiplexer bus communications fail."""

    pass


class SensorReadError(Exception):
    """Raised when an individual SHT45 sensor read fails."""

    pass


def get_reset_cause() -> str:
    """Determine the reset cause, prioritizing OTA firmware updates."""
    try:
        with open("/ugit_log.txt", "r") as log_file:
            log_data: str = log_file.read()
        if "/main.py updated" in log_data:
            os.remove("/ugit_log.txt")
            return "Firmware Upgrade"
    except OSError:
        pass

    cause: int = machine.reset_cause()
    if cause == machine.PWRON_RESET:
        return "Power On"
    if cause == machine.WDT_RESET:
        return "Watchdog Timer"
    if cause == machine.SOFT_RESET:
        return "Software Reset"
    return "Unknown"


last_reset_reason: str = get_reset_cause()

for i in range(10, 0, -1):
    onboard_led.toggle()
    time.sleep(0.2)


class I2CMultiplexer:
    """Controls a PCA9548A / TCA9548A I2C multiplexer."""

    def __init__(
        self, i2c_bus: machine.SoftI2C, address: int = mux_address
    ) -> None:
        self.i2c: machine.SoftI2C = i2c_bus
        self.address: int = address

    def select_channel(self, channel: int) -> None:
        """Enables exactly one of the eight I2C channels."""
        if not 0 <= channel <= 7:
            raise ValueError("Channel must be between zero and seven")

        try:
            self.i2c.writeto(self.address, bytes([1 << channel]))
        except OSError as e:
            raise MultiplexerError(
                f"Failed to switch to multiplexer channel {channel}"
            ) from e


class SHT45:
    """Driver for the Sensirion SHT45 temperature and humidity sensor."""

    def __init__(
        self, i2c_bus: machine.SoftI2C, address: int = sht_address
    ) -> None:
        self.i2c: machine.SoftI2C = i2c_bus
        self.address: int = address

    async def read_temp_humidity(self) -> tuple[float, float]:
        """Triggers a high-precision measurement and returns temperature and humidity."""
        try:
            self.i2c.writeto(self.address, b"\xFD")
        except OSError as e:
            raise SensorReadError(
                "Failed to send measurement command to SHT45"
            ) from e

        await asyncio.sleep_ms(10)

        try:
            data: bytes = self.i2c.readfrom(self.address, 6)
        except OSError as e:
            raise SensorReadError(
                "Failed to read data bytes from SHT45"
            ) from e

        t_ticks: int = (data[0] << 8) | data[1]
        rh_ticks: int = (data[3] << 8) | data[4]

        temperature: float = -45.0 + (175.0 * t_ticks / 65535.0)
        humidity: float = -6.0 + (125.0 * rh_ticks / 65535.0)

        return round(temperature, 2), round(humidity, 1)


async def reset_multiplexer(reset_pin: machine.Pin) -> None:
    """Pulls the HW-617 reset pin low briefly to reset the device."""
    reset_pin.value(0)
    await asyncio.sleep_ms(10)
    reset_pin.value(1)
    await asyncio.sleep_ms(10)


async def flash_led(led_pin: machine.Pin) -> None:
    """Briefly flashes the onboard LED on successful read."""
    led_pin.value(1)
    await asyncio.sleep_ms(50)
    led_pin.value(0)


# ==========================================
# 2. THE NETWORK MANAGER
# ==========================================
class NetworkManager:
    """Manages WiFi connectivity, MQTT communication, exponential backoff, and discovery payloads."""

    def __init__(
        self, client_id: str, broker: str, user: str, password: str
    ) -> None:
        self.client_id: str = client_id
        self.broker: str = broker
        self.user: str = user
        self.password: str = password
        self.client: Optional[MQTTClient] = None
        self.failed_attempts: int = 0
        self.reconnects: int = 0
        self.current_backoff: float = initial_backoff_secs

        self.master_avail_topic: str = (
            f"homeassistant/sensor/{self.client_id}/availability"
        )
        self.relay_state_topic: str = (
            f"homeassistant/switch/{self.client_id}_relay/state"
        )
        self.relay_cmd_topic: str = (
            f"homeassistant/switch/{self.client_id}_relay/set"
        )
        self.ota_cmd_topic: str = (
            f"homeassistant/button/{self.client_id}_ota/set"
        )

    async def maintain_connection(self) -> bool:
        """Maintain robust network and MQTT connectivity using exponential backoff."""
        global system_status
        wlan: network.WLAN = network.WLAN(network.STA_IF)
        wlan.active(True)

        if wlan.isconnected() and self.client is not None:
            return True

        system_status = "Connecting"
        if not wlan.isconnected():
            wlan.connect(secrets.wifiSsid, secrets.wifiPassword)
            for _ in range(10):
                if wlan.isconnected():
                    break
                if watchdog is not None:
                    watchdog.feed()
                await asyncio.sleep(1)

        if wlan.isconnected():
            try:
                unique_id: str = self.client_id + "_" + device_serial
                self.client = MQTTClient(
                    unique_id,
                    self.broker,
                    user=self.user,
                    password=self.password,
                    keepalive=heartbeat_interval_secs
                    + mqtt_keepalive_grace_secs,
                )

                lwt_topic_bytes: bytes = self.master_avail_topic.encode(
                    "utf-8"
                )
                self.client.set_last_will(
                    lwt_topic_bytes, b"offline", retain=True
                )

                self.client.connect()
                self.publish(self.master_avail_topic, "online", retain=True)

                # --- OTA Validation Handshake ---
                try:
                    os.remove("ota_pending.flag")
                except OSError:
                    pass

                self.reconnects += 1
                self.failed_attempts = 0
                self.current_backoff = initial_backoff_secs
                system_status = "Healthy"
                return True
            except (OSError, MQTTException):
                self.client = None

        self.failed_attempts += 1
        system_status = (
            f"Network Error ({self.failed_attempts}/{max_failed_attempts})"
        )

        sleep_steps: int = int(self.current_backoff)
        for _ in range(max(1, sleep_steps)):
            if watchdog is not None:
                watchdog.feed()
            await asyncio.sleep(1)

        self.current_backoff = min(
            self.current_backoff * 2.0, max_backoff_secs
        )

        if self.failed_attempts >= max_failed_attempts:
            machine.reset()

        return False

    def publish(
        self, topic: str, payload_data: Any, retain: bool = True
    ) -> bool:
        """Publish data payload to specified MQTT topic."""
        if self.client is None:
            return False
        try:
            t_bytes: bytes = topic.encode("utf-8")
            p_bytes: bytes = (
                json.dumps(payload_data).encode("utf-8")
                if isinstance(payload_data, dict)
                else str(payload_data).encode("utf-8")
            )
            self.client.publish(t_bytes, p_bytes, retain=retain)
            return True
        except (OSError, MQTTException):
            self.client = None
            return False

    def check_messages(self) -> None:
        """Check for incoming MQTT messages."""
        if self.client:
            try:
                self.client.check_msg()
            except (OSError, MQTTException):
                self.client = None

    def send_discovery(self) -> bool:
        """Send Home Assistant MQTT discovery configuration."""
        try:
            device_info: Dict[str, Any] = {
                "identifiers": [self.client_id, device_serial],
                "name": "MVHR Monitor",
                "model": "Raspberry Pi Pico 2 W",
                "sw_version": app_version,
                "serial_number": device_serial,
            }

            # Register Relay Switch
            switch_config_topic: str = (
                f"homeassistant/switch/{self.client_id}_relay/config"
            )
            switch_payload: Dict[str, Any] = {
                "name": "Boost",
                "unique_id": f"{self.client_id}_relay",
                "command_topic": self.relay_cmd_topic,
                "state_topic": self.relay_state_topic,
                "availability_topic": self.master_avail_topic,
                "device": device_info,
            }
            self.publish(switch_config_topic, switch_payload)
            time.sleep(0.1)

            # Register OTA Update Button
            ota_config_topic: str = (
                f"homeassistant/button/{self.client_id}_ota/config"
            )
            ota_payload: Dict[str, Any] = {
                "name": "Update Firmware",
                "unique_id": f"{self.client_id}_ota",
                "command_topic": self.ota_cmd_topic,
                "device_class": "update",
                "availability_topic": self.master_avail_topic,
                "device": device_info,
            }
            self.publish(ota_config_topic, ota_payload)
            time.sleep(0.1)

            # Register Duct Sensors (Temperature and Humidity)
            for channel, info in active_ducts.items():
                duct_id: str = info["id"]
                duct_name: str = info["name"]

                for measure_type, d_class, unit in [
                    ("temperature", "temperature", "°C"),
                    ("humidity", "humidity", "%"),
                ]:
                    key_suffix: str = (
                        "T" if measure_type == "temperature" else "H"
                    )
                    config_topic: str = f"homeassistant/sensor/{self.client_id}_{duct_id}_{key_suffix}/config"
                    payload: Dict[str, Any] = {
                        "name": f"{duct_name} {'Temperature' if measure_type == 'temperature' else 'Humidity'}",
                        "unique_id": f"{self.client_id}_{duct_id}_{key_suffix}",
                        "state_topic": f"homeassistant/sensor/{self.client_id}_{duct_id}/state",
                        "availability": [
                            {"topic": self.master_avail_topic},
                            {
                                "topic": f"homeassistant/sensor/{self.client_id}_{duct_id}/availability"
                            },
                        ],
                        "availability_mode": "all",
                        "value_template": f"{{{{ value_json.{measure_type} }}}}",
                        "device_class": d_class,
                        "unit_of_measurement": unit,
                        "state_class": "measurement",
                        "device": device_info,
                    }
                    self.publish(config_topic, payload)
                    time.sleep(0.1)

            # Register System Diagnostics Entities
            sys_sensors = [
                (
                    "pico_temp",
                    "Internal CPU Temp",
                    "temperature",
                    "°C",
                    "measurement",
                ),
                ("rssi", "Signal Strength", "signal_strength", "dBm", "measurement"),
                ("uptime", "Uptime", "duration", "s", None),
                ("version", "Firmware Version", None, None, None),
                ("reconnects", "Reconnect Count", None, None, None),
                ("last_reset", "Last Reset Reason", None, None, None),
                ("status", "System Status", None, None, None),
            ]

            for key, name, d_class, unit, s_class in sys_sensors:
                topic = f"homeassistant/sensor/{self.client_id}_{key}/config"
                payload = {
                    "name": name,
                    "unique_id": f"{self.client_id}_{key}",
                    "state_topic": f"homeassistant/sensor/{self.client_id}_sys/state",
                    "availability_topic": self.master_avail_topic,
                    "value_template": f"{{{{ value_json.{key} }}}}",
                    "entity_category": "diagnostic",
                    "device": device_info,
                }
                if d_class:
                    payload["device_class"] = d_class
                if unit:
                    payload["unit_of_measurement"] = unit
                if s_class:
                    payload["state_class"] = s_class
                self.publish(topic, payload)
                time.sleep(0.1)

            return True
        except (OSError, ValueError):
            return False


# ==========================================
# 3. THE MAIN LOOP & ASYNC TASKS
# ==========================================
async def main() -> None:
    global system_status, last_published_status, force_sensor_publish, watchdog

    # Start the watchdog strictly when the application loop begins
    watchdog = machine.WDT(timeout=watchdog_timeout_ms)

    # Initialise SoftI2C matching working hardware setup
    i2c = machine.SoftI2C(
        scl=machine.Pin(i2c_scl_pin),
        sda=machine.Pin(i2c_sda_pin),
        freq=400000,
    )

    # Initialise hardware pins
    reset_pin: machine.Pin = machine.Pin(mux_reset_pin_num, machine.Pin.OUT)
    relay_pin: machine.Pin = machine.Pin(relay_pin_num, machine.Pin.OUT)
    led_pin: machine.Pin = machine.Pin("LED", machine.Pin.OUT)

    # Reversed Relay Logic: Default to zero (OFF) at startup
    relay_pin.value(0)
    led_pin.value(0)

    # Hardware reset the HW-617 multiplexer at startup
    await reset_multiplexer(reset_pin)

    mux: I2CMultiplexer = I2CMultiplexer(i2c)
    sensor: SHT45 = SHT45(i2c)
    network_controller: NetworkManager = NetworkManager(
        client_id, secrets.mqttBroker, secrets.mqttUser, secrets.mqttPassword
    )

    # MQTT message callback handler
    def sub_cb(topic: bytes, msg: bytes) -> None:
        topic_str: str = topic.decode("utf-8")
        msg_str: str = msg.decode("utf-8")

        if topic_str == network_controller.relay_cmd_topic:
            if msg_str.upper() == "ON":
                relay_pin.value(1)  # Set HIGH for ON
                network_controller.publish(
                    network_controller.relay_state_topic, "ON", retain=True
                )
            elif msg_str.upper() == "OFF":
                relay_pin.value(0)  # Set LOW for OFF
                network_controller.publish(
                    network_controller.relay_state_topic, "OFF", retain=True
                )

        elif topic_str == network_controller.ota_cmd_topic:
            if msg_str.upper() == "PRESS":
                try:
                    # Base system telemetry payload
                    internal_volts = internal_temp_sensor.read_u16() * (3.3 / 65535)
                    pico_temp_c = round(
                        27 - (internal_volts - 0.706) / 0.001721, 1
                    )
                    sys_data: Dict[str, Any] = {
                        "pico_temp": pico_temp_c,
                        "rssi": network.WLAN(network.STA_IF).status("rssi"),
                        "uptime": time.time() - system_start_time_secs,
                        "version": app_version,
                        "reconnects": network_controller.reconnects,
                        "last_reset": last_reset_reason,
                    }

                    # Stage 1: Announce OTA initiation
                    sys_data["status"] = "OTA: Initiating Sequence"
                    network_controller.publish(
                        f"homeassistant/sensor/{client_id}_sys/state", sys_data
                    )
                    
                    # Suspend RP2040/RP2350 software execution timer constraints natively
                    # (Fallback safety mechanism is handled by boot.py missing main trigger)
                    with open("ota_pending.flag", "w") as f:
                        f.write("pending")

                    try:
                        os.remove("main_backup.py")
                    except OSError:
                        pass

                    os.rename("main.py", "main_backup.py")

                    # Stage 2: Announce download start
                    sys_data["status"] = "OTA: Downloading Files..."
                    network_controller.publish(
                        f"homeassistant/sensor/{client_id}_sys/state", sys_data
                    )
                    
                    # Stage 3: Blocking download
                    ugit.pull_all(
                        isconnected=True,
                        ignore=[
                            "/README.md",
                            "/secrets.py",
                            "/secrets_example.py",
                            "/config.json",
                            "/main_backup.py",
                            "/LICENSE",
                        ],
                    )

                    # Stage 3.5: Parse the ugit log for downloaded or updated files
                    try:
                        with open("ugit_log.txt", "r") as log_file:
                            log_lines: List[str] = log_file.readlines()
                            
                        updated_files: List[str] = []
                        for line in log_lines:
                            clean_line: str = line.strip()
                            if "updated" in clean_line or "downloaded" in clean_line:
                                # Standard ugit log format: "/main.py updated"
                                parts: List[str] = clean_line.split(" ")
                                if parts:
                                    # Strip leading slash for cleaner display in Home Assistant
                                    file_name: str = parts[0].lstrip("/")
                                    if file_name not in updated_files:
                                        updated_files.append(file_name)
                                        
                        if updated_files:
                            sys_data["status"] = f"OTA Files: {', '.join(updated_files)}"
                        else:
                            sys_data["status"] = "OTA: No files changed"
                            
                        network_controller.publish(
                            f"homeassistant/sensor/{client_id}_sys/state", sys_data
                        )
                        time.sleep(2)  # Allow MQTT buffer time to flush to HA
                        
                    except OSError:
                        pass
                    
                    # Stage 4: Announce reboot
                    sys_data["status"] = "OTA: Complete. Rebooting"
                    network_controller.publish(
                        f"homeassistant/sensor/{client_id}_sys/state", sys_data
                    )
                    time.sleep(1)  # Allow MQTT buffer to flush
                    machine.reset()

                except Exception as e:
                    # Stage 5: Handle and log mid-flight failures
                    sys_data["status"] = f"OTA Failed: {e}"
                    network_controller.publish(
                        f"homeassistant/sensor/{client_id}_sys/state", sys_data
                    )
                    try:
                        os.rename("main_backup.py", "main.py")
                        os.remove("ota_pending.flag")
                    except OSError:
                        pass
                    time.sleep(2)
                    machine.reset()

    discovery_sent: bool = False
    last_heartbeat_time_secs: float = 0

    while True:
        if watchdog is not None:
            watchdog.feed()
        now_secs = time.time()

        # 1. Maintain Network Connection
        if not await network_controller.maintain_connection():
            discovery_sent = False
            await asyncio.sleep(2)
            continue

        if network_controller.client and not discovery_sent:
            network_controller.client.set_callback(sub_cb)

            # Subscribe to command topics
            network_controller.client.subscribe(
                network_controller.relay_cmd_topic.encode("utf-8")
            )
            network_controller.client.subscribe(
                network_controller.ota_cmd_topic.encode("utf-8")
            )

            if network_controller.send_discovery():
                discovery_sent = True
                current_state: str = (
                    "ON" if relay_pin.value() == 1 else "OFF"
                )
                network_controller.publish(
                    network_controller.relay_state_topic,
                    current_state,
                    retain=True,
                )
                last_heartbeat_time_secs = (
                    now_secs - heartbeat_interval_secs
                )

        network_controller.check_messages()

        # 2. Dynamic System Telemetry Broadcast
        force_heartbeat = (
            now_secs - last_heartbeat_time_secs >= heartbeat_interval_secs
        )
        status_changed = system_status != last_published_status

        if (
            force_heartbeat or status_changed
        ) and network_controller.client is not None:
            internal_volts = internal_temp_sensor.read_u16() * (3.3 / 65535)
            pico_temp_c = round(
                27 - (internal_volts - 0.706) / 0.001721, 1
            )

            sys_data = {
                "pico_temp": pico_temp_c,
                "rssi": network.WLAN(network.STA_IF).status("rssi"),
                "uptime": time.time() - system_start_time_secs,
                "version": app_version,
                "reconnects": network_controller.reconnects,
                "last_reset": last_reset_reason,
                "status": system_status,
            }
            network_controller.publish(
                f"homeassistant/sensor/{client_id}_sys/state", sys_data
            )
            last_published_status = system_status
            if force_heartbeat:
                force_sensor_publish = True
                last_heartbeat_time_secs = now_secs

        # 3. Poll Sensors Across Active Channels
        bus_fault_detected: bool = False
        offline_ducts: List[str] = []

        for channel, info in active_ducts.items():
            if watchdog is not None:
                watchdog.feed()
            duct_id: str = info["id"]
            duct_name: str = info["name"]
            try:
                # Isolate multiplexer bus selection exceptions
                try:
                    mux.select_channel(channel)
                except MultiplexerError:
                    bus_fault_detected = True
                    raise

                # Isolate individual sensor read exceptions
                try:
                    temp, humidity = await sensor.read_temp_humidity()
                except SensorReadError:
                    info["fail_count"] += 1
                    if (
                        info["fail_count"] >= 3
                        and info["last_avail"] != "offline"
                    ):
                        network_controller.publish(
                            f"homeassistant/sensor/{client_id}_{duct_id}/availability",
                            "offline",
                        )
                        info["last_avail"] = "offline"

                    if info["fail_count"] >= 3:
                        offline_ducts.append(duct_name)
                    continue

                await flash_led(led_pin)
                info["fail_count"] = 0

                # --- Median Filtering (History of Three Samples) ---
                info["history_t"].append(temp)
                if len(info["history_t"]) > 3:
                    info["history_t"].pop(0)
                sorted_t = sorted(info["history_t"])
                filtered_t = (
                    sorted_t[1] if len(sorted_t) == 3 else sorted_t[-1]
                )

                info["history_h"].append(humidity)
                if len(info["history_h"]) > 3:
                    info["history_h"].pop(0)
                sorted_h = sorted(info["history_h"])
                filtered_h = (
                    sorted_h[1] if len(sorted_h) == 3 else sorted_h[-1]
                )

                current_avail = "online"

                if (
                    force_sensor_publish
                    or current_avail != info["last_avail"]
                ):
                    network_controller.publish(
                        f"homeassistant/sensor/{client_id}_{duct_id}/availability",
                        current_avail,
                    )
                    info["last_avail"] = current_avail

                # --- Delta Threshold Evaluation ---
                if current_avail == "online":
                    t_changed = (
                        abs(filtered_t - info["last_temp"])
                        >= sensor_change_threshold_t
                    )
                    h_changed = (
                        abs(filtered_h - info["last_hum"])
                        >= sensor_change_threshold_h
                    )

                    if force_sensor_publish or t_changed or h_changed:
                        payload: Dict[str, float] = {
                            "temperature": filtered_t,
                            "humidity": filtered_h,
                        }
                        topic: str = (
                            f"homeassistant/sensor/{client_id}_{duct_id}/state"
                        )
                        network_controller.publish(topic, payload)
                        info["last_temp"] = filtered_t
                        info["last_hum"] = filtered_h

            except MultiplexerError:
                pass
            except (SensorReadError, OSError):
                pass

            # Track offline status for system status evaluation
            if info["fail_count"] >= 3 or info["last_avail"] == "offline":
                if duct_name not in offline_ducts:
                    offline_ducts.append(duct_name)

            await asyncio.sleep(0.1)

        # Update system status
        if bus_fault_detected:
            system_status = "I2C Multiplexer Error"
        elif offline_ducts:
            if len(offline_ducts) == 1:
                system_status = f"{offline_ducts[0]} Sensor Offline"
            else:
                system_status = (
                    f"Multiple Sensors Offline ({', '.join(offline_ducts)})"
                )
        else:
            system_status = "Healthy"

        force_sensor_publish = False

        # Wait two seconds between polling loops
        await asyncio.sleep(2)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
