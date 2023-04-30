"""Contains the plant class"""
import json
from machine import ADC, Pin, Signal

import uasyncio as asyncio
from .micropython_mqtt.mqtt_as.mqtt_as import MQTTClient

# pylint: disable=broad-exception-raised

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
        allowed_adc_pins = list(range(32, 40))
        if sensor_pin_no not in allowed_adc_pins:
            raise Exception(
                f"Sensor pin can only be attached to {allowed_adc_pins} but trying to attach to {sensor_pin_no}"
            )

        self.sensor_pin_no = sensor_pin_no
        self.pin_sensor = ADC(Pin(sensor_pin_no, Pin.IN))
        self.pin_sensor.atten(
            ADC.ATTN_11DB
        )  # Set up attenuation so we have a range of 0.15V ... 2.45V
        forbidden_pins = [12]
        self.valve_pin_no = valve_pin_no
        if valve_pin_no in forbidden_pins:
            raise Exception(
                f"Valve pin cannot be attached to {forbidden_pins} but trying to attach to {valve_pin_no}"
            )
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

    async def read(self,publish_handle):
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
            print(
                f"{self.name} reading is out of range! Got {self.reading_bits} bits but should be between {self.min_value} and {self.max_value}"
            )

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
            "moisture_bits": self.reading_bits,
            "moisture": self.moisture,
            "name": self.name,
            "sensor_pin_no": self.sensor_pin_no,
            "valid_reading": valid_reading_payload,
            "valve_pin_no": self.valve_pin_no,
        }
        await publish_handle(
            topic=self.state_topic,
            msg=json.dumps(payload_json),
            retain=True,
            )
        return self.moisture

    async def check_if_dry(self,publish_handle):
        """Checks if a plant is dry"""
        if self.watering_threshold_pct is None:
            print("Cannot read because watering threshold is set to None")
            return

        await self.read(publish_handle)
        return self.moisture < self.watering_threshold_pct

    async def water(self,publish_handle):
        """Performs watering by opening the valve for the specified amount of seconds"""
        is_dry = await self.check_if_dry(publish_handle)
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

    def define_mqtt_client(self, mqtt_client: MQTTClient):
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
