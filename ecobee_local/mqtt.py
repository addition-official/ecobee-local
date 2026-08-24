"""
ecobee_mqtt.py -- an optional MQTT bridge for ecobee-local.

It connects to your thermostats (using the ecobee-local library) and mirrors
them onto an MQTT broker, so other tools (Node-RED, dashboards, custom scripts,
Home Assistant, etc.) can read state and send commands over MQTT.

It does two things continuously:

  1. PUBLISH STATE  -- every few seconds it reads each thermostat and publishes
     values to topics like:
         ecobee/<label>/temperature      -> 78
         ecobee/<label>/target           -> 75
         ecobee/<label>/mode             -> cool          (off/heat/cool/auto)
         ecobee/<label>/humidity         -> 57
         ecobee/<label>/fan              -> auto           (auto/on)
         ecobee/<label>/comfort          -> home           (home/sleep/away/hold)
         ecobee/<label>/online           -> true
     plus a JSON blob of everything at:
         ecobee/<label>/state            -> {...}

  2. LISTEN FOR COMMANDS  -- it subscribes to command topics and applies them:
         ecobee/<label>/set/target       payload: 72        (Fahrenheit)
         ecobee/<label>/set/target_c     payload: 22.5      (Celsius)
         ecobee/<label>/set/mode         payload: cool      (off/heat/cool/auto)
         ecobee/<label>/set/heat         payload: 68        (auto-mode threshold, F)
         ecobee/<label>/set/cool         payload: 75        (auto-mode threshold, F)
         ecobee/<label>/set/fan          payload: auto      (auto/on)
         ecobee/<label>/set/humidity     payload: 40        (percent)
         ecobee/<label>/set/comfort      payload: away      (home/sleep/away/hold)

Requirements:
    pip install ecobee-local paho-mqtt
    and an MQTT broker reachable on the network (e.g. Mosquitto).

Usage:
    python ecobee_mqtt.py                       # broker at localhost:1883
    python ecobee_mqtt.py --host 192.168.1.50   # broker elsewhere
    python ecobee_mqtt.py --host x --port 1883 --username u --password p
    python ecobee_mqtt.py --prefix ecobee --interval 5

This bridge is intentionally simple and broker-agnostic. It does NOT implement
Home Assistant MQTT Discovery (kept deliberately minimal); state and command
topics are plain and documented above so any MQTT client can use them.
"""

import argparse
import json
import sys
import time

try:
    import paho.mqtt.client as mqtt
except ImportError:
    print("paho-mqtt is required: pip install paho-mqtt", file=sys.stderr)
    sys.exit(1)

from ecobee_local import EcobeeController


MODE_TO_STR = {0: "off", 1: "heat", 2: "cool", 3: "auto"}
STR_TO_MODE = {v: k for k, v in MODE_TO_STR.items()}


def _make_client():
    """Create an MQTT client without triggering paho's deprecation warning.
    Uses the modern CallbackAPIVersion.VERSION2 on paho-mqtt 2.x, and falls
    back to the classic constructor on 1.x."""
    try:
        return mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    except AttributeError:
        return mqtt.Client()


def build_state_payloads(prefix, label, st):
    """Return a list of (topic, payload) pairs for one thermostat's state."""
    out = []

    def pub(field, value):
        out.append((f"{prefix}/{label}/{field}", value))

    online = st.get("online")
    pub("online", "true" if online else "false")

    ct = st.get("current_temp")
    if isinstance(ct, int):
        pub("temperature", str(ct))
    tt = st.get("target_temp")
    if isinstance(tt, int):
        pub("target", str(tt))

    mode = st.get("mode")
    if mode in MODE_TO_STR:
        pub("mode", MODE_TO_STR[mode])

    hum = st.get("humidity")
    if isinstance(hum, int):
        pub("humidity", str(hum))

    tf = st.get("target_fan")  # 1 auto, 0 on
    if tf in (0, 1):
        pub("fan", "auto" if tf == 1 else "on")

    cm = st.get("comfort_mode")
    if cm and cm != "--":
        pub("comfort", cm)

    for k in ("heat_threshold", "cool_threshold"):
        v = st.get(k)
        if isinstance(v, int):
            pub(k, str(v))

    # full state as JSON for consumers that want everything at once
    pub("state", json.dumps(st, default=str))
    return out


