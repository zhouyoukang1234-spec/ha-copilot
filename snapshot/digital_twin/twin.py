"""Digital-twin of the user's physical smart-home devices via MQTT discovery.

Backs the entities that have no hardware on this VM (Sonoff plugs, Xiaomi/giot/
huca wifi switches, EcoFlow power stations, outdoor power source "de_2dian_yuan",
Philips light strip, fan, speaker buttons, phone battery) so the entire derived
graph (Riemann energy integration -> utility meters -> templates -> powercalc ->
automations/scenes) comes alive and is controllable.

Runs as a daemon: publishes retained discovery + initial state, then echoes
command topics back to state topics. Each Sonoff plug's power sensor follows its
switch state, so toggling a plug drives the live energy pipeline.
"""
import json
import time
import random
import threading
import paho.mqtt.client as mqtt

DISC = "homeassistant"          # discovery prefix
BASE = "twin"                   # state/command base topic
DEV = {"identifiers": ["devin_digital_twin"], "name": "Devin Digital Twin",
       "manufacturer": "Devin", "model": "VM Replica"}

# --- Sonoff plugs: switch <-> power sensor, with an "active wattage" per plug ---
SONOFF = {
    "10022cf570": ("中央2号插头", 36.0),
    "10022cf6a2": ("友康床插头", 18.0),
    "10022cf70f": ("俊伟床插头", 22.0),
    "10022cf71d": ("中央5号插头", 41.0),
    "10022cfc76": ("中央1号插头", 33.0),
    "10022ddc35": ("顶灯1", 60.0),
    "10022de30e": ("中央3号插头", 28.0),
    "10022de63b": ("顶灯2", 60.0),
    "10022dede9": ("加湿器", 25.0),
    "100235142b": ("Sonoff3号", 15.0),
}
# extra switches with no power sensor
PLAIN_SWITCHES = {
    "sonoff_10022dedc7_1": "五号开关",
    "sonoff_1002212786": "Sonoff门口插座",
    "giot_cn_1116357483_v6oodm_on_p_2_1": "二号灯开关",
    "giot_cn_1116363322_v6oodm_on_p_2_1": "一号灯开关",
    "giot_cn_1116363360_v6oodm_on_p_2_1": "三号灯开关",
    "giot_cn_1116373212_v6oodm_on_p_2_1": "床底灯开关",
    "huca_cn_1103518825_dh2_on_p_2_1": "筒灯2开关",
    "huca_cn_1103518825_dh2_on_p_3_1": "筒灯1开关",
    "de_2dian_yuan_ac_enabled": "户外电源AC输出",
    "de_2dian_yuan_usb_enabled": "户外电源USB输出",
    "xiaomi_x08e_8219_switch_status": "小米插座开关",
    "yszn01_ys2102_08c6_dryer": "毛巾架烘干",
    "yszn01_ys2102_08c6_uv": "毛巾架UV杀菌",
    "delta2_1838_ac_enabled": "Delta2 AC输出",
    "delta2_1838_ac_always_on": "Delta2 AC常开",
    "delta2_1838_dc_12v_enabled": "Delta2 DC12V",
    "delta2_1838_usb_enabled": "Delta2 USB",
    "delta2_1838_beeper": "Delta2 蜂鸣器",
    "delta2_1838_x_boost_enabled": "Delta2 X-Boost",
    "delta2_1838_prio_solar_charging": "Delta2 优先太阳能",
    "delta2_1838_backup_reserve_enabled": "Delta2 备electricity",
    "river_2_max_ac_enabled": "River2 AC输出",
    "river_2_max_ac_always_on": "River2 AC常开",
    "river_2_max_dc_12v_enabled": "River2 DC12V",
    "river_2_max_x_boost_enabled": "River2 X-Boost",
    "river_2_max_backup_reserve_enabled": "River2 备electricity",
}
# default-ON switches for realism
DEFAULT_ON = {"sonoff_10022ddc35_1", "sonoff_10022de63b_1", "de_2dian_yuan_ac_enabled"}

