"""Contains the irrigation system program"""
# pylint: disable=broad-exception-raised,relative-beyond-top-level
import json
import network
from utime import ticks_diff, ticks_ms
from machine import Pin, Signal, deepsleep, reset

import uasyncio as asyncio
from .micropython_mqtt.mqtt_as.mqtt_as import MQTTClient
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
        self.mqtt_switch_topic = f"{main_topic}/switch"
        self.mqtt_energy_saver_topic = f"{main_topic}/energy_saver_switch"
        self.mqtt_energy_saver_command_topic = f"{self.mqtt_energy_saver_topic}/set"
        self.mqtt_energy_saver_available_topic = f"{self.mqtt_energy_saver_topic}/available"
        self.mqtt_switch_command_topic = self.mqtt_switch_topic + "/set"
        self.mqtt_switch_available_topic = self.mqtt_switch_topic + "/available"
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
        self.water_level_topic = None
        self.water_level_topic_availability = None
        if self.water_level_pin is not None:
            if self.water_level_pin in plant_pins:
                raise Exception(
                    f"Pin {self.water_level_pin} is already occupied and cannot be used for checking the water level"
                )

            # Set pull up:
            self.water_level_pin = Pin(self.water_level_pin, Pin.IN, Pin.PULL_UP)

            # Set MQTT topic
            self.water_level_topic = f"{main_topic}/water_empty"
            self.water_level_topic_availability = (
                f"{self.water_level_topic}/availability"
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

    async def subscribe_with_timeout(self,topic):
        """Subscribes to a topic and defers to reconnect on a time-out"""
        try:
            await asyncio.wait_for(self.mqtt_client.subscribe(topic, 1),self.time_out_time)
            print(f"Subscribed to {topic}")
        except asyncio.TimeoutError:
            print(f"Failed to subscribe to {topic} because time-out of {self.time_out_time} has expired")
            await self.wait_until_connected()
        except Exception as e:
            print(f"Failed to subscribe to {topic} because of exception {e}")

    async def publish_with_timeout(self,topic,msg,retain=True):
        """PUblishes a message to a topic with a time-out and defers to reconnect on a time-out"""
        try:
            print(f"Publishing {msg} on {topic}")
            await asyncio.wait_for(self.mqtt_client.publish(topic, msg, retain=retain),self.time_out_time)
        except asyncio.TimeoutError:
            print(f"Failed to publish {msg} to {topic} because time-out of {self.time_out_time} has expired")
            await self.wait_until_connected()
        except Exception as e:
            print(f"Failed to publish to {topic} because of exception {e}")

    async def wait_until_connected(self):
        """Waits until connected, if the time-out expires, reboot the device"""
        print("Checking MQTT connection status")
        cnt = 0
        while not self.mqtt_client.isconnected():
            print("Waiting for MQTT connection, waiting 1s")
            cnt += 1
            if cnt < self.mqtt_connectivity_timeout:
                await asyncio.sleep_ms(1000)
            else:
                print(f"Rebooting because MQTT timeout of {self.mqtt_connectivity_timeout} was exceeded")
                reset()
        print("MQTT connected!")

    async def mqtt_connect(self, client):
        """Handles establishing an mqtt connection"""
        print(
            f"Connected to {client.server}"
        )
        # Subscribe to threshold and watering time updates
        for plant in self.plants:
            await self.subscribe_with_timeout(plant.threshold_topic)
            await self.subscribe_with_timeout(plant.time_topic)

        # Subscribe to watering switch
        await self.publish_with_timeout(self.mqtt_switch_available_topic, "online", retain=True)
        await self.subscribe_with_timeout(self.mqtt_switch_command_topic)

        # Subscribe to energy saving mode switch
        await self.publish_with_timeout(self.mqtt_energy_saver_available_topic, "online", retain=True)
        await self.subscribe_with_timeout(self.mqtt_energy_saver_command_topic)

    def mqtt_message_received(self, topic, payload, _):
        """Handles received mqtt messages"""
        # Convert from bytes to strings:
        topic = topic.decode()
        payload = payload.decode()

        # Print message
        self.printd(f"Message {payload} received on {topic}")

        # Handle watering switch
        if topic == self.mqtt_switch_command_topic:
            if payload == "ON" or payload == "OFF":
                # Publishes back the state, needs to be asynchronous as well:
                self.event_loop.create_task(
                    self.publish_with_timeout(
                        self.mqtt_switch_topic, payload, retain=True
                    )
                )

            if payload == "ON":
                self.watering_task = self.event_loop.create_task(self.water())
                return
            elif payload == "OFF":
                self.cancel_watering(publish_set_off=False)
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
        if topic == self.mqtt_energy_saver_command_topic:
            if self.developer_mode:
                print("Cannot set energy saving mode in developer mode as this would lead to connectivity problems")
                self.event_loop.create_task(self.publish_with_timeout(self.mqtt_energy_saver_topic, "OFF", retain=True))
                self.loop_time_ms = self.loop_time_developer_mode
            elif payload == "ON":
                self.event_loop.create_task(self.publish_with_timeout(self.mqtt_energy_saver_topic, payload, retain=True))
                self.energy_saving_mode = True
                print("Switching energy saving mode on")
                self.loop_time_ms = self.loop_time_energy_saving_ms
            elif payload == "OFF":
                self.event_loop.create_task(self.publish_with_timeout(self.mqtt_energy_saver_topic, payload, retain=True))
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
            await self.wait_until_connected()
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
                await plant.read(self.publish_with_timeout)
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
                await self.battery_monitor.read(self.publish_with_timeout)
            except Exception as e:
                print(f"Failed to read battery level because of {e}")

        # Read connectivity
        try:
            payload_json = {
                "ip": network.WLAN().ifconfig()[0],
                "rssi": network.WLAN().status("rssi"),
            }
            self.event_loop.create_task(
                self.publish_with_timeout(
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
        self.cancel_watering(publish_set_off=False)

        # Start the event loop:
        print("Starting the event loop")
        self.event_loop.run_forever()

    async def water(self):
        """Check if any plants are dry and water those"""
        # check water level
        if self.water_level_pin is not None:
            if not self.check_water_level():
                print("Water is empty, aborting watering sequence")
                self.finish_watering(publish_set_off=True)
                return
            else:
                print("Water level OK")

        self.watering = True
        print("Checking if any plants are dry")
        for n, plant in enumerate(self.plants):
            print(f"Checking plant #{n}")
            if self.watering:
                is_dry = await plant.check_if_dry(self.publish_with_timeout)
                if is_dry:
                    break
        if is_dry:
            print("At least one plant is dry, starting watering")
        else:
            print("No dry plants were detected, aborting")
            self.watering = False
            # Publish watering state off
            self.event_loop.create_task(
                self.publish_with_timeout(self.mqtt_switch_topic, "OFF", retain=True)
            )
            self.event_loop.create_task(
                self.publish_with_timeout(self.mqtt_switch_command_topic, "OFF", retain=True)
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
                await plant.water(self.publish_with_timeout)

        # Switch off again
        print("Finished watering sequence")
        self.finish_watering(publish_set_off=True)

    def check_water_level(self):
        """Check if there is water in the reservoir"""
        # Set sensor online
        self.event_loop.create_task(
            self.publish_with_timeout(
                self.water_level_topic_availability, "online", retain=True
            )
        )
        if self.water_level_pin.value() == 0:
            print("Publish water empty")
            self.event_loop.create_task(
                self.publish_with_timeout(self.water_level_topic, "OFF", retain=True)
            )
            return False
        else:
            print("Publish water full")
            self.event_loop.create_task(
                self.publish_with_timeout(self.water_level_topic, "ON", retain=True)
            )
            return True

    def finish_watering(self, publish_set_off=False):
        """Finished the watering sequence"""
        print("Finishing watering sequence")
        self.watering = False

        # Switch off pump
        print("Switching off pump")
        self.pump_pin.off()

        # Publish watering state off
        print("Publish watering status off")
        self.event_loop.create_task(
            self.publish_with_timeout(self.mqtt_switch_topic, "OFF", retain=True)
        )
        if publish_set_off:
            self.event_loop.create_task(
                self.publish_with_timeout(self.mqtt_switch_command_topic, "OFF", retain=True)
            )
        return

    def cancel_watering(self,publish_set_off=True):
        """Cancel a running watering sequence"""
        # Cancel running tasks:
        if self.watering_task is not None:
            self.watering_task.cancel()

        # Stop watering all plants:
        for plant in self.plants:
            plant.pin_valve.off()

        # Normal finish:
        self.finish_watering(publish_set_off=publish_set_off)

    def exit_gracefully(self):
        """Exits gracefully"""
        print("Stopping gracefully")

        print("Disconnecting from mqtt")
        print(f"Publishing 'offline' to {self.mqtt_switch_available_topic}")
        self.event_loop.run_until_complete(
            self.publish_with_timeout(
                topic=self.mqtt_switch_available_topic, msg="offline", retain=True
            )
        )
        if self.water_level_topic_availability is not None:
            print(f"Publishing 'offline' to {self.water_level_topic_availability}")
            self.event_loop.run_until_complete(
                self.publish_with_timeout(
                    self.water_level_topic_availability, msg="offline", retain=True
                )
            )
        print(f"Publishing 'offline' to {self.mqtt_energy_saver_available_topic}")
        self.event_loop.run_until_complete(
            self.publish_with_timeout(
                self.mqtt_energy_saver_available_topic, msg="offline", retain=True
            )
        )
        self.mqtt_client.disconnect()
        print("Done")

        print("Finish stopping gracefully")
