# config.py Local configuration for mqtt_as demo programs.
from secrets import mqtt_user, mqtt_password, mqtt_server, wifi_sid, wifi_pw
from mqtt_as.config import config as mqtt_config

mqtt_config["server"] = mqtt_server
mqtt_config["user"] = mqtt_user
mqtt_config["password"] = mqtt_password

# Wifi
mqtt_config["ssid"] = wifi_sid
mqtt_config["wifi_pw"] = wifi_pw