def make_command_handler(ec, prefix):
    """Return an on_message callback that applies set/* commands."""
    def on_message(client, userdata, msg):
        topic = msg.topic
        payload = msg.payload.decode("utf-8", "replace").strip()
        # expected: <prefix>/<label>/set/<thing>
        parts = topic.split("/")
        if len(parts) != 4 or parts[0] != prefix or parts[2] != "set":
            return
        label, thing = parts[1], parts[3]

        try:
            if thing == "target":
                res = ec.set_temp(label, int(round(float(payload))))
            elif thing == "target_c":
                res = ec.set_temp_c(label, float(payload))
            elif thing == "mode":
                m = STR_TO_MODE.get(payload.lower())
                res = (ec.set_mode(label, m) if m is not None
                       else {"ok": False, "error": f"bad mode '{payload}'"})
            elif thing == "heat":
                res = ec.set_heat_threshold(label, int(round(float(payload))))
            elif thing == "cool":
                res = ec.set_cool_threshold(label, int(round(float(payload))))
            elif thing == "fan":
                res = ec.set_fan(label, payload.lower() == "auto")
            elif thing == "humidity":
                res = ec.set_target_humidity(label, int(round(float(payload))))
            elif thing == "comfort":
                res = ec.set_comfort_mode(label, payload.lower())
            else:
                res = {"ok": False, "error": f"unknown command '{thing}'"}
        except ValueError:
            res = {"ok": False, "error": f"bad payload '{payload}'"}

        if not res.get("ok"):
            print(f"[cmd] {label}/{thing}={payload!r} -> {res.get('error')}",
                  file=sys.stderr)
        else:
            print(f"[cmd] {label}/{thing}={payload!r} -> ok")
    return on_message


def main():
    ap = argparse.ArgumentParser(description="MQTT bridge for ecobee-local")
    ap.add_argument("--host", default="localhost", help="MQTT broker host")
    ap.add_argument("--port", type=int, default=1883, help="MQTT broker port")
    ap.add_argument("--username", default=None)
    ap.add_argument("--password", default=None)
    ap.add_argument("--prefix", default="ecobee", help="topic prefix")
    ap.add_argument("--interval", type=float, default=5.0,
                    help="seconds between state publishes")
    ap.add_argument("--folder", default=None, help="pairing-files folder")
    ap.add_argument("--retain", action="store_true",
                    help="publish state as retained messages")
    args = ap.parse_args()

    ec = EcobeeController.from_folder(app_folder=args.folder,
                                     comfort_warning=False)
    ec.start()
    labels = list(ec.labels)
    if not labels:
        print("no thermostats found; pair one first with ecobee-pair",
              file=sys.stderr)
        sys.exit(1)
    print(f"thermostats: {', '.join(labels)}")

    prefix = args.prefix.rstrip("/")

    client = _make_client()
    if args.username:
        client.username_pw_set(args.username, args.password)

    # availability topic (last-will so consumers know if the bridge dies)
    avail_topic = f"{prefix}/bridge/online"
    client.will_set(avail_topic, "false", retain=True)

    def on_connect(cl, userdata, flags, rc, *_extra):
        # rc is an int (v1) or a ReasonCode (v2); both compare/str cleanly.
        # *_extra swallows the extra `properties` arg passed by the v2 API.
        failed = (rc != 0) if isinstance(rc, int) else (rc.is_failure
                  if hasattr(rc, "is_failure") else bool(int(rc)))
        if failed:
            print(f"MQTT connect failed ({rc})", file=sys.stderr)
            return
        print(f"connected to broker {args.host}:{args.port}")
        cl.publish(avail_topic, "true", retain=True)
        # subscribe to all command topics
        cl.subscribe(f"{prefix}/+/set/+")
        print(f"listening for commands on {prefix}/<label>/set/<thing>")

    client.on_connect = on_connect
    client.on_message = make_command_handler(ec, prefix)

    try:
        client.connect(args.host, args.port, keepalive=60)
    except Exception as e:
        print(f"could not connect to broker: {e}", file=sys.stderr)
        ec.stop()
        sys.exit(1)

    client.loop_start()
    print(f"publishing state every {args.interval}s. Ctrl+C to stop.")
    try:
        while True:
            for label in labels:
                st = ec.get_status(label)
                for topic, payload in build_state_payloads(prefix, label, st):
                    client.publish(topic, payload, retain=args.retain)
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print("\nstopping...")
    finally:
        client.publish(avail_topic, "false", retain=True)
        client.loop_stop()
        client.disconnect()
        ec.stop()


if __name__ == "__main__":
    main()
