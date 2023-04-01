"""Contains the irrigation system program"""
import json
import network
from utime import ticks_diff, ticks_ms
from machine import ADC, Pin, Signal

import uasyncio as asyncio
from mqtt_as.mqtt_as import MQTTClient

# pylint: disable=broad-exception-raised

class Irrigation:
    """Main class of irrigation system"""

    def __init__(self, plants, mqtt_config, pump_pin=26,debug=False,switch_topic="irrigation_system/switch",water_level_pin=None,water_level_topic="irrigation_system/water_empty",):
        """Setup"""
        print("Setting up irrigation system")
        self.running = False
        self.debug = debug

        # Set up MQTT connection
        mqtt_config["subs_cb"] = self.mqtt_message_received
        mqtt_config["connect_coro"] = self.mqtt_connect
        self.mqtt_state_topic = switch_topic
        self.mqtt_command_topic = self.mqtt_state_topic + "/set"
        self.mqtt_available_topic = self.mqtt_state_topic + "/available"
        print(f"Setting up mqtt connection with cofiguration: {mqtt_config}")
        self.mqtt_client = MQTTClient(mqtt_config)
        self.mqtt_client.DEBUG = False

        # Set up plants
        self.plants = plants
        for plant in self.plants:
            plant.define_mqtt_client(self.mqtt_client)

        # Set up pump
        if pump_pin == 12:
            raise Exception('Please do not connect the pump pin to 12, this will cause reboot problems')
        self.pump_pin = Signal(Pin(pump_pin, Pin.OUT), invert=True)

        # Set up water level pin
        self.water_level_pin = water_level_pin
        self.water_level_topic = None
        self.water_level_topic_availability = None
        if self.water_level_pin is not None:
            # Check if not used by plants
            plant_pins = []
            for plant in self.plants:
                plant_pins.append(plant.sensor_pin_no)
                plant_pins.append(plant.valve_pin_no)
            if self.water_level_pin in plant_pins:
                raise Exception(f'Pin {self.water_level_pin} is already occupied and cannot be used for checking the water level')

            # Set pull up:
            self.water_level_pin = Pin(self.water_level_pin, Pin.IN, Pin.PULL_UP)

            # Set MQTT topic
            self.water_level_topic = water_level_topic
            self.water_level_topic_availability = f"{self.water_level_topic}/availability"

        # Set up loop time
        self.loop_time_ms = 1000.0 * 60

        # Event loop:
        self.event_loop = asyncio.get_event_loop()

        # Watering status
        self.watering = False

        # Watering task
        self.watering_task = None

    def printd(self, msg):
        if self.debug:
            print(msg)

    async def mqtt_connect(self, client):
        """Handles establishing an mqtt connection"""
        await client.publish(self.mqtt_available_topic, "online", retain=True)
        await client.subscribe(self.mqtt_command_topic, 1)
        print(
            f"Connected to {client.server}, subscribed to {self.mqtt_command_topic} topic"
        )

        # Subscribe to threshold and watering time updates
        for plant in self.plants:
            await client.subscribe(plant.threshold_topic, 1)
            print(f"Subscribed to {plant.threshold_topic}")
            await client.subscribe(plant.time_topic, 1)
            print(f"Subscribed to {plant.time_topic}")

    def mqtt_message_received(self, topic, payload, _):
        """Handles received mqtt messages"""
        # Convert from bytes to strings:
        topic = topic.decode()
        payload = payload.decode()

        # Print message
        self.printd(f"Message {payload} received on {topic}")

        # Handle watering switch
        if topic == self.mqtt_command_topic:
            if payload == "ON" or payload == "OFF":
                # Publishes back the state, needs to be asynchronous as well:
                self.event_loop.create_task(
                    self.mqtt_client.publish(
                        self.mqtt_state_topic, payload, retain=True
                    )
                )
            if payload == "ON":
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

        # When we arrive here the payload on this topic was not processed:
        print(f"Warning, not able to process topic {topic} with payload {payload}")

    async def run_mqtt(self):
        """Runs mqtt"""
        print("Starting mqtt client")
        await self.mqtt_client.connect()
        print("Done")

    async def run_sensor_reading_loop(self):
        """Runs the sensor reading loop"""
        print("Starting sensor reading loop")
        while self.running:
            # Get starting time
            start = ticks_ms()

            await self.sensor_reading_loop()

            # Sleep to maintain loop time
            elapsed_time_ms = ticks_diff(ticks_ms(), start)
            sleep_time_ms = self.loop_time_ms - elapsed_time_ms
            if sleep_time_ms < 0.0:
                print(
                    f"Loop is too slow to still be able to communicate, got elapsed time {elapsed_time_ms:.1f} [ms], "
                    + f"increase loop time to at least {elapsed_time_ms:.1f} [ms] to be able to still communicate"
                )
                await asyncio.sleep_ms(int(self.loop_time_ms))
            else:
                self.printd(f"Sleeping for {sleep_time_ms} [ms]")
                await asyncio.sleep_ms(int(sleep_time_ms))

    async def sensor_reading_loop(self):
        """Runs a sensor reading loop"""

        # Read all plants
        for n, plant in enumerate(self.plants):
            self.printd(f"Reading plant #{n}")
            await plant.read()

        # Read water level
        if self.water_level_pin is not None:
            self.check_water_level()

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
        self.watering = True
        print("Checking if any plants are dry")
        for n, plant in enumerate(self.plants):
            print(f"Checking plant #{n}")
            if self.watering:
                is_dry = await plant.check_if_dry()
                if is_dry:
                    break
        if is_dry:
            print("At least one plant is dry, starting watering")
        else:
            print("No dry plants were detected, aborting")
            self.watering = False
            # Publish watering state off
            self.event_loop.create_task(
                self.mqtt_client.publish(self.mqtt_state_topic, "OFF", retain=True)
            )
            return

        # check water level
        if self.water_level_pin is not None:
            if not self.check_water_level():
                print("Water is empty, aborting watering sequence")
                self.finish_watering()
                return
            else:
                print("Water level OK")


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
            self.mqtt_client.publish(
                self.water_level_topic_availability, "online", retain=True
            )
        )
        if self.water_level_pin.value() == 0:
            print("Publish water empty")
            self.event_loop.create_task(
            self.mqtt_client.publish(self.water_level_topic, "OFF", retain=True)
            )
            return False
        else:
            print("Publish water full")
            self.event_loop.create_task(
            self.mqtt_client.publish(self.water_level_topic, "ON", retain=True)
            )
            return True

    def finish_watering(self):
        """Finished the watering sequence"""
        print("Finishing watering sequence")
        self.watering = False

        # Switch off pump
        print("Switching off pump")
        self.pump_pin.off()

        # Publish watering state off
        print("Publish watering status off")
        self.event_loop.create_task(
            self.mqtt_client.publish(self.mqtt_state_topic, "OFF", retain=True)
        )
        return

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
        self.event_loop.run_until_complete(
            self.mqtt_client.publish(
                topic=self.mqtt_available_topic, msg="offline", retain=True
            )
        )
        self.event_loop.create_task(
            self.mqtt_client.publish(
                self.water_level_topic_availability, "online", retain=True
            )
        )
        self.mqtt_client.disconnect()
        print("Done")

        print("Finish stopping gracefully")