# --- plain numeric sensors (object_id -> (name, unit, device_class, value)) ---
SENSORS = {
    "de_2dian_yuan_battery_level": ("户外电源电量", "%", "battery", 86),
    "de_2dian_yuan_total_in_power": ("户外电源总输入功率", "W", "power", 62),
    "de_2dian_yuan_ac_out_power": ("户外电源AC输出功率", "W", "power", 120),
    "de_2dian_yuan_ac_in_power": ("户外电源AC输入功率", "W", "power", 65),
    "de_2dian_yuan_type_c_1_out_power": ("户外电源TypeC1功率", "W", "power", 18),
    "de_2dian_yuan_type_c_2_out_power": ("户外电源TypeC2功率", "W", "power", 0),
    "de_2dian_yuan_usb_1_out_power": ("户外电源USB1功率", "W", "power", 5),
    "de_2dian_yuan_usb_2_out_power": ("户外电源USB2功率", "W", "power", 0),
    "phone_battery_level": ("Phone Battery Level", "%", "battery", 73),
    # Xiaomi 温湿度传感器 (miaomiaoce T9) — drives the chips + 设备电量 cards
    "miaomiaoce_t9_0582_temperature": ("卧室温度", "\u00b0C", "temperature", 24.5),
    "miaomiaoce_t9_0582_relative_humidity": ("卧室湿度", "%", "humidity", 52),
    "miaomiaoce_t9_0582_battery_level": ("温湿度计电量", "%", "battery", 88),
    # 其余设备电量
    "su7_battery_level": ("SU7电量", "%", "battery", 64),
    "quest_battery_level": ("Quest电量", "%", "battery", 47),
    "ne2210_battery_level": ("NE2210电量", "%", "battery", 91),
    "unknown_battery_level": ("未知设备电量", "%", "battery", 100),
}

client = mqtt.Client(client_id="devin-twin", protocol=mqtt.MQTTv311)

state = {}  # object_id -> current state string


def pub(topic, payload, retain=True):
    client.publish(topic, payload, qos=1, retain=retain)


def disc_switch(obj, name):
    cfg = {"name": name, "object_id": obj, "unique_id": "twin_" + obj,
           "command_topic": f"{BASE}/{obj}/set", "state_topic": f"{BASE}/{obj}/state",
           "payload_on": "ON", "payload_off": "OFF", "device": DEV}
    pub(f"{DISC}/switch/{obj}/config", json.dumps(cfg))


def disc_sensor(obj, name, unit, dclass):
    cfg = {"name": name, "object_id": obj, "unique_id": "twin_" + obj,
           "state_topic": f"{BASE}/{obj}/state", "unit_of_measurement": unit,
           "state_class": "measurement", "device": DEV}
    if dclass:
        cfg["device_class"] = dclass
    pub(f"{DISC}/sensor/{obj}/config", json.dumps(cfg))


def disc_fan(obj, name):
    cfg = {"name": name, "object_id": obj, "unique_id": "twin_" + obj,
           "command_topic": f"{BASE}/{obj}/set", "state_topic": f"{BASE}/{obj}/state",
           "payload_on": "ON", "payload_off": "OFF",
           "percentage_command_topic": f"{BASE}/{obj}/pct/set",
           "percentage_state_topic": f"{BASE}/{obj}/pct", "device": DEV}
    pub(f"{DISC}/fan/{obj}/config", json.dumps(cfg))


def disc_light(obj, name):
    cfg = {"name": name, "object_id": obj, "unique_id": "twin_" + obj,
           "command_topic": f"{BASE}/{obj}/set", "state_topic": f"{BASE}/{obj}/state",
           "brightness_command_topic": f"{BASE}/{obj}/bri/set",
           "brightness_state_topic": f"{BASE}/{obj}/bri",
           "payload_on": "ON", "payload_off": "OFF", "device": DEV}
    pub(f"{DISC}/light/{obj}/config", json.dumps(cfg))


def disc_button(obj, name):
    cfg = {"name": name, "object_id": obj, "unique_id": "twin_" + obj,
           "command_topic": f"{BASE}/{obj}/press", "device": DEV}
    pub(f"{DISC}/button/{obj}/config", json.dumps(cfg))


def sonoff_power_obj(plug_id):
    return f"sonoff_{plug_id}_power"


def set_state(obj, val):
    state[obj] = val
    pub(f"{BASE}/{obj}/state", val)


