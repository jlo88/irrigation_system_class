"""Custom MQTTClient"""
# pyright: reportMissingImports=false

import uasyncio as asyncio # pylint: disable=import-error
from machine import reset # pylint: disable=import-error

from .micropython_mqtt.mqtt_as.mqtt_as import MQTTClient

class MQTTClientWithTimeOut(MQTTClient):
    def __init__(self,*args,**kwargs):
        super().__init__(*args,**kwargs)

        self.time_out_time = 5
        self.mqtt_connectivity_timeout = 10

    async def subscribe_with_timeout(self,topic):
        """Subscribes to a topic and defers to reconnect on a time-out"""
        try:
            await asyncio.wait_for(self.subscribe(topic, 1),self.time_out_time)
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
            await asyncio.wait_for(self.publish(topic, msg, retain=retain),self.time_out_time)
        except asyncio.TimeoutError:
            print(f"Failed to publish {msg} to {topic} because time-out of {self.time_out_time} has expired")
            await self.wait_until_connected()
        except Exception as e:
            print(f"Failed to publish to {topic} because of exception {e}")

    async def wait_until_connected(self):
        """Waits until connected, if the time-out expires, reboot the device"""
        print("Checking MQTT connection status")
        cnt = 0
        while not self.isconnected():
            print("Waiting for MQTT connection, waiting 1s")
            cnt += 1
            if cnt < self.mqtt_connectivity_timeout:
                await asyncio.sleep_ms(1000)
            else:
                print(f"Rebooting because MQTT timeout of {self.mqtt_connectivity_timeout} was exceeded")
                reset()
        print("MQTT connected!")
