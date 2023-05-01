"""Contains the irrigation system program"""
# pylint: disable=broad-exception-raised,relative-beyond-top-level
# pyright: reportMissingImports=false

import json
import network # pylint: disable=import-error
from utime import ticks_diff, ticks_ms # pylint: disable=import-error
from machine import Pin, Signal, deepsleep # pylint: disable=import-error

import uasyncio as asyncio # pylint: disable=import-error
from .mqtt_client import MQTTClientWithTimeOut as MQTTClient
from .battery_monitor import BatteryMonitor


class Irrigation:
    """Main class of irrigation system"""

    def __init__(
        self,
        plants,
        mqtt_config,
        pump_pin=26,
        debug=False,
        main_topic="irrigation_system",
        water_level_pin=None,
        battery_monitor:BatteryMonitor=None,
        developer_mode=False,
    ):
        """Setup"""
        print("Setting up irrigation system")
        self.running = False
        self.debug = debug

        # Set up MQTT connection
        mqtt_config["subs_cb"] = self.mqtt_message_received
        mqtt_config["connect_coro"] = self.mqtt_connect
        self.mqtt_main_topic = main_topic
        self.mqtt_watering_switch_topic = f"{main_topic}/watering_switch"
        self.mqtt_energy_saver_topic = f"{main_topic}/energy_saver_switch"
        self.mqtt_status_topic = main_topic + "/connectivity_status"
        print(f"Setting up mqtt connection with configuration: {mqtt_config}")
        self.mqtt_client = MQTTClient(mqtt_config)
        self.mqtt_client.DEBUG = False
        self.mqtt_connectivity_timeout = 200

        # Set up plants
        self.plants = plants
        for plant in self.plants:
            plant.define_mqtt_client(self.mqtt_client)

        # Get occupied pins:
        plant_pins = []
        for plant in self.plants:
            plant_pins.append(plant.sensor_pin_no)
            plant_pins.append(plant.valve_pin_no)

        # Set up pump
        if pump_pin == 12:
            raise Exception(
                "Please do not connect the pump pin to 12, this will cause reboot problems"
            )
        self.pump_pin = Signal(Pin(pump_pin, Pin.OUT), invert=True)

        # Set up water level pin
        self.water_level_pin = water_level_pin
        self.water_empty_topic = None
        self.water_empty_topic_availability = None
        if self.water_level_pin is not None:
            if self.water_level_pin in plant_pins:
                raise Exception(
                    f"Pin {self.water_level_pin} is already occupied and cannot be used for checking the water level"
                )

            # Set pull up:
            self.water_level_pin = Pin(self.water_level_pin, Pin.IN, Pin.PULL_UP)

            # Set MQTT topic
            self.water_empty_topic = f"{main_topic}/water_empty"
            self.water_empty_topic_availability = (
                f"{self.water_empty_topic}/availability"
            )

        # Set up energy saving mode
        self.energy_saving_mode = False

        # Set up developer mode
        self.developer_mode = developer_mode

        # Set up loop time
        self.loop_time_normal_ms = 1000.0 * 10
        self.loop_time_energy_saving_ms = 1000.0 * 300
        self.loop_time_developer_mode = 1000.0 * 1
        if self.developer_mode:
            self.loop_time_ms = self.loop_time_developer_mode
        else:
            self.loop_time_ms = self.loop_time_normal_ms

        # Event loop:
        self.event_loop = asyncio.get_event_loop()

        # Watering status
        self.watering = False

        # Watering task
        self.watering_task = None

        # Battery monitor
        self.battery_monitor = battery_monitor
        if self.battery_monitor is not None:
            self.battery_monitor.define_mqtt_client(self.mqtt_client,main_topic)

        # Time-out definition
        self.time_out_time = 5

    def printd(self, msg):
        if self.debug:
            print(msg)

    async def mqtt_connect(self, client):
        """Handles establishing an mqtt connection"""
        print(
            f"Connected to {client.server}"
        )
        # Subscribe to threshold and watering time updates
        for plant in self.plants:
            await self.mqtt_client.subscribe_with_timeout(plant.threshold_topic)
            await self.mqtt_client.subscribe_with_timeout(plant.time_topic)

        # Subscribe to watering switch
        await self.mqtt_client.subscribe_with_timeout(self.mqtt_watering_switch_topic)

        # Subscribe to energy saving mode switch
        await self.mqtt_client.subscribe_with_timeout(self.mqtt_energy_saver_topic)

    def mqtt_message_received(self, topic, payload, _):
        """Handles received mqtt messages"""
        # Convert from bytes to strings:
        topic = topic.decode()
        payload = payload.decode()

        # Print message
        print(f"Message {payload} received on {topic}")

        # Handle watering switch
        if topic == self.mqtt_watering_switch_topic:
            if payload == "ON" and not self.watering:
                self.watering_task = self.event_loop.create_task(self.water())
                return
            elif payload == "OFF":
                self.cancel_watering()
                return
            else:
                print(
                    f"Got an unexpected payload: {payload} on topic {topic}, expected 'ON' or 'OFF'"
                )

        # Handle settings update of plants
        for plant in self.plants:
            if plant.check_message(topic, payload):
                return

        # Handle energy saving mode update:
        if topic == self.mqtt_energy_saver_topic:
            if self.developer_mode:
                print("Cannot set energy saving mode in developer mode as this would lead to connectivity problems")
                if self.energy_saving_mode: # Only publish when the state changes
                    self.event_loop.create_task(self.mqtt_client.publish_with_timeout(self.mqtt_energy_saver_topic, "OFF", retain=True))
                self.loop_time_ms = self.loop_time_developer_mode
                self.energy_saving_mode = False
            elif payload == "ON":
                if not self.energy_saving_mode: # Only publish when the state changes
                    self.event_loop.create_task(self.mqtt_client.publish_with_timeout(self.mqtt_energy_saver_topic, payload, retain=True))
                self.energy_saving_mode = True
                print("Switching energy saving mode on")
                self.loop_time_ms = self.loop_time_energy_saving_ms
            elif payload == "OFF":
                if self.energy_saving_mode: # Only publish when the state changes:
                    self.event_loop.create_task(self.mqtt_client.publish_with_timeout(self.mqtt_energy_saver_topic, payload, retain=True))
                self.energy_saving_mode = False
                print("Switching energy saving mode off")
                self.loop_time_ms = self.loop_time_normal_ms
            else:
                print(
                    f"Got an unexpected payload: {payload} on topic {topic}, expected 'ON' or 'OFF'"
                )

            print(f"Setting loop time to {self.loop_time_ms / 1000 / 60:0.1f} minutes")
            return

        # When we arrive here the payload on this topic was not processed:
        print(f"Warning, not able to process topic {topic} with payload {payload}")

    async def run_mqtt(self):
        """Runs mqtt"""
        print("Starting mqtt client")
        await self.mqtt_client.connect(quick=True)
        print("Done")

    async def run_sensor_reading_loop(self):
        """Runs the sensor reading loop"""
        print("Starting sensor reading loop")
        while self.running:
            # Get starting time
            start = ticks_ms()

            # Wait until connected
            await self.mqtt_client.wait_until_connected()
            await self.sensor_reading_loop()

            # Calculate sleep time
            elapsed_time_ms = ticks_diff(ticks_ms(), start)
            sleep_time_ms = self.loop_time_ms - elapsed_time_ms
            if sleep_time_ms < 0.0:
                print(
                    f"Loop is too slow to still be able to communicate, got elapsed time {elapsed_time_ms:.1f} [ms], "
                    + f"increase loop time to at least {elapsed_time_ms:.1f} [ms] to be able to still communicate"
                )
                sleep_time_ms = self.loop_time_ms

            # Go to deep-sleep in the energy saving mode
            if self.energy_saving_mode and not self.developer_mode and not self.watering:
                print(f"Deep sleeping for {sleep_time_ms} [ms]")
                await self.mqtt_client.disconnect()
                deepsleep(int(self.loop_time_ms))
            else:
                print(f"Sleeping for {sleep_time_ms} [ms]")
                await asyncio.sleep_ms(int(self.loop_time_ms))

    async def sensor_reading_loop(self):
        """Runs a sensor reading loop"""
        # Read all plants
        for n, plant in enumerate(self.plants):
            self.printd(f"Reading plant #{n}")
            try:
                await plant.read()
            except Exception as e:
                print(f"Failed to read plant {n} because of {e}")

        # Read water level
        try:
            if self.water_level_pin is not None:
                self.check_water_level()
        except Exception as e:
            print(f"Failed to check water level because of {e}")

        # Read battery level
        if self.battery_monitor is not None:
            try:
                await self.battery_monitor.read()
            except Exception as e:
                print(f"Failed to read battery level because of {e}")

        # Read connectivity
        try:
            payload_json = {
                "ip": network.WLAN().ifconfig()[0],
                "rssi": network.WLAN().status("rssi"),
            }
            self.event_loop.create_task(
                self.mqtt_client.publish_with_timeout(
                    self.mqtt_status_topic, json.dumps(payload_json), retain=True
                )
            )
        except Exception as e:
            print(f"Failed to read connectivity because of {e}")

    def run(self):
        """Runs the motion loop and mqtt client"""
        self.running = True

        # Create task for the mqtt client:
        print("Creating task for the mqtt client")
        self.event_loop.create_task(self.run_mqtt())
        print("Done")

        # Create task for the sensor reading loop
        print("Creating task for the plant reading loop")
        self.event_loop.create_task(self.run_sensor_reading_loop())
        print("Done")

        # Publish off state:
        self.cancel_watering()

        # Start the event loop:
        print("Starting the event loop")
        self.event_loop.run_forever()

    async def water(self):
        """Check if any plants are dry and water those"""
        # check water level
        if self.water_level_pin is not None:
            if not self.check_water_level():
                print("Water is empty, aborting watering sequence")
                self.finish_watering()
                return
            else:
                print("Water level OK")

        self.watering = True
        print("Checking if any plants are dry")
        for n, plant in enumerate(self.plants):
            print(f"Checking plant #{n}")
            if self.watering:
                is_dry, valid_reading = await plant.check_if_dry()
                if is_dry and valid_reading:
                    break
        if is_dry and valid_reading:
            print("At least one plant is dry, starting watering")
        else:
            print("No dry plants were detected, aborting")
            self.watering = False
            # Publish watering state off
            self.event_loop.create_task(
                self.mqtt_client.publish_with_timeout(self.mqtt_watering_switch_topic, "OFF", retain=True)
            )
            self.event_loop.create_task(
                self.mqtt_client.publish_with_timeout(self.mqtt_watering_switch_topic, "OFF", retain=True)
            )
            return

        print("Switching on pump")
        self.pump_pin.on()
        await asyncio.sleep(5)
        print("Pump switched on")

        print("Starting watering sequence")
        for n, plant in enumerate(self.plants):
            print(f"Watering plant #{n}")
            if self.watering:
                await plant.water()

        # Switch off again
        print("Finished watering sequence")
        self.finish_watering()

    def check_water_level(self):
        """Check if there is water in the reservoir"""
        # Set sensor online
        self.event_loop.create_task(
            self.mqtt_client.publish_with_timeout(
                self.water_empty_topic_availability, "online", retain=True
            )
        )
        if self.water_level_pin.value() == 0:
            print("Publish water empty")
            self.event_loop.create_task(
                self.mqtt_client.publish_with_timeout(self.water_empty_topic, "ON", retain=True)
            )
            return False
        else:
            print("Publish water full")
            self.event_loop.create_task(
                self.mqtt_client.publish_with_timeout(self.water_empty_topic, "OFF", retain=True)
            )
            return True

    def finish_watering(self):
        """Finished the watering sequence"""
        print("Finishing watering sequence")

        # Switch off pump
        print("Switching off pump")
        self.pump_pin.off()

        # Publish watering state off
        print("Publish watering status off")
        if self.watering:
            self.event_loop.create_task(
                self.mqtt_client.publish_with_timeout(self.mqtt_watering_switch_topic, "OFF", retain=True)
            )

        # Set internal state to off
        self.watering = False

    def cancel_watering(self):
        """Cancel a running watering sequence"""
        # Cancel running tasks:
        if self.watering_task is not None:
            self.watering_task.cancel()

        # Stop watering all plants:
        for plant in self.plants:
            plant.pin_valve.off()

        # Normal finish:
        self.finish_watering()

    def exit_gracefully(self):
        """Exits gracefully"""
        print("Stopping gracefully")

        print("Disconnecting from mqtt")
        self.mqtt_client.disconnect()
        print("Done")

        print("Finish stopping gracefully")