def on_connect(c, u, flags, rc, *a):
    print("twin connected rc", rc)
    # discovery for sonoff switches + their power sensors
    for pid, (label, watts) in SONOFF.items():
        sw = f"sonoff_{pid}_1"
        disc_switch(sw, f"Sonoff {label}")
        disc_sensor(sonoff_power_obj(pid), f"{label}功率", "W", "power")
    for obj, name in PLAIN_SWITCHES.items():
        disc_switch(obj, name)
    for obj, (name, unit, dclass, val) in SENSORS.items():
        disc_sensor(obj, name, unit, dclass)
    disc_fan("dmaker_p221_5b47_fan", "米家风扇")
    disc_light("philips_strip3_12ad_light", "飞利浦灯带")
    disc_light("philips_cn_531616941_strip3_s_2", "飞利浦灯带2")
    disc_button("xiaomi_cn_795748340_lx06_play_music_a_5_2", "小爱播放音乐")
    disc_button("xiaomi_cn_795748340_lx06_wake_up_a_5_3", "唤醒小爱")
    time.sleep(1.5)
    # subscribe to all command topics
    c.subscribe(f"{BASE}/+/set")
    c.subscribe(f"{BASE}/+/pct/set")
    c.subscribe(f"{BASE}/+/bri/set")
    c.subscribe(f"{BASE}/+/press")
    # initial states
    for pid, (label, watts) in SONOFF.items():
        sw = f"sonoff_{pid}_1"
        on = sw in DEFAULT_ON
        set_state(sw, "ON" if on else "OFF")
        set_state(sonoff_power_obj(pid), str(watts if on else 0.4))
    for obj in PLAIN_SWITCHES:
        set_state(obj, "ON" if obj in DEFAULT_ON else "OFF")
    for obj, (name, unit, dclass, val) in SENSORS.items():
        set_state(obj, str(val))
    set_state("dmaker_p221_5b47_fan", "OFF")
    pub(f"{BASE}/dmaker_p221_5b47_fan/pct", "0")
    set_state("philips_strip3_12ad_light", "OFF")
    pub(f"{BASE}/philips_strip3_12ad_light/bri", "0")
    set_state("philips_cn_531616941_strip3_s_2", "OFF")
    pub(f"{BASE}/philips_cn_531616941_strip3_s_2/bri", "0")
    print("twin initial states published:", len(state), "entities")
    threading.Thread(target=jitter_loop, daemon=True).start()


def jitter_loop():
    """Republish power sensors every 20s with small variation so the Riemann
    integration / utility meters / powercalc energy actually accumulate."""
    while True:
        time.sleep(20)
        for pid, (label, watts) in SONOFF.items():
            sw = f"sonoff_{pid}_1"
            on = state.get(sw) == "ON"
            base = watts if on else 0.4
            # round to 2 dp so even small standby values genuinely change each
            # tick -- otherwise HA fires no state_changed and the Riemann
            # integration never advances.
            val = round(base * random.uniform(0.85, 1.15), 2)
            set_state(sonoff_power_obj(pid), str(val))
        for obj, (name, unit, dclass, v) in SENSORS.items():
            if dclass == "power":
                set_state(obj, str(round(v * random.uniform(0.85, 1.15), 2)))


def on_message(c, u, msg):
    topic = msg.topic
    payload = msg.payload.decode()
    parts = topic.split("/")
    # twin/<obj>/set | twin/<obj>/pct/set | twin/<obj>/bri/set | twin/<obj>/press
    if topic.endswith("/pct/set"):
        obj = parts[1]; pub(f"{BASE}/{obj}/pct", payload)
        return
    if topic.endswith("/bri/set"):
        obj = parts[1]; pub(f"{BASE}/{obj}/bri", payload)
        set_state(obj, "ON")
        return
    if topic.endswith("/press"):
        return
    obj = parts[1]
    set_state(obj, payload)
    # if a sonoff plug switch toggled, drive its power sensor
    for pid, (label, watts) in SONOFF.items():
        if obj == f"sonoff_{pid}_1":
            set_state(sonoff_power_obj(pid), str(watts if payload == "ON" else 0.4))


client.on_connect = on_connect
client.on_message = on_message
client.connect("127.0.0.1", 1883, 60)
client.loop_forever()
