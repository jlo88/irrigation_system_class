"""Contains the battery monitor class"""
import json
from machine import ADC, Pin
from .micropython_mqtt.mqtt_as.mqtt_as import MQTTClient
from .adc_cal.adc1_cal import ADC1Cal


# pylint: disable=broad-exception-raised

class BatteryMonitor:
    """Battery monitoring class"""
    def __init__(self,
                 sensor_pin:int,
                 resistor_high:float=91e3,
                 resistor_low:float=15e3,
                 min_voltage:float=7,
                 n_readings:int=100,
                 ):

        # Calibration values
        self.min_voltage = min_voltage
        self.resistor_high = resistor_high
        self.resistor_low = resistor_low

        # MQTT
        self.mqtt_client = None
        self.state_topic = None

        # Pin allocation
        allowed_adc_pins = list(range(32, 40))
        if sensor_pin not in allowed_adc_pins:
            raise Exception(
                f"Battery sensor pin can only be attached to {allowed_adc_pins} but trying "
                + f"to attach to {sensor_pin}"
            )
        self.sensor_pin = ADC1Cal(Pin(sensor_pin, Pin.IN),1,None,n_readings,"ADC1 Calibrated")
        self.sensor_pin.atten(ADC.ATTN_11DB)

        # Multiplier setup
        self.multiplier = (self.resistor_high + self.resistor_low) / self.resistor_low
        print(f"Set up battery voltage reading at pin {self.sensor_pin} "
                +f"with multiplier {1/self.multiplier:0.1f} [V/V] "
        )

        # Readings
        self.voltage_reading = None

    def define_mqtt_client(self, mqtt_client: MQTTClient, parent_topic:str):
        self.mqtt_client = mqtt_client
        self.state_topic = parent_topic + "/battery_level"


    async def read(self,publish_handle):
        """Reads the sensor"""
        print("Reading battery level")

        # Read ADC
        voltage_reading_adc = self.sensor_pin.voltage / 1000.0
        print(f"Calculated {voltage_reading_adc:.2f} [V] at the ADC")
        self.voltage_reading = (
            self.multiplier * voltage_reading_adc
        )
        print(f"Calculated {self.voltage_reading} [V] battery voltage")

        # Range check
        if self.voltage_reading < self.min_voltage:
            valid_reading = "OFF"
        else:
            valid_reading = "ON"

        # Publish results
        payload_json = {
            "value": self.voltage_reading,
            "valid_reading": valid_reading,
        }
        await publish_handle(
            topic=self.state_topic,
            msg=json.dumps(payload_json),
            retain=True,
            )