class Plant:
    """Plant class"""

    def __init__(
        self,
        sensor_pin_no: int,
        valve_pin_no: int,
        name: str,
        state_topic: str,
        threshold_topic: str,
        time_topic: str,
        dry_value=2200,
        wet_value=1100,
        n_readings=100,
    ):
        # Name
        self.name = name[0].upper() + name[1:]

        # Calibration values
        self.dry_value = dry_value
        self.wet_value = wet_value
        self.min_value = 500
        self.max_value = 3800

        # States
        self.moisture = None
        self.reading_bits = None
        self.watering = False

        # Pins
        allowed_adc_pins = list(range(32,40))
        if sensor_pin_no not in allowed_adc_pins:
            raise Exception(f"Sensor pin can only be attached to {allowed_adc_pins} but trying to attach to {sensor_pin_no}")

        self.sensor_pin_no = sensor_pin_no
        self.pin_sensor = ADC(Pin(sensor_pin_no, Pin.IN))
        self.pin_sensor.atten(
            ADC.ATTN_11DB
        )  # Set up attenuation so we have a range of 0V ... 3.3V
        forbidden_pins = [12]
        self.valve_pin_no = valve_pin_no
        if valve_pin_no in forbidden_pins:
            raise Exception(f"Valve pin cannot be attached to {forbidden_pins} but trying to attach to {valve_pin_no}")
        self.pin_valve = Signal(Pin(valve_pin_no, Pin.OUT), invert=True)
        self.pin_valve.off()

        # MQTT
        self.mqtt_client = None
        self.state_topic = state_topic
        self.threshold_topic = threshold_topic
        self.time_topic = time_topic

        # Averaging
        self.n_readings = n_readings

        # Watering time
        self.watering_time_s = None
        self.watering_threshold_pct = None

    async def read(self):
        """Reads the sensor and updates the moisture"""
        print(f"Reading moisture level of {self.name}")

        # Read ADC and average:
        readings_bit = [[]] * self.n_readings
        for n, _ in enumerate(readings_bit):
            readings_bit[n] = self.pin_sensor.read()
        self.reading_bits = sum(readings_bit) / self.n_readings

        # Do a range check
        valid_reading_payload = "ON"
        if self.reading_bits < self.min_value or self.reading_bits > self.max_value:
            valid_reading_payload = "OFF"
            print(f"{self.name} reading is out of range! Got {self.reading_bits} bits but should be between {self.min_value} and {self.max_value}")

        # Convert to percentages
        self.moisture = self.map_value(
            self.reading_bits,
            self.dry_value,
            self.wet_value,
            0.0,
            100.0,
        )
        print(f"{self.name} has a moisture level of {self.moisture:.2f} [%]")

        # Publish state over mqtt:
        payload_json = {
            "moisture": self.moisture,
            "moisture_bits": self.reading_bits,
            "name": self.name,
            "ip": network.WLAN().ifconfig()[0],
            "senor_pin_no": self.sensor_pin_no,
            "valve_pin_no": self.valve_pin_no,
            "valid_reading": valid_reading_payload,
        }
        print(f"Message to send: {json.dumps(payload_json)}")
        await self.mqtt_client.publish(
            topic=self.state_topic,
            msg=json.dumps(payload_json),
        )

        return self.moisture

    async def check_if_dry(self):
        """Checks if a plant is dry"""
        if self.watering_threshold_pct is None:
            print('Cannot read because watering threshold is set to None')
            return

        await self.read()
        return self.moisture < self.watering_threshold_pct

    async def water(self):
        """Performs watering by opening the valve for the specified amount of seconds"""
        is_dry = await self.check_if_dry()
        if not is_dry:
            print(
                f"{self.name} does not have to be watered because moisture level of {self.moisture:.1f}[%] is more than the configured threshold of {self.watering_threshold_pct:.1f}[%]"
            )
            return

        if self.watering_time_s is not None:
            print(f"Watering {self.name} for {self.watering_time_s} seconds")
            self.pin_valve.on()
            self.watering = True
            await asyncio.sleep(self.watering_time_s)
            self.pin_valve.off()
            self.watering = False
            print(f"Finished watering {self.name}")
        else:
            print("Cannot water because watering time is set to None")

    def define_mqtt_client(self,mqtt_client: MQTTClient):
        self.mqtt_client = mqtt_client

    def exit_gracefully(self):
        print(f"Shutting down {self.name}")
        self.pin_valve.off()

    def check_message(self, topic, payload):
        """Checks a payload on a topic"""
        if topic == self.threshold_topic:
            try:
                self.watering_threshold_pct = float(
                    payload
                )  # Remove quotation marks and convert to string
                print(
                    f"Updating watering threshold of {self.name} to {self.watering_threshold_pct} [%]"
                )
                return True
            except Exception as e:  # type: ignore
                print(
                    f"Failed to update watering threshold of {self.name} because of {e}"
                )
                return False

        elif topic == self.time_topic:
            try:
                self.watering_time_s = float(payload)
                print(
                    f"Updating watering time of {self.name} to {self.watering_time_s} [s]"
                )
                return True
            except Exception as e:  # type: ignore
                print(f"Failed to update watering time of {self.name} because of {e}")
                return False
        else:
            return False

    @staticmethod
    def map_value(value, old_min, old_max, new_min, new_max):
        """Maps a value to a different interval"""
        # Convert:
        value_mapped = (
            ((value - old_min) * (new_max - new_min)) / (old_max - old_min)
        ) + new_min

        # Force limits
        if value_mapped > new_max:
            value_mapped = new_max
        if value_mapped < new_min:
            value_mapped = new_min

        return value_mapped
